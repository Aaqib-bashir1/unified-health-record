"""
medical_events/models.py
=========================
The core clinical data layer of UHR.

Models:
  MedicalEvent        — base table, every timeline entry
  VisitEvent          — typed extension: clinical visit / encounter
  ObservationEvent    — typed extension: lab result, vital sign
  ConditionEvent      — typed extension: diagnosis
  MedicationEvent     — typed extension: prescription, medication history
  ProcedureEvent      — typed extension: surgery, intervention
  DocumentEvent       — typed extension: uploaded report / prescription PDF
  SecondOpinionEvent  — typed extension: external doctor's opinion

Architectural invariants (from uhr-schema-v1.md section 12):
  12.1 Immutability    — events never modified after creation
  12.2 Provenance      — every event retains actor, source_type, verification_level
  12.3 Patient anchor  — every event references a valid patient_id
  12.4 Authority       — provider_verified only from authenticated practitioners
  12.5 Timeline        — clinical_timestamp is offset-aware, ordered by UTC
  12.7 Staging safety  — visit/share-link events default to pending_approval

Dependency rule:
  medical_events/ depends on → patients/, organisations/, practitioners/
  medical_events/ must never import from → claims/, audit/ at module level
"""

import hashlib
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


# ===========================================================================
# CHOICES
# ===========================================================================

class EventType(models.TextChoices):
    VISIT          = "visit",          "Visit / Encounter"
    OBSERVATION    = "observation",    "Observation / Lab Result"
    CONDITION      = "condition",      "Condition / Diagnosis"
    MEDICATION     = "medication",     "Medication"
    PROCEDURE      = "procedure",      "Procedure / Surgery"
    DOCUMENT       = "document",       "Document / Report"
    SECOND_OPINION = "second_opinion", "Second Opinion"
    ALLERGY        = "allergy",        "Allergy / Intolerance"
    VACCINATION    = "vaccination",    "Vaccination / Immunization"
    CONSULTATION   = "consultation",   "Consultation"
    VITAL_SIGNS    = "vital_signs",    "Vital Signs"


class SourceType(models.TextChoices):
    """
    Who or what created this event.
    Determines default verification_level and visibility rules.
    """
    PATIENT = "patient", "Patient (Self-entered or uploaded)"
    DOCTOR  = "doctor",  "Doctor / Practitioner"
    LAB     = "lab",     "Laboratory (Digital integration)"
    SYSTEM  = "system",  "System (Auto-generated)"


class VerificationLevel(models.TextChoices):
    """
    Confidence level of this event's clinical content.

    Authority matrix (schema invariant 12.4):
      provider_verified  → ONLY authenticated Practitioner accounts
      patient_confirmed  → patient confirmed OCR-extracted data
      self_reported      → patient manually entered, no independent verification
      digitally_verified → received via structured digital integration (lab system)

    The system must NEVER infer verification_level from content alone.
    """
    SELF_REPORTED      = "self_reported",      "Self-Reported (Patient manually entered)"
    PATIENT_CONFIRMED  = "patient_confirmed",  "Patient-Confirmed (OCR-extracted, patient approved)"
    PROVIDER_VERIFIED  = "provider_verified",  "Provider-Verified (Authenticated practitioner)"
    DIGITALLY_VERIFIED = "digitally_verified", "Digitally Verified (Structured digital integration)"


class VisibilityStatus(models.TextChoices):
    """
    Controls whether an event appears in standard timeline queries.

    visible          → appears in all authorised views
    hidden           → patient has hidden it; excluded from shared views
                       (clinical disclosure rule: banner shown to doctors)
    pending_approval → submitted via visit, share link, or doctor contribution
                       excluded from all queries until patient approves
    """
    VISIBLE          = "visible",          "Visible"
    HIDDEN           = "hidden",           "Hidden by Patient"
    PENDING_APPROVAL = "pending_approval", "Pending Patient Approval"


class RelationshipType(models.TextChoices):
    """
    How this event relates to another event via amends_event_id or parent_event_id.
    """
    NONE      = "none",      "No relationship"
    AMENDMENT = "amendment", "Amends a previous event (correction)"
    LIFECYCLE = "lifecycle", "Lifecycle transition (e.g. medication modified/stopped)"
    RELATED   = "related",   "Clinically related event"


class MedicationStatus(models.TextChoices):
    ACTIVE       = "active",       "Active"
    COMPLETED    = "completed",    "Completed (course finished)"
    DISCONTINUED = "discontinued", "Discontinued"
    ON_HOLD      = "on_hold",      "On Hold"
    UNKNOWN      = "unknown",      "Unknown"


