"""
clinical/models.py
==================
Active clinical workflow objects.

Models:
  - TestOrder   — a doctor-initiated test or investigation order

Unlike medical_events/, records here are MUTABLE — they track
workflow state as it changes (pending → resulted).

When a TestOrder results, it creates an ObservationEvent in medical_events/
and links back to it via resulting_event_id. The permanent record lives in
medical_events/. The workflow lives here.

CPOE extensibility:
  Fields marked # CPOE are nullable now but provide the data structure
  needed to integrate with a full Computerized Physician Order Entry system
  in a future phase without requiring a breaking migration.

Dependency rule:
  clinical/ depends on → patients/, practitioners/, organisations/, medical_events/
  clinical/ must never be imported by → medical_events/, patients/, audit/

FHIR R4: ServiceRequest resource.
  https://www.hl7.org/fhir/servicerequest.html
"""

import uuid

from django.conf import settings
from django.db import models


# ===========================================================================
# CHOICES
# ===========================================================================

class OrderStatus(models.TextChoices):
    """
    Full lifecycle of a test order.

    Transitions:
      draft             → active (doctor confirms and submits)
      active            → specimen_collected (sample taken)
      specimen_collected → in_lab (sample received by lab)
      in_lab            → resulted (result received and linked)
      active/any        → cancelled (order cancelled before result)
      any               → on_hold (temporarily paused)

    FHIR R4: ServiceRequest.status
    """
    DRAFT               = "draft",               "Draft (not yet submitted)"
    ACTIVE              = "active",              "Active (submitted, awaiting collection)"
    SPECIMEN_COLLECTED  = "specimen_collected",  "Specimen Collected"
    IN_LAB              = "in_lab",              "In Lab (processing)"
    RESULTED            = "resulted",            "Resulted"
    CANCELLED           = "cancelled",           "Cancelled"
    ON_HOLD             = "on_hold",             "On Hold"


class OrderPriority(models.TextChoices):
    """
    FHIR R4: ServiceRequest.priority
    """
    ROUTINE = "routine", "Routine"
    URGENT  = "urgent",  "Urgent (same day)"
    STAT    = "stat",    "STAT (immediate)"
    ASAP    = "asap",    "ASAP (as soon as possible)"


class OrderCategory(models.TextChoices):
    """
    Broad category of the order — determines routing and department.
    Maps to FHIR ServiceRequest.category.
    """
    LABORATORY   = "laboratory",   "Laboratory / Pathology"
    IMAGING      = "imaging",      "Imaging / Radiology"
    CARDIOLOGY   = "cardiology",   "Cardiology (ECG, Echo, Stress)"
    PULMONOLOGY  = "pulmonology",  "Pulmonology (PFT, Sleep Study)"
    NEUROLOGY    = "neurology",    "Neurology (EEG, NCS)"
    MICROBIOLOGY = "microbiology", "Microbiology / Culture"
    HISTOLOGY    = "histology",    "Histology / Biopsy"
    SPECIALIST   = "specialist",   "Specialist Consultation"
    OTHER        = "other",        "Other"


# ===========================================================================
# MODEL: TEST ORDER
# ===========================================================================

