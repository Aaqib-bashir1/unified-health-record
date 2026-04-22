from django.db import models

# Create your models here.
"""
lab_reports/models.py
=====================
Lab report ingestion, extraction staging, and result confirmation.

Models:
  LabIntegration   — external lab system connection (Apollo, Thyrocare, HL7 etc.)
  LabPanel         — reusable grouping of related tests (CBC, LFT, KFT, Lipid)
  LabReport        — a single lab report from any source
  LabReportField   — individual extracted/received values (staging layer)

Design principles:
  - LabReportField is the staging layer. Values live here until confirmed.
  - ObservationEvent (medical_events/) is the permanent clinical record.
  - Once a field is confirmed, resulting_event FK is set — link is permanent.
  - Original LabReport and fields are NEVER deleted — append-only.
  - Source of truth hierarchy:
      digitally_verified (org machine / lab API) > patient_confirmed (OCR review) > self_reported

Ingestion paths:
  1. patient_upload   → DocumentEvent + OCR → LabReportField → patient confirms
  2. organisation_push → LabReportField auto-created → auto ObservationEvent
  3. lab_integration  → same as org_push, via LabIntegration API connection

Dependency rule:
  lab_reports/ depends on → patients/, organisations/, medical_events/
  lab_reports/ must never be imported by → medical_events/, patients/
  medical_events/ is the output layer — lab_reports/ feeds into it

FHIR R4:
  LabReport     → DiagnosticReport resource
  LabReportField → Observation resource (component)
  LabIntegration → MessageHeader / endpoint configuration
"""

import uuid

from django.conf import settings
from django.db import models


# ===========================================================================
# CHOICES
# ===========================================================================

class LabReportSource(models.TextChoices):
    """
    How this lab report entered the system.
    Determines the default verification level of its fields.
    """
    PATIENT_UPLOAD    = "patient_upload",    "Patient Upload (PDF / Image)"
    ORGANISATION_PUSH = "organisation_push", "Organisation Machine Push (Analyser / EHR)"
    LAB_INTEGRATION   = "lab_integration",   "Lab Integration API (Apollo, Thyrocare etc.)"
    MANUAL_ENTRY      = "manual_entry",      "Manual Entry by Patient"


class LabReportStatus(models.TextChoices):
    """
    Lifecycle state of a LabReport.

    Transitions:
      uploaded         → extracting (OCR job started)
      extracting       → extracted (OCR complete, fields created)
      extracted        → pending_review (patient notified to review)
      pending_review   → confirmed (patient confirmed all fields)
      confirmed        → resulted (all ObservationEvents created)

      For org_push / lab_integration:
        received → resulted (direct, no review step unless patient hasn't consented)
        received → pending_review (if patient hasn't pre-consented to auto-import)
    """
    UPLOADED       = "uploaded",       "Uploaded (awaiting extraction)"
    EXTRACTING     = "extracting",     "Extracting (OCR in progress)"
    EXTRACTED      = "extracted",      "Extracted (awaiting patient review)"
    PENDING_REVIEW = "pending_review", "Pending Review (patient notified)"
    CONFIRMED      = "confirmed",      "Confirmed (patient verified)"
    RESULTED       = "resulted",       "Resulted (ObservationEvents created)"
    RECEIVED       = "received",       "Received (from org/lab — no OCR needed)"
    FAILED         = "failed",         "Failed (extraction or ingestion error)"


class FieldStatus(models.TextChoices):
    """
    Individual field confirmation state.
    """
    EXTRACTED  = "extracted",  "Extracted (not yet reviewed)"
    CONFIRMED  = "confirmed",  "Confirmed by Patient"
    CORRECTED  = "corrected",  "Corrected by Patient (value changed)"
    REJECTED   = "rejected",   "Rejected by Patient (not added to timeline)"
    RESULTED   = "resulted",   "Resulted (ObservationEvent created)"


class IntegrationProtocol(models.TextChoices):
    """
    Protocol used by the external lab integration.
    """
    HL7_V2    = "hl7_v2",    "HL7 v2 (ASTM / MLLP)"
    HL7_FHIR  = "hl7_fhir",  "HL7 FHIR R4 (REST)"
    PROPRIETARY = "proprietary", "Proprietary API"
    CSV_SFTP  = "csv_sftp",  "CSV over SFTP"
    WEBHOOK   = "webhook",   "Webhook (HTTP POST)"


# ===========================================================================
# MODEL: LAB INTEGRATION
# ===========================================================================