class ConditionClinicalStatus(models.TextChoices):
    ACTIVE     = "active",     "Active"
    RECURRENCE = "recurrence", "Recurrence"
    RELAPSE    = "relapse",    "Relapse"
    INACTIVE   = "inactive",   "Inactive"
    REMISSION  = "remission",  "Remission"
    RESOLVED   = "resolved",   "Resolved"


# ===========================================================================
# MODEL: MEDICAL EVENT (base)
# ===========================================================================

class MedicalEvent(models.Model):
    """
    The canonical base record for every entry on a patient's timeline.

    Every clinical fact in UHR — a visit, a lab result, a diagnosis,
    a medication, a procedure, an uploaded report, or a second opinion —
    is stored as a MedicalEvent plus one typed extension (1:1).

    Immutability rule (invariant 12.1):
      Events are NEVER modified after creation.
      Corrections create a new MedicalEvent with:
        relationship_type = amendment
        amends_event_id   = original event
        amendment_reason  = mandatory explanation

    Medication lifecycle rule (spec section 9):
      Medication changes create a new MedicalEvent with:
        relationship_type = lifecycle
        parent_event_id   = original medication event

    Dual timestamp rule (invariant 12.5):
      clinical_timestamp → when the event happened in real life
                           (may be backdated e.g. uploading old reports)
      system_timestamp   → when it was recorded in UHR (auto-set)
      Timeline ordered by clinical_timestamp (UTC).
      UI displays local time for clinical context.

    Staging safety rule (invariant 12.7):
      Events submitted via visit session or share link must default
      to visibility_status = pending_approval and are excluded from
      all standard queries until the patient approves them.

    FHIR R4: Multiple resources depending on event_type.
      See each typed extension for FHIR mapping.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Patient anchor (invariant 12.3) ───────────────────────────────────────
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="medical_events",
    )

    # ── Event classification ──────────────────────────────────────────────────
    event_type = models.CharField(max_length=20, choices=EventType.choices)

    # ── Dual timestamps (invariant 12.5) ──────────────────────────────────────
    # clinical_timestamp: timezone-aware, set by the submitter.
    # system_timestamp:   auto-set at creation, never changed.
    clinical_timestamp = models.DateTimeField(
        help_text=(
            "When this event occurred in real life. "
            "Must be timezone-aware. Used for timeline ordering."
        ),
    )
    system_timestamp = models.DateTimeField(
        auto_now_add=True,
        help_text="When this event was recorded in UHR. Set once. Never changed.",
    )

    # ── Provenance (invariant 12.2) ───────────────────────────────────────────
    source_type = models.CharField(
        max_length=10,
        choices=SourceType.choices,
        help_text="Who or what created this event.",
    )
    source_practitioner = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="authored_events",
        help_text="Populated when source_type=doctor.",
    )
    source_organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sourced_events",
        help_text="The organisation context in which this event was created.",
    )

    # ── Verification (invariant 12.4) ─────────────────────────────────────────
    verification_level = models.CharField(
        max_length=20,
        choices=VerificationLevel.choices,
        default=VerificationLevel.SELF_REPORTED,
        help_text=(
            "Confidence level of this event's clinical content. "
            "provider_verified ONLY for authenticated practitioner submissions."
        ),
    )

    # ── Visibility ────────────────────────────────────────────────────────────
    visibility_status = models.CharField(
        max_length=20,
        choices=VisibilityStatus.choices,
        default=VisibilityStatus.VISIBLE,
    )

    # ── Creator ───────────────────────────────────────────────────────────────
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_medical_events",
        help_text="The user account that submitted this event.",
    )

    # ── Amendment chain (invariant 12.1) ─────────────────────────────────────
    # amends_event_id:  set when this event CORRECTS a previous one
    # parent_event_id:  set when this event is a LIFECYCLE transition
    # relationship_type: clarifies the nature of the link
    amends_event = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="amendments",
        help_text="The event this one corrects. Required when relationship_type=amendment.",
    )
    amendment_reason = models.TextField(
        null=True,
        blank=True,
        help_text=(
            "Mandatory when amending. Explains why the correction was made. "
            "e.g. 'Typographical error in dosage', 'Updated clinical evidence'."
        ),
    )
    parent_event = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="lifecycle_events",
        help_text="The medication event this lifecycle transition references.",
    )
    relationship_type = models.CharField(
        max_length=10,
        choices=RelationshipType.choices,
        default=RelationshipType.NONE,
    )

    # ── Interoperability hooks (schema section 8) ─────────────────────────────
    external_system      = models.CharField(max_length=100, null=True, blank=True)
    external_resource_id = models.CharField(max_length=256, null=True, blank=True)
    fhir_resource_type   = models.CharField(max_length=64,  null=True, blank=True)
    fhir_logical_id      = models.CharField(max_length=256, null=True, blank=True)

    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "medical_events"
        constraints = [
            # Amendment consistency: amends_event required when type=amendment
            models.CheckConstraint(
                condition=(
                    models.Q(relationship_type="amendment", amends_event__isnull=False)
                    | ~models.Q(relationship_type="amendment")
                ),
                name="chk_me_amendment_requires_amends_event",
            ),
            # Lifecycle consistency: parent_event required when type=lifecycle
            models.CheckConstraint(
                condition=(
                    models.Q(relationship_type="lifecycle", parent_event__isnull=False)
                    | ~models.Q(relationship_type="lifecycle")
                ),
                name="chk_me_lifecycle_requires_parent_event",
            ),
        ]
        indexes = [
            # Primary timeline query (schema section 13.1)
            models.Index(
                fields=["patient", "clinical_timestamp"],
                name="idx_me_patient_clinical_ts",
            ),
            models.Index(fields=["event_type"],           name="idx_me_event_type"),
            models.Index(fields=["source_practitioner"],  name="idx_me_source_prac"),
            models.Index(fields=["visibility_status"],    name="idx_me_visibility"),
            models.Index(fields=["external_resource_id"], name="idx_me_ext_resource"),
        ]
        ordering = ["-clinical_timestamp"]

    def __str__(self):
        return (
            f"[{self.event_type}] Patient {self.patient_id} "
            f"@ {self.clinical_timestamp.date()} "
            f"({self.verification_level})"
        )

    @property
    def is_visible(self) -> bool:
        return self.visibility_status == VisibilityStatus.VISIBLE

    @property
    def is_pending(self) -> bool:
        return self.visibility_status == VisibilityStatus.PENDING_APPROVAL

    @property
    def typed_extension(self):
        """
        Return the typed extension object for this event.
        Returns None if the extension has not been created yet.
        """
        extension_map = {
            EventType.VISIT:          "visit_event",
            EventType.OBSERVATION:    "observation_event",
            EventType.CONDITION:      "condition_event",
            EventType.MEDICATION:     "medication_event",
            EventType.PROCEDURE:      "procedure_event",
            EventType.DOCUMENT:       "document_event",
            EventType.SECOND_OPINION: "second_opinion_event",
            EventType.ALLERGY:        "allergy_event",
            EventType.VACCINATION:    "vaccination_event",
            EventType.CONSULTATION:   "consultation_event",
            EventType.VITAL_SIGNS:    "vital_signs_event",
        }
        attr = extension_map.get(self.event_type)
        if not attr:
            return None
        try:
            return getattr(self, attr)
        except Exception:
            return None


# ===========================================================================
# TYPED EXTENSION: VISIT EVENT
# ===========================================================================

class VisitEvent(models.Model):
    """
    Extension for event_type=visit.
    FHIR R4: Encounter resource.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="visit_event",
    )
    reason     = models.TextField(null=True, blank=True)
    visit_type = models.CharField(max_length=100, null=True, blank=True,
                                  help_text="e.g. outpatient, inpatient, emergency, telehealth")
    notes      = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "visit_events"

    def __str__(self):
        return f"VisitEvent → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: OBSERVATION EVENT
