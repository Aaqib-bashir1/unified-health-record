"""
visits/models.py
================
Organisation-scoped visit sessions — lazy practitioner access.

Models:
  - PatientVisit        — created when patient scans org QR at reception
  - PatientVisitAccess  — created lazily when a practitioner first accesses the patient

Design rules:
  - Patient initiates — patient scans the org's static QR (patient sovereignty)
  - Lazy access — PatientVisitAccess is created on first practitioner lookup,
    not at visit creation. Audit log reflects actual access, not theoretical grants.
  - All practitioners at a verified organisation can access during an active visit
  - Visit auto-expires at expires_at (24hr default)
  - Patient or org admin can end a visit early
  - All visit-submitted medical events default to visibility_status=pending_approval
    (schema invariant 12.7)

Dependency rule:
  visits/ depends on → patients/, integrations/ (Organisation model)
  visits/ must never be imported by → patients/, share/
"""

import uuid

from django.conf import settings
from django.db import models


# ===========================================================================
# MODEL: PATIENT VISIT
# ===========================================================================

class PatientVisit(models.Model):
    """
    An in-person visit session created when a patient scans an
    organisation's QR code at reception.

    During an active visit, any verified practitioner at that organisation
    can access the patient's timeline. Access is created lazily on first lookup.

    The visit auto-expires at expires_at. The patient or an org admin
    can end it early via the API.

    FHIR R4: Encounter resource (partial mapping).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="visits",
    )

    # The organisation the patient is visiting.
    # String reference — visits/ depends on organisations/, not vice versa.
    organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        related_name="patient_visits",
    )

    # The user who initiated the visit (the patient's UHR user account).
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="initiated_visits",
    )

    # ── Timing ────────────────────────────────────────────────────────────────
    initiated_at = models.DateTimeField(auto_now_add=True)

    # Default 24 hours. Extended by org admin or patient if needed.
    expires_at = models.DateTimeField(
        help_text="When this visit session automatically expires.",
    )

    # Set when patient or org admin explicitly ends the visit early.
    ended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Explicit end time. Null if visit expired naturally.",
    )
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ended_visits",
    )

    # ── State ─────────────────────────────────────────────────────────────────
    is_active = models.BooleanField(
        default=True,
        help_text="False once ended or expired.",
    )

    # ── Optional clinical context ─────────────────────────────────────────────
    # Patient or reception can provide context for the visit.
    visit_reason = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Optional reason for visit e.g. 'Annual checkup', 'Follow-up'.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "visits"
        db_table  = "patient_visits"
        indexes = [
            # Find active visit for a patient at an org — the core access check query
            models.Index(
                fields=["patient", "organisation", "is_active"],
                name="idx_visit_patient_org_active",
            ),
            # Patient's visit history
            models.Index(
                fields=["patient", "initiated_at"],
                name="idx_visit_patient_history",
            ),
            # Expiry sweep
            models.Index(
                fields=["is_active", "expires_at"],
                name="idx_visit_active_expiry",
            ),
        ]
        ordering = ["-initiated_at"]

    def __str__(self):
        return (
            f"PatientVisit patient={self.patient_id} "
            f"org={self.organisation_id} active={self.is_active}"
        )

    @property
    def is_currently_active(self) -> bool:
        """True if the visit is active and has not expired."""
        from django.utils import timezone
        return self.is_active and self.expires_at > timezone.now()


# ===========================================================================
# MODEL: PATIENT VISIT ACCESS
# ===========================================================================

class PatientVisitAccess(models.Model):
    """
    A lazy access record created when a practitioner first accesses a patient's
    timeline during an active visit.

    Created by: visits.services._get_or_create_visit_access()
    Called by:  any practitioner-facing endpoint that checks visit access.

    Why lazy (not eager):
      - Eager would create records for all org practitioners at visit start —
        hundreds of phantom records for access that may never happen.
      - Lazy creates a record only when access actually occurs.
      - The audit log reflects truth: this practitioner actually accessed this patient.

    Revocation:
      - Individual record: revoke this practitioner's access mid-visit
      - Whole visit: ending the visit deactivates all derived visit access records

    FHIR: No direct mapping. UHR-native audit construct.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    visit = models.ForeignKey(
        PatientVisit,
        on_delete=models.PROTECT,
        related_name="practitioner_accesses",
    )

    # Denormalised for query speed — avoids join through visit on every access check.
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="visit_accesses",
    )

    # The practitioner who accessed the record.
    # String reference to avoid circular imports with integrations/.
    practitioner = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        related_name="patient_visit_accesses",
    )

    # ── Timing ────────────────────────────────────────────────────────────────
    first_accessed_at = models.DateTimeField(auto_now_add=True)
    last_accessed_at  = models.DateTimeField(auto_now=True)

    # ── State ─────────────────────────────────────────────────────────────────
    is_active = models.BooleanField(
        default=True,
        help_text="False if this practitioner's access was revoked mid-visit.",
    )
    revoked_at        = models.DateTimeField(null=True, blank=True)
    revoked_by        = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="revoked_visit_accesses",
    )
    revocation_reason = models.TextField(null=True, blank=True)

    class Meta:
        app_label = "visits"
        db_table  = "patient_visit_accesses"
        constraints = [
            # One visit access record per (visit, practitioner).
            # The record is updated (last_accessed_at) on subsequent access,
            # not duplicated.
            models.UniqueConstraint(
                fields=["visit", "practitioner"],
                name="uq_visit_access_visit_practitioner",
            ),
        ]
        indexes = [
            # Check if a practitioner already has a record for this visit
            models.Index(
                fields=["visit", "practitioner"],
                name="idx_visit_access_visit_prac",
            ),
            # All accesses for a patient (audit view)
            models.Index(
                fields=["patient", "first_accessed_at"],
                name="idx_visit_access_patient",
            ),
        ]

    def __str__(self):
        return (
            f"PatientVisitAccess visit={self.visit_id} "
            f"practitioner={self.practitioner_id} active={self.is_active}"
        )