class LabIntegration(models.Model):
    """
    Connection configuration for an external lab or analyser system.

    One record per external system per organisation.
    Credentials are stored encrypted — never in plain text.

    Examples:
      - Apollo FHIR API endpoint
      - Thyrocare CSV drop
      - Hospital Siemens analyser (HL7 v2 MLLP)
      - Generic webhook from any lab

    FHIR R4: MessageHeader / Endpoint resource (partial)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        related_name="lab_integrations",
        help_text="The organisation that owns this integration.",
    )

    name        = models.CharField(max_length=255,
                                    help_text="e.g. 'Apollo FHIR', 'Thyrocare CSV'")
    protocol    = models.CharField(max_length=20, choices=IntegrationProtocol.choices)
    endpoint    = models.TextField(null=True, blank=True,
                                    help_text="API endpoint URL or SFTP path.")

    # Credentials stored as encrypted JSON — never plain text
    # In production: use django-encrypted-model-fields or AWS Secrets Manager
    credentials_encrypted = models.TextField(
        null=True, blank=True,
        help_text=(
            "Encrypted credentials blob (API key, SFTP password etc.). "
            "Never store plain text here. Use django-encrypted-model-fields."
        ),
    )

    # ── Auto-import consent ───────────────────────────────────────────────────
    # If True, results from this integration are auto-created as ObservationEvents
    # without requiring patient review (digitally_verified trust level).
    # If False, results go to pending_review first.
    auto_import = models.BooleanField(
        default=False,
        help_text=(
            "If True, results are auto-imported as ObservationEvents "
            "without patient review. Requires explicit patient consent per integration."
        ),
    )

    # ── FHIR integration specifics ────────────────────────────────────────────
    fhir_system_uri = models.CharField(
        max_length=512,
        null=True, blank=True,
        help_text="FHIR Identifier.system for this lab's identifiers.",
    )

    # ── State ─────────────────────────────────────────────────────────────────
    is_active    = models.BooleanField(default=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "lab_reports"
        db_table  = "lab_integrations"
        constraints = [
            models.UniqueConstraint(
                fields=["organisation", "name"],
                name="uq_lab_integration_org_name",
            ),
        ]
        indexes = [
            models.Index(fields=["organisation", "is_active"],
                         name="idx_lab_int_org_active"),
        ]

    def __str__(self):
        return f"{self.name} @ {self.organisation.name} [{self.protocol}]"


# ===========================================================================
# MODEL: LAB PANEL
# ===========================================================================

class LabPanel(models.Model):
    """
    A reusable grouping of related lab tests.

    Not per-report — these are system-level templates.
    When a LabReport contains a known panel, fields are grouped under it.

    Examples:
      CBC  → WBC, RBC, HGB, HCT, MCV, MCH, MCHC, PLT
      LFT  → ALT, AST, ALP, GGT, Bilirubin (total/direct), Albumin, Total Protein
      KFT  → Creatinine, BUN, Uric Acid, eGFR
      Lipid → Total Cholesterol, LDL, HDL, VLDL, Triglycerides
      TFT  → TSH, T3, T4, Free T3, Free T4
      HbA1c panel → HbA1c, Fasting Glucose, Post-prandial Glucose

    FHIR R4: Observation with hasMember[] pointing to component observations
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name         = models.CharField(max_length=100, unique=True,
                                     help_text="e.g. 'CBC', 'LFT', 'Lipid Profile'")
    display_name = models.CharField(max_length=200,
                                     help_text="e.g. 'Complete Blood Count'")
    description  = models.TextField(null=True, blank=True)

    # LOINC panel code
    loinc_code   = models.CharField(max_length=20, null=True, blank=True,
                                     help_text="LOINC panel code e.g. '58410-2' for CBC")

    is_active    = models.BooleanField(default=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "lab_reports"
        db_table  = "lab_panels"
        ordering  = ["name"]

    def __str__(self):
        return f"{self.name} — {self.display_name}"


# ===========================================================================
# MODEL: LAB REPORT
# ===========================================================================

class LabReport(models.Model):
    """
    A single lab report from any ingestion source.

    One LabReport = one physical report (one PDF, one machine output, one API batch).
    Multiple LabReportField records hang off it — one per test result.

    Immutability:
      The LabReport record itself is never deleted or modified after creation.
      Status transitions are the only mutations.
      Fields and their confirmed values are the staging layer.
      ObservationEvents are the permanent record.

    FHIR R4: DiagnosticReport resource.
      patient         → DiagnosticReport.subject
      report_date     → DiagnosticReport.effectiveDateTime
      lab_name        → DiagnosticReport.performer
      source          → DiagnosticReport.category (lab, imaging etc.)
      document_event  → DiagnosticReport.presentedForm (the original document)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Patient ───────────────────────────────────────────────────────────────
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="lab_reports",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="lab_reports_created",
    )

    # ── Source ────────────────────────────────────────────────────────────────
    source = models.CharField(
        max_length=25,
        choices=LabReportSource.choices,
        help_text="How this report entered the system.",
    )

    # For org_push / lab_integration sources
    integration = models.ForeignKey(
        LabIntegration,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="lab_reports",
        help_text="Populated for lab_integration source only.",
    )
    uploading_organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="uploaded_lab_reports",
        help_text="Org that pushed/uploaded this report.",
    )

    # ── Report metadata ───────────────────────────────────────────────────────
    lab_name    = models.CharField(max_length=255, null=True, blank=True,
                                    help_text="Name of the lab e.g. 'Apollo Diagnostics'")
    report_date = models.DateField(null=True, blank=True,
                                    help_text="Date the report was generated by the lab.")
    report_id   = models.CharField(max_length=100, null=True, blank=True,
                                    help_text="Lab's own report reference number.")

    # Ordering practitioner (if result from a TestOrder)
    ordered_by = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="lab_reports_ordered",
        help_text="The practitioner who ordered this test, if known.",
    )

    # ── Panel grouping ────────────────────────────────────────────────────────
    panel = models.ForeignKey(
        LabPanel,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="lab_reports",
        help_text="If this report matches a known panel (CBC, LFT etc.).",
    )

    # ── Source document (for patient_upload) ─────────────────────────────────
    # FK to the DocumentEvent in medical_events/ — the original file.
    # This is the source of truth for uploaded reports.
    document_event = models.OneToOneField(
        "medical_events.MedicalEvent",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="lab_report",
        help_text=(
            "The DocumentEvent in medical_events/ holding the original file. "
            "Set for patient_upload source. This is the source of truth."
        ),
    )

    # ── OCR / extraction metadata ─────────────────────────────────────────────
    # Populated after OCR runs. Null until extraction completes.
    ocr_provider      = models.CharField(max_length=100, null=True, blank=True,
                                          help_text="e.g. 'google_vision', 'aws_textract', 'tesseract'")
    ocr_confidence    = models.FloatField(null=True, blank=True,
                                           help_text="Overall OCR confidence score 0.0–1.0")
    ocr_raw_output    = models.JSONField(null=True, blank=True,
                                          help_text=(
                                              "Raw OCR output. Internal use only. "
                                              "Never exposed in patient-facing APIs."
                                          ))
    ocr_completed_at  = models.DateTimeField(null=True, blank=True)
    ocr_error_message = models.TextField(null=True, blank=True,
                                          help_text="Error detail if OCR failed.")

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status     = models.CharField(max_length=20, choices=LabReportStatus.choices,
                                   default=LabReportStatus.UPLOADED)
    notes      = models.TextField(null=True, blank=True)

    # When the patient confirmed all fields
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="lab_reports_confirmed",
    )

    # When all ObservationEvents were created
    resulted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "lab_reports"
        db_table  = "lab_reports"
        indexes = [
            models.Index(fields=["patient", "status"],
                         name="idx_lab_report_patient_status"),
            models.Index(fields=["patient", "report_date"],
                         name="idx_lab_report_patient_date"),
            models.Index(fields=["integration", "report_id"],
                         name="idx_lab_report_integration"),
            models.Index(fields=["uploading_organisation"],
                         name="idx_lab_report_org"),
            models.Index(fields=["status", "created_at"],
                         name="idx_lab_report_status_date"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"LabReport [{self.source}] "
            f"patient={self.patient_id} "
            f"lab={self.lab_name or 'unknown'} "
            f"date={self.report_date} "
            f"[{self.status}]"
        )

    @property
    def total_fields(self) -> int:
        return self.fields.count()

    @property
    def confirmed_fields(self) -> int:
        return self.fields.filter(
            status__in=[FieldStatus.CONFIRMED, FieldStatus.CORRECTED, FieldStatus.RESULTED]
        ).count()

    @property
    def pending_fields(self) -> int:
        return self.fields.filter(status=FieldStatus.EXTRACTED).count()

    @property
    def is_fully_confirmed(self) -> bool:
        """True when every field has been reviewed by the patient."""
        return self.total_fields > 0 and self.pending_fields == 0


# ===========================================================================
# MODEL: LAB REPORT FIELD
# ===========================================================================

class LabReportField(models.Model):
    """
    A single extracted or received value from a LabReport.

    This is the staging layer between raw input and the permanent
    ObservationEvent in medical_events/.

    Confirmation flow:
      EXTRACTED → patient reviews → CONFIRMED or CORRECTED or REJECTED
      CONFIRMED/CORRECTED → ObservationEvent created → RESULTED

    For org_push / lab_integration:
      If auto_import=True on the integration:
        Field status = RESULTED immediately (no review step)
      If auto_import=False:
        Field status = EXTRACTED (patient reviews)

    Patient corrections:
      If the patient corrects a value, patient_corrected_value is stored.
      The ObservationEvent is created from patient_corrected_value.
      The original extracted value is preserved for audit.

    FHIR R4: Observation resource.
      test_name       → Observation.code.text
      loinc_code      → Observation.code.coding[].code
      confirmed_value → Observation.value[x]
      unit            → Observation.value[x].unit
      reference_range → Observation.referenceRange[].text
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    lab_report = models.ForeignKey(
        LabReport,
        on_delete=models.PROTECT,
        related_name="fields",
    )

    # ── Test identification ───────────────────────────────────────────────────
    test_name = models.CharField(
        max_length=255,
        help_text="Human-readable test name as it appears in the report.",
    )
    loinc_code    = models.CharField(max_length=20,  null=True, blank=True,
                                      help_text="Matched LOINC code. Null if not matched.")
    loinc_display = models.CharField(max_length=255, null=True, blank=True)
    panel         = models.ForeignKey(
        LabPanel,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="fields",
        help_text="Which panel this field belongs to, if identified.",
    )

    # ── Raw extracted value (immutable after creation) ────────────────────────
    extracted_value = models.CharField(
        max_length=500,
        null=True, blank=True,
        help_text=(
            "Raw value as extracted by OCR or received from the lab. "
            "Never modified after creation."
        ),
    )
    extracted_unit  = models.CharField(max_length=50, null=True, blank=True)

    # OCR confidence for this specific field (may differ from overall report confidence)
    field_confidence = models.FloatField(
        null=True, blank=True,
        help_text="OCR confidence score for this specific field (0.0–1.0).",
    )

    # ── Patient-reviewed value ────────────────────────────────────────────────
    # If patient confirmed without change → patient_corrected_value is null
    # If patient corrected the value → patient_corrected_value holds the correction
    patient_corrected_value = models.CharField(
        max_length=500,
        null=True, blank=True,
        help_text="Patient's corrected value, if different from extracted_value.",
    )
    patient_corrected_unit = models.CharField(max_length=50, null=True, blank=True)

    # ── Reference range ───────────────────────────────────────────────────────
    reference_range      = models.CharField(max_length=200, null=True, blank=True,
                                             help_text="e.g. '70–100 mg/dL'")
    is_abnormal          = models.BooleanField(
        null=True,
        help_text="True if value is outside reference range. Null if unknown.",
    )
    abnormal_flag        = models.CharField(
        max_length=10, null=True, blank=True,
        help_text="Lab flag: H (high), L (low), HH (critical high), LL (critical low), N (normal).",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=15,
        choices=FieldStatus.choices,
        default=FieldStatus.EXTRACTED,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="lab_fields_reviewed",
    )
    rejection_reason = models.CharField(
        max_length=500,
        null=True, blank=True,
        help_text="Why the patient rejected this field.",
    )

    # ── Result linkage ────────────────────────────────────────────────────────
    resulting_event = models.OneToOneField(
        "medical_events.MedicalEvent",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="source_lab_field",
        help_text=(
            "The ObservationEvent created from this field. "
            "Null until the field is confirmed and resulted."
        ),
    )

    # Display ordering within the report
    display_order = models.PositiveSmallIntegerField(
        default=0,
        help_text="Order in which to display this field within the report.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "lab_reports"
        db_table  = "lab_report_fields"
        indexes = [
            models.Index(fields=["lab_report", "status"],
                         name="idx_lrf_report_status"),
            models.Index(fields=["loinc_code"],
                         name="idx_lrf_loinc_code"),
            models.Index(fields=["is_abnormal"],
                         name="idx_lrf_abnormal"),
        ]
        ordering = ["display_order", "test_name"]

    def __str__(self):
        return (
            f"LabReportField [{self.test_name}] "
            f"value={self.extracted_value} {self.extracted_unit or ''} "
            f"[{self.status}]"
        )

    @property
    def confirmed_value(self) -> str:
        """
        The value to use when creating the ObservationEvent.
        Patient correction takes precedence over extracted value.
        """
        return self.patient_corrected_value or self.extracted_value

    @property
    def confirmed_unit(self) -> str:
        return self.patient_corrected_unit or self.extracted_unit