# ===========================================================================

class ObservationEvent(models.Model):
    """
    Extension for event_type=observation.
    Lab results, vital signs, measurements.
    FHIR R4: Observation resource.

    Coding: optional. Use LOINC codes when available.
    Value: flexible — numeric (value_quantity + value_unit) or
           text (value_string) depending on the measurement type.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="observation_event",
    )

    # ── Coding (optional — LOINC recommended) ────────────────────────────────
    coding_system  = models.CharField(max_length=100, null=True, blank=True,
                                       help_text="e.g. 'http://loinc.org'")
    coding_code    = models.CharField(max_length=50,  null=True, blank=True,
                                       help_text="e.g. '2345-7' (LOINC for blood glucose)")
    coding_display = models.CharField(max_length=255, null=True, blank=True,
                                       help_text="Human-readable name e.g. 'Glucose [Mass/volume] in Blood'")

    # ── Free text name (always) ───────────────────────────────────────────────
    observation_name = models.CharField(
        max_length=255,
        help_text="Human-readable test name. Always required.",
    )

    # ── Value ─────────────────────────────────────────────────────────────────
    value_type     = models.CharField(max_length=10, default="quantity",
                                       help_text="quantity | string | boolean")
    value_quantity = models.DecimalField(max_digits=12, decimal_places=4,
                                          null=True, blank=True)
    value_unit     = models.CharField(max_length=50, null=True, blank=True,
                                       help_text="e.g. 'mg/dL', 'mmol/L', 'bpm'")
    value_string   = models.CharField(max_length=500, null=True, blank=True)

    # ── Reference range ───────────────────────────────────────────────────────
    reference_range = models.CharField(max_length=100, null=True, blank=True,
                                        help_text="e.g. '70-100 mg/dL'")

    class Meta:
        app_label = "medical_events"
        db_table  = "observation_events"
        indexes   = [
            # Per schema section 13.2: coding_code is the primary search field
            models.Index(fields=["coding_code"], name="idx_obs_coding_code"),
        ]

    def __str__(self):
        return f"ObservationEvent [{self.observation_name}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: CONDITION EVENT
# ===========================================================================

class ConditionEvent(models.Model):
    """
    Extension for event_type=condition.
    Diagnoses, health conditions.
    FHIR R4: Condition resource.

    Coding: optional. Use ICD-10 or SNOMED CT when available.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="condition_event",
    )

    # ── Coding (optional — ICD-10 / SNOMED CT) ───────────────────────────────
    coding_system  = models.CharField(max_length=100, null=True, blank=True)
    coding_code    = models.CharField(max_length=50,  null=True, blank=True)
    coding_display = models.CharField(max_length=255, null=True, blank=True)

    # ── Free text name (always) ───────────────────────────────────────────────
    condition_name = models.CharField(
        max_length=255,
        help_text="Human-readable diagnosis name. Always required.",
    )

    # ── Clinical status ───────────────────────────────────────────────────────
    clinical_status = models.CharField(
        max_length=20,
        choices=ConditionClinicalStatus.choices,
        default=ConditionClinicalStatus.ACTIVE,
    )

    onset_date      = models.DateField(null=True, blank=True)
    abatement_date  = models.DateField(null=True, blank=True)
    notes           = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "condition_events"

    def __str__(self):
        return f"ConditionEvent [{self.condition_name}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: MEDICATION EVENT