class TestOrder(models.Model):
    """
    A doctor-initiated test or investigation order.

    Lifecycle:
      1. Doctor creates order (status=active, or draft for CPOE pre-approval)
      2. Patient has specimen collected / attends imaging (status=specimen_collected)
      3. Lab or imaging processes (status=in_lab)
      4. Result received:
         - System or doctor creates ObservationEvent in medical_events/
         - TestOrder.resulting_event is set
         - TestOrder.status = resulted
      5. Patient can see the result on their timeline
         (it's an ObservationEvent, always was)

    CPOE extensibility:
      Fields marked # CPOE are nullable now.
      In a full CPOE phase, these are populated by the order management system.
      Zero migration required to enable CPOE — just populate the fields.

    FHIR R4: ServiceRequest resource.
      test_name          → ServiceRequest.code.text
      loinc_code         → ServiceRequest.code.coding[].code
      category           → ServiceRequest.category
      priority           → ServiceRequest.priority
      clinical_reason    → ServiceRequest.reasonCode[].text
      ordered_at         → ServiceRequest.authoredOn
      ordering_practitioner → ServiceRequest.requester
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Patient and ordering context ──────────────────────────────────────────
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="test_orders",
    )
    ordering_practitioner = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        related_name="test_orders_placed",
        help_text="The practitioner who ordered this test.",
    )
    ordering_organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="test_orders",
        help_text="The organisation context in which this order was placed.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="test_orders_created",
    )

    # ── Test identification ───────────────────────────────────────────────────
    test_name = models.CharField(
        max_length=255,
        help_text="Human-readable test name. e.g. 'Complete Blood Count', 'Chest X-Ray'.",
    )
    category = models.CharField(
        max_length=20,
        choices=OrderCategory.choices,
        default=OrderCategory.LABORATORY,
    )

    # ── Coding (optional — LOINC for labs, SNOMED for procedures) ────────────
    coding_system  = models.CharField(max_length=100, null=True, blank=True,
                                       help_text="'http://loinc.org' or 'http://snomed.info/sct'")
    coding_code    = models.CharField(max_length=50,  null=True, blank=True)
    coding_display = models.CharField(max_length=255, null=True, blank=True)

    # ── Order details ─────────────────────────────────────────────────────────
    priority = models.CharField(
        max_length=10,
        choices=OrderPriority.choices,
        default=OrderPriority.ROUTINE,
    )
    clinical_reason = models.TextField(
        help_text="Why this test is being ordered. Shown to patient and lab.",
    )
    special_instructions = models.TextField(
        null=True, blank=True,
        help_text="e.g. 'Fasting required', 'Collect at 8am', 'Use pediatric tube'.",
    )
    specimen_type = models.CharField(
        max_length=100,
        null=True, blank=True,
        help_text="e.g. 'Venous blood', 'Mid-stream urine', 'Nasopharyngeal swab'.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status     = models.CharField(
        max_length=25,
        choices=OrderStatus.choices,
        default=OrderStatus.ACTIVE,
    )
    ordered_at = models.DateTimeField(auto_now_add=True)
    due_date   = models.DateField(
        null=True, blank=True,
        help_text="When the test should be completed by.",
    )

    # ── Status transition timestamps ──────────────────────────────────────────
    specimen_collected_at = models.DateTimeField(null=True, blank=True)
    resulted_at           = models.DateTimeField(null=True, blank=True)
    cancelled_at          = models.DateTimeField(null=True, blank=True)
    cancellation_reason   = models.TextField(null=True, blank=True)

    # ── Result linkage ────────────────────────────────────────────────────────
    # When the test results, an ObservationEvent is created in medical_events/
    # and linked here. This is the bridge between the workflow (clinical/)
    # and the permanent record (medical_events/).
    resulting_event = models.OneToOneField(
        "medical_events.MedicalEvent",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="source_order",
        help_text=(
            "The ObservationEvent created when this order resulted. "
            "Null until the result is received."
        ),
    )

    # ── CPOE extensibility hooks ──────────────────────────────────────────────
    # All nullable. Populated by CPOE integration in a future phase.
    # Zero migration needed to enable CPOE.

    # CPOE: Unique order ID from the external order management system
    cpoe_order_id      = models.CharField(max_length=256, null=True, blank=True,
                                           help_text="# CPOE — External order ID")

    # CPOE: Order set this order belongs to (e.g. 'Sepsis Bundle')
    order_set_id       = models.UUIDField(null=True, blank=True,
                                           help_text="# CPOE — Parent order set UUID")

    # CPOE: Billing / CPT code for insurance and revenue cycle
    billing_code       = models.CharField(max_length=20, null=True, blank=True,
                                           help_text="# CPOE — CPT or local billing code")

    # CPOE: Lab interface routing code (used in HL7 OBR segment)
    lab_interface_code = models.CharField(max_length=100, null=True, blank=True,
                                           help_text="# CPOE — HL7 OBR-4 universal service ID")

    # CPOE: Target lab or imaging centre
    destination_lab    = models.CharField(max_length=255, null=True, blank=True,
                                           help_text="# CPOE — Where the order is routed")

    # CPOE: Whether this order requires pre-authorisation from insurance
    requires_auth      = models.BooleanField(default=False,
                                              help_text="# CPOE — Insurance pre-auth required")
    auth_reference     = models.CharField(max_length=100, null=True, blank=True,
                                           help_text="# CPOE — Insurance authorisation reference")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "clinical"
        db_table  = "test_orders"
        indexes = [
            # Patient's pending orders
            models.Index(
                fields=["patient", "status"],
                name="idx_order_patient_status",
            ),
            # Practitioner's orders
            models.Index(
                fields=["ordering_practitioner", "status"],
                name="idx_order_prac_status",
            ),
            # Organisation's orders
            models.Index(
                fields=["ordering_organisation", "status"],
                name="idx_order_org_status",
            ),
            # CPOE integration lookup
            models.Index(
                fields=["cpoe_order_id"],
                name="idx_order_cpoe_id",
            ),
            # Order set grouping
            models.Index(
                fields=["order_set_id"],
                name="idx_order_set",
            ),
        ]
        ordering = ["-ordered_at"]

    def __str__(self):
        return (
            f"TestOrder [{self.test_name}] "
            f"patient={self.patient_id} "
            f"status={self.status} "
            f"priority={self.priority}"
        )

    @property
    def is_pending(self) -> bool:
        return self.status in (
            OrderStatus.ACTIVE,
            OrderStatus.DRAFT,
            OrderStatus.SPECIMEN_COLLECTED,
            OrderStatus.IN_LAB,
        )

    @property
    def is_resulted(self) -> bool:
        return self.status == OrderStatus.RESULTED

    @property
    def is_cancelled(self) -> bool:
        return self.status == OrderStatus.CANCELLED