# ===========================================================================

class MedicationEvent(models.Model):
    """
    Extension for event_type=medication.
    Medications: prescriptions, OTC, supplements.
    FHIR R4: MedicationRequest resource.

    Lifecycle:
      Medication Started     → new event, relationship_type=none
      Medication Modified    → new event, relationship_type=lifecycle,
                               parent_event → original
      Medication Discontinued → new event, relationship_type=lifecycle,
                               parent_event → original, status=discontinued

    Active medications computed by finding events with status=active
    that have no later lifecycle event setting status to discontinued.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="medication_event",
    )

    medication_name = models.CharField(max_length=255)
    dosage          = models.CharField(max_length=100, null=True, blank=True,
                                        help_text="e.g. '500mg'")
    frequency       = models.CharField(max_length=100, null=True, blank=True,
                                        help_text="e.g. 'twice daily', 'every 8 hours'")
    route           = models.CharField(max_length=100, null=True, blank=True,
                                        help_text="e.g. 'oral', 'intravenous', 'topical'")
    start_date      = models.DateField(null=True, blank=True)
    end_date        = models.DateField(null=True, blank=True)
    status          = models.CharField(
        max_length=20,
        choices=MedicationStatus.choices,
        default=MedicationStatus.ACTIVE,
    )
    notes           = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "medication_events"
        indexes   = [
            # Per schema section 13.3
            models.Index(fields=["status"], name="idx_med_status"),
        ]

    def __str__(self):
        return f"MedicationEvent [{self.medication_name}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: PROCEDURE EVENT
# ===========================================================================

class ProcedureEvent(models.Model):
    """
    Extension for event_type=procedure.
    Surgeries, interventions, therapeutic procedures.
    FHIR R4: Procedure resource.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="procedure_event",
    )

    # ── Coding (optional — SNOMED CT recommended) ────────────────────────────
    coding_system  = models.CharField(max_length=100, null=True, blank=True)
    coding_code    = models.CharField(max_length=50,  null=True, blank=True)
    coding_display = models.CharField(max_length=255, null=True, blank=True)

    procedure_name  = models.CharField(
        max_length=255,
        help_text="Human-readable procedure name. Always required.",
    )
    performed_date  = models.DateField(null=True, blank=True)
    notes           = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "procedure_events"

    def __str__(self):
        return f"ProcedureEvent [{self.procedure_name}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: DOCUMENT EVENT
# ===========================================================================

class DocumentEvent(models.Model):
    """
    Extension for event_type=document.
    Uploaded PDFs, images (lab reports, prescriptions, scans).
    FHIR R4: DocumentReference resource.

    Storage:
      Files stored in S3-compatible storage.
      file_url: presigned or permanent URL to the file.
      storage_provider: 's3' | 'r2' | 'minio' | 'local'
      checksum: SHA-256 of file contents, computed at upload time.

    Integrity rule (schema section 6.6):
      Checksum computed at upload time.
      On every retrieval, checksum must be validated against file contents.
      Mismatch triggers integrity alert and blocks access.

    Document types:
      lab_report | prescription | imaging | discharge_summary
      | referral | vaccination | insurance | other
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="document_event",
    )

    # ── File metadata ─────────────────────────────────────────────────────────
    file_url          = models.TextField(
        help_text="S3 URL or presigned URL to the stored file.",
    )
    file_type         = models.CharField(
        max_length=20,
        help_text="MIME type e.g. 'application/pdf', 'image/jpeg'.",
    )
    document_type     = models.CharField(
        max_length=30,
        default="other",
        help_text=(
            "lab_report | prescription | imaging | discharge_summary "
            "| referral | vaccination | insurance | other"
        ),
    )
    original_filename = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Original filename as uploaded by the patient.",
    )
    file_size_bytes   = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    # ── Integrity (schema invariant — mandatory) ──────────────────────────────
    checksum          = models.CharField(
        max_length=64,
        help_text="SHA-256 hex digest computed at upload time. Validated on every retrieval.",
    )
    storage_provider  = models.CharField(
        max_length=20,
        default="s3",
        help_text="s3 | r2 | minio | local",
    )

    # ── S3 reference ─────────────────────────────────────────────────────────
    s3_bucket         = models.CharField(max_length=255, null=True, blank=True)
    s3_key            = models.CharField(
        max_length=1024,
        null=True,
        blank=True,
        help_text="S3 object key. Used to generate presigned URLs on demand.",
    )

    class Meta:
        app_label = "medical_events"
        db_table  = "document_events"

    def __str__(self):
        return f"DocumentEvent [{self.document_type}] → {self.medical_event_id}"

    @staticmethod
    def compute_checksum(file_bytes: bytes) -> str:
        """Compute SHA-256 hex digest for a file's bytes."""
        return hashlib.sha256(file_bytes).hexdigest()

    def verify_checksum(self, file_bytes: bytes) -> bool:
        """
        Validate file contents against stored checksum.
        Call on every file retrieval. Return False on mismatch.
        """
        return self.compute_checksum(file_bytes) == self.checksum


# ===========================================================================
# TYPED EXTENSION: SECOND OPINION EVENT
# ===========================================================================

class SecondOpinionEvent(models.Model):
    """
    Extension for event_type=second_opinion.
    An external doctor's opinion on the patient's case.
    FHIR R4: Communication resource.

    Submission sources:
      - Share link anonymous doctor (always pending_approval)
      - Registered practitioner with access (pending_approval until patient approves)

    approved_by_patient:
      False (default) → opinion is in pending_approval state
      True → patient has reviewed and approved for inclusion on timeline
      Mirrors visibility_status on the base MedicalEvent.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="second_opinion_event",
    )

    doctor_name                = models.CharField(max_length=200)
    doctor_registration_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Optional — not required for anonymous share-link submissions.",
    )
    opinion_text               = models.TextField()
    approved_by_patient        = models.BooleanField(
        default=False,
        help_text=(
            "True once the patient has reviewed and approved this opinion. "
            "Unapproved opinions are excluded from doctor-facing timeline views."
        ),
    )

    class Meta:
        app_label = "medical_events"
        db_table  = "second_opinion_events"

    def __str__(self):
        return f"SecondOpinionEvent [{self.doctor_name}] → {self.medical_event_id}"


# ===========================================================================
# NEW CHOICES for new event types
# ===========================================================================

class AllergyType(models.TextChoices):
    ALLERGY     = "allergy",     "Allergy"
    INTOLERANCE = "intolerance", "Intolerance"

class AllergyCriticality(models.TextChoices):
    LOW              = "low",              "Low risk"
    HIGH             = "high",             "High risk (life-threatening)"
    UNABLE_TO_ASSESS = "unable_to_assess", "Unable to assess"

class AllergyCategory(models.TextChoices):
    FOOD        = "food",        "Food"
    MEDICATION  = "medication",  "Medication"
    ENVIRONMENT = "environment", "Environment"
    BIOLOGIC    = "biologic",    "Biologic"
    OTHER       = "other",       "Other"

class AllergyStatus(models.TextChoices):
    ACTIVE            = "active",             "Active"
    RESOLVED          = "resolved",           "Resolved"
    ENTERED_IN_ERROR  = "entered_in_error",   "Entered in Error"

class ConsultationDepartment(models.TextChoices):
    GENERAL_PRACTICE  = "general_practice",  "General Practice / Family Medicine"
    CARDIOLOGY        = "cardiology",         "Cardiology"
    NEUROLOGY         = "neurology",          "Neurology"
    ONCOLOGY          = "oncology",           "Oncology"
    ORTHOPAEDICS      = "orthopaedics",       "Orthopaedics"
    GASTROENTEROLOGY  = "gastroenterology",   "Gastroenterology"
    PULMONOLOGY       = "pulmonology",        "Pulmonology / Respiratory"
    NEPHROLOGY        = "nephrology",         "Nephrology"
    ENDOCRINOLOGY     = "endocrinology",      "Endocrinology / Diabetes"
    DERMATOLOGY       = "dermatology",        "Dermatology"
    PSYCHIATRY        = "psychiatry",         "Psychiatry / Mental Health"
    OPHTHALMOLOGY     = "ophthalmology",      "Ophthalmology"
    ENT               = "ent",                "ENT (Ear, Nose & Throat)"
    UROLOGY           = "urology",            "Urology"
    GYNAECOLOGY       = "gynaecology",        "Gynaecology & Obstetrics"
    PAEDIATRICS       = "paediatrics",        "Paediatrics"
    HAEMATOLOGY       = "haematology",        "Haematology"
    RADIOLOGY         = "radiology",          "Radiology"
    RHEUMATOLOGY      = "rheumatology",       "Rheumatology"
    EMERGENCY         = "emergency",          "Emergency Medicine"
    OTHER             = "other",              "Other"


# ===========================================================================
# TYPED EXTENSION: ALLERGY EVENT
# ===========================================================================

class AllergyEvent(models.Model):
    """
    Extension for event_type=allergy.
    Records a patient's allergy or intolerance.

    Clinical importance:
      Allergies affect every prescribing decision. They must be surfaced
      prominently in the doctor dashboard and never buried in the timeline.
      criticality=high events (anaphylaxis risk) must trigger visible alerts.

    FHIR R4: AllergyIntolerance resource.
      substance_name   → AllergyIntolerance.code.text
      coding_code      → AllergyIntolerance.code.coding[].code (SNOMED CT)
      allergy_type     → AllergyIntolerance.type
      category         → AllergyIntolerance.category
      criticality      → AllergyIntolerance.criticality
      clinical_status  → AllergyIntolerance.clinicalStatus
      reaction_type    → AllergyIntolerance.reaction[].manifestation
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="allergy_event",
    )

    # ── Substance ─────────────────────────────────────────────────────────────
    substance_name = models.CharField(
        max_length=255,
        help_text="Free-text substance name. Always required.",
    )
    coding_system  = models.CharField(max_length=100, null=True, blank=True,
                                       help_text="e.g. 'http://snomed.info/sct'")
    coding_code    = models.CharField(max_length=50,  null=True, blank=True)
    coding_display = models.CharField(max_length=255, null=True, blank=True)

    # ── Classification ────────────────────────────────────────────────────────
    allergy_type  = models.CharField(max_length=20,  choices=AllergyType.choices,
                                      default=AllergyType.ALLERGY)
    category      = models.CharField(max_length=20,  choices=AllergyCategory.choices,
                                      default=AllergyCategory.MEDICATION)
    criticality   = models.CharField(max_length=20,  choices=AllergyCriticality.choices,
                                      default=AllergyCriticality.UNABLE_TO_ASSESS)

    # ── Reaction ──────────────────────────────────────────────────────────────
    reaction_type = models.CharField(
        max_length=200,
        null=True, blank=True,
        help_text="e.g. 'anaphylaxis', 'urticaria', 'nausea', 'angioedema'",
    )
    reaction_severity = models.CharField(
        max_length=20,
        null=True, blank=True,
        help_text="mild | moderate | severe",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    clinical_status = models.CharField(
        max_length=20,
        choices=AllergyStatus.choices,
        default=AllergyStatus.ACTIVE,
    )
    onset_date = models.DateField(null=True, blank=True)
    notes      = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "allergy_events"
        indexes   = [
            models.Index(fields=["clinical_status"], name="idx_allergy_status"),
            models.Index(fields=["criticality"],     name="idx_allergy_criticality"),
            models.Index(fields=["category"],        name="idx_allergy_category"),
        ]

    def __str__(self):
        return f"AllergyEvent [{self.substance_name} — {self.criticality}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: VACCINATION EVENT
# ===========================================================================

class VaccinationEvent(models.Model):
    """
    Extension for event_type=vaccination.
    Immunisation history.

    FHIR R4: Immunization resource.
      vaccine_name  → Immunization.vaccineCode.text
      coding_code   → Immunization.vaccineCode.coding[].code (CVX)
      dose_number   → Immunization.protocolApplied[].doseNumber
      lot_number    → Immunization.lotNumber
      administered_date → Immunization.occurrenceDateTime
      administering_organisation → Immunization.location
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="vaccination_event",
    )

    vaccine_name  = models.CharField(max_length=255,
                                      help_text="e.g. 'COVID-19 mRNA (Pfizer-BioNTech)'")
    coding_system = models.CharField(max_length=100, null=True, blank=True,
                                      help_text="CVX system: 'http://hl7.org/fhir/sid/cvx'")
    coding_code   = models.CharField(max_length=50,  null=True, blank=True,
                                      help_text="CVX code e.g. '208' for Pfizer COVID-19")
    coding_display = models.CharField(max_length=255, null=True, blank=True)

    dose_number           = models.CharField(max_length=20, null=True, blank=True,
                                              help_text="e.g. '1', '2', 'Booster'")
    lot_number            = models.CharField(max_length=100, null=True, blank=True)
    administered_date     = models.DateField(null=True, blank=True)
    next_dose_due_date    = models.DateField(null=True, blank=True)
    administering_org     = models.CharField(max_length=255, null=True, blank=True,
                                              help_text="Hospital or clinic where administered.")
    site                  = models.CharField(max_length=100, null=True, blank=True,
                                              help_text="e.g. 'Left arm', 'Right deltoid'")
    route                 = models.CharField(max_length=100, null=True, blank=True,
                                              help_text="e.g. 'Intramuscular', 'Subcutaneous'")
    notes                 = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "vaccination_events"
        indexes   = [
            models.Index(fields=["coding_code"], name="idx_vacc_coding_code"),
        ]

    def __str__(self):
        return f"VaccinationEvent [{self.vaccine_name}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: CONSULTATION EVENT
# ===========================================================================

class ConsultationEvent(models.Model):
    """
    Extension for event_type=consultation.
    A structured clinical note from a specialist or GP consultation.

    Department routing enables filtering by specialty —
    a cardiologist sees cardiology consultations first.

    Unlike a VisitEvent (which just records attendance),
    a ConsultationEvent contains the full clinical note:
    complaint, findings, assessment, and plan.

    FHIR R4: ClinicalImpression resource.
      department              → ClinicalImpression.encounter (context)
      chief_complaint         → ClinicalImpression.description
      assessment              → ClinicalImpression.summary
      examination_findings    → ClinicalImpression.finding[].itemCodeableConcept
      plan                    → ClinicalImpression.prognosisCodeableConcept
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="consultation_event",
    )

    # ── Department routing ────────────────────────────────────────────────────
    department = models.CharField(
        max_length=30,
        choices=ConsultationDepartment.choices,
        default=ConsultationDepartment.GENERAL_PRACTICE,
        help_text="The clinical department this consultation belongs to.",
    )
    sub_specialty = models.CharField(
        max_length=100,
        null=True, blank=True,
        help_text="Further specialization e.g. 'Interventional Cardiology', 'Epilepsy'.",
    )

    # ── Referral chain ────────────────────────────────────────────────────────
    consulting_practitioner = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="consultation_events",
        help_text="The practitioner who conducted this consultation.",
    )
    referred_by = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="referral_consultation_events",
        help_text="The practitioner who referred the patient for this consultation.",
    )

    # ── Clinical content ──────────────────────────────────────────────────────
    chief_complaint         = models.TextField(
        help_text="Why the patient presented. Always required.",
    )
    history_of_present_illness = models.TextField(
        null=True, blank=True,
        help_text="Relevant history leading up to this consultation.",
    )
    examination_findings    = models.TextField(
        null=True, blank=True,
        help_text="Physical examination findings.",
    )
    investigations_ordered  = models.TextField(
        null=True, blank=True,
        help_text="Tests or investigations ordered during this consultation.",
    )
    assessment              = models.TextField(
        null=True, blank=True,
        help_text="Clinical assessment / impression.",
    )
    plan                    = models.TextField(
        null=True, blank=True,
        help_text="Management plan agreed during the consultation.",
    )

    # ── Follow-up ─────────────────────────────────────────────────────────────
    follow_up_date          = models.DateField(null=True, blank=True)
    follow_up_instructions  = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "consultation_events"
        indexes   = [
            models.Index(fields=["department"],              name="idx_consult_dept"),
            models.Index(fields=["consulting_practitioner"], name="idx_consult_prac"),
        ]

    def __str__(self):
        return f"ConsultationEvent [{self.department}] → {self.medical_event_id}"


# ===========================================================================
# TYPED EXTENSION: VITAL SIGNS EVENT
# ===========================================================================

class VitalSignsEvent(models.Model):
    """
    Extension for event_type=vital_signs.
    A dedicated event for a full vital signs set recorded at one time.

    Rationale for separating from ObservationEvent:
      - Vitals are always recorded as a set (BP + HR + temp + SpO2 together)
      - They have a specific FHIR category (vital-signs)
      - The doctor dashboard surfaces them differently from isolated lab results
      - LOINC codes for each component are standardized

    All fields nullable — not every vital is recorded every time.

    FHIR R4: Observation with category=vital-signs.
      Each component → Observation.component[] with LOINC code.
    """
    medical_event = models.OneToOneField(
        MedicalEvent,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="vital_signs_event",
    )

    # ── Blood pressure (LOINC 55284-4 panel) ─────────────────────────────────
    systolic_bp   = models.DecimalField(max_digits=6, decimal_places=1,
                                         null=True, blank=True,
                                         help_text="mmHg — LOINC 8480-6")
    diastolic_bp  = models.DecimalField(max_digits=6, decimal_places=1,
                                         null=True, blank=True,
                                         help_text="mmHg — LOINC 8462-4")
    bp_position   = models.CharField(max_length=20, null=True, blank=True,
                                      help_text="sitting | standing | lying")

    # ── Heart rate (LOINC 8867-4) ─────────────────────────────────────────────
    heart_rate    = models.DecimalField(max_digits=6, decimal_places=1,
                                         null=True, blank=True,
                                         help_text="bpm")
    heart_rhythm  = models.CharField(max_length=50, null=True, blank=True,
                                      help_text="regular | irregular")

    # ── Temperature (LOINC 8310-5) ───────────────────────────────────────────
    temperature   = models.DecimalField(max_digits=5, decimal_places=1,
                                         null=True, blank=True,
                                         help_text="°C")
    temp_site     = models.CharField(max_length=20, null=True, blank=True,
                                      help_text="oral | axillary | tympanic | rectal")

    # ── SpO2 (LOINC 59408-5) ─────────────────────────────────────────────────
    spo2          = models.DecimalField(max_digits=5, decimal_places=1,
                                         null=True, blank=True,
                                         help_text="%")
    on_oxygen     = models.BooleanField(null=True, blank=True,
                                         help_text="Whether patient is on supplemental oxygen.")

    # ── Respiratory rate (LOINC 9279-1) ──────────────────────────────────────
    respiratory_rate = models.DecimalField(max_digits=5, decimal_places=1,
                                            null=True, blank=True,
                                            help_text="breaths/min")

    # ── Weight / Height / BMI ─────────────────────────────────────────────────
    weight_kg     = models.DecimalField(max_digits=6, decimal_places=2,
                                         null=True, blank=True)
    height_cm     = models.DecimalField(max_digits=6, decimal_places=1,
                                         null=True, blank=True)
    bmi           = models.DecimalField(max_digits=5, decimal_places=2,
                                         null=True, blank=True,
                                         help_text="Auto-computed if weight+height provided.")

    # ── Pain ─────────────────────────────────────────────────────────────────
    pain_score    = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text="0–10 numeric pain scale.",
    )

    notes         = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "medical_events"
        db_table  = "vital_signs_events"

    def __str__(self):
        return f"VitalSignsEvent → {self.medical_event_id}"

    def save(self, *args, **kwargs):
        # Auto-compute BMI from weight + height if both provided and BMI not set
        if self.weight_kg and self.height_cm and not self.bmi:
            try:
                h_m = float(self.height_cm) / 100
                self.bmi = round(float(self.weight_kg) / (h_m ** 2), 2)
            except (TypeError, ZeroDivisionError):
                pass
        super().save(*args, **kwargs)