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
# CHOICES
# ===========================================================================

class VisitScope(models.TextChoices):
    """
    What clinical data an organisation can access during a visit.

    stats_only (default):
      Automatically granted when patient initiates a visit.
      Covers: active allergies, active medications, active conditions,
              recent vitals, blood group, basic demographics.
      No patient approval needed — equivalent to triage-level access.
      Maps to FHIR SMART scope: patient/Observation.read + patient/AllergyIntolerance.read
                                 + patient/MedicationRequest.read + patient/Condition.read

    full_timeline:
      Requires explicit patient approval via VisitScopeRequest.
      Covers: full event history, documents, consultation notes, second opinions.
      Maps to FHIR SMART scope: patient/*.read
    """
    STATS_ONLY    = "stats_only",    "Stats Only (Triage — auto-granted)"
    FULL_TIMELINE = "full_timeline", "Full Timeline (requires patient approval)"


class ScopeRequestStatus(models.TextChoices):
    PENDING  = "pending",  "Pending patient approval"
    APPROVED = "approved", "Approved"
    DENIED   = "denied",   "Denied"
    EXPIRED  = "expired",  "Expired (patient did not respond)"


# ===========================================================================
# MODEL: PATIENT VISIT
# ===========================================================================

class VisitAccessScope(models.TextChoices):
    """
    What clinical data the visiting organisation can access.

    emergency_summary (default, auto-granted):
      Blood group, active allergies, active medications,
      recent vitals, active conditions.
      Granted immediately when patient initiates visit.
      No patient action required — this is the IPS (International Patient Summary).
      Always available to verified organisations.

    full_timeline (requires patient approval):
      Complete visible timeline — all event types the patient has not hidden.
      Org requests via VisitTimelineRequest.
      Patient approves or denies per visit.
      Maps to FHIR Consent with scope=patient/$everything.
    """
    EMERGENCY_SUMMARY = "emergency_summary", "Emergency Summary (Auto-granted)"
    FULL_TIMELINE     = "full_timeline",     "Full Timeline (Patient approved)"


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

    # ── Access scope ──────────────────────────────────────────────────────────
    # Default: stats_only — automatic, no patient approval required.
    # Upgrades to full_timeline only after patient approves a VisitScopeRequest.
    scope = models.CharField(
        max_length=20,
        choices=VisitScope.choices,
        default=VisitScope.STATS_ONLY,
        help_text=(
            "What data this organisation can access. "
            "stats_only is auto-granted. full_timeline requires patient approval."
        ),
    )

    # ── Access scope ──────────────────────────────────────────────────────────
    # Default: stats_only — automatic Tier 1 (health stats, allergies,
    # active meds, active conditions). No patient approval needed.
    # full_timeline — patient explicitly approved via OrgTimelineRequest.
    access_scope = models.CharField(
        max_length=20,
        default="stats_only",
        help_text=(
            "stats_only (default): health stats, allergies, active meds, conditions. "
            "full_timeline: patient approved full record access for this visit."
        ),
    )

    # ── Optional clinical context ─────────────────────────────────────────────
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


# ===========================================================================
# MODEL: VISIT SCOPE REQUEST
# ===========================================================================

class VisitScopeRequest(models.Model):
    """
    A practitioner's request for full timeline access during a visit.

    Default visit scope is stats_only (auto-granted, no approval needed).
    When a practitioner needs the full timeline, they submit this request.
    The patient approves or denies — if approved, the parent PatientVisit
    scope upgrades to full_timeline for the remainder of the visit.

    FHIR alignment:
      This maps to a FHIR Consent resource with scope = patient/*.read.
      The VisitScopeRequest is the consent request.
      Approval creates an implicit Consent record (the scope upgrade on PatientVisit).

    Healthcare compliance:
      The patient's explicit approval is the legal and ethical basis for
      full timeline access. Denial must be respected immediately.
      All decisions are permanently recorded for audit.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    visit = models.ForeignKey(
        PatientVisit,
        on_delete=models.PROTECT,
        related_name="scope_requests",
    )

    requested_by = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        related_name="scope_requests_made",
        help_text="The practitioner requesting full timeline access.",
    )

    reason = models.TextField(
        help_text=(
            "Clinical reason for requesting full timeline access. "
            "Shown to the patient. Be specific — e.g. 'Reviewing full cardiac history "
            "before procedure' rather than 'Need to see records'."
        ),
    )

    status = models.CharField(
        max_length=10,
        choices=ScopeRequestStatus.choices,
        default=ScopeRequestStatus.PENDING,
    )

    # ── Response ──────────────────────────────────────────────────────────────
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="scope_request_responses",
    )
    denial_reason = models.TextField(
        null=True,
        blank=True,
        help_text="Optional reason shown to the practitioner on denial.",
    )

    # ── Expiry ────────────────────────────────────────────────────────────────
    # Request expires if patient doesn't respond (e.g. unconscious patient in ER)
    # In ER break-glass scenarios this is handled separately
    expires_at = models.DateTimeField(
        help_text="Request expires if patient does not respond by this time.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "visits"
        db_table  = "visit_scope_requests"
        constraints = [
            # One pending request per (visit, practitioner)
            models.UniqueConstraint(
                fields=["visit", "requested_by"],
                condition=models.Q(status="pending"),
                name="uq_scope_req_one_pending_per_prac",
            ),
        ]
        indexes = [
            models.Index(
                fields=["visit", "status"],
                name="idx_scope_req_visit_status",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"VisitScopeRequest visit={self.visit_id} "
            f"by={self.requested_by_id} status={self.status}"
        )

    @property
    def is_pending(self) -> bool:
        from django.utils import timezone
        return (
            self.status == ScopeRequestStatus.PENDING
            and self.expires_at > timezone.now()
        )


# ===========================================================================
# CHOICES: VISIT CONSENT
# ===========================================================================

class ConsentScope(models.TextChoices):
    """
    What a VisitConsentRequest grants access to.

    clinical_summary → Tier 1 (auto-available, no request needed)
                       Allergies, active meds, active conditions, vitals, blood group
    full_timeline    → Tier 2 (explicit patient consent required)
                       All visible events, documents, full history
    date_range       → Tier 2 scoped variant
                       Patient chooses a specific date window
    """
    CLINICAL_SUMMARY = "clinical_summary", "Clinical Summary (Tier 1 — auto)"
    FULL_TIMELINE    = "full_timeline",    "Full Timeline (Tier 2 — consent required)"
    DATE_RANGE       = "date_range",       "Date Range (Tier 2 — scoped consent)"


class ConsentRequestStatus(models.TextChoices):
    PENDING  = "pending",  "Pending Patient Response"
    APPROVED = "approved", "Approved"
    DENIED   = "denied",   "Denied"
    REVOKED  = "revoked",  "Revoked by Patient"
    EXPIRED  = "expired",  "Expired (visit ended)"


# ===========================================================================
# MODEL: VISIT CONSENT REQUEST
# ===========================================================================

class VisitConsentRequest(models.Model):
    """
    A request by an organisation practitioner for elevated access
    to a patient's medical data during an active visit.

    Tier 1 (clinical_summary) is AUTOMATIC — no model record needed.
    Any verified practitioner at the org gets the clinical summary
    the moment an active visit exists. This model only tracks Tier 2.

    Tier 2 (full_timeline or date_range) requires explicit patient consent.
    The patient approves or denies via the UHR app. Approved access lasts
    for the duration of the visit unless revoked earlier.

    EHR compliance:
      When full_timeline consent is approved, a FHIR Bundle endpoint
      becomes available for the org to import into their EHR system.
      This is the Tier 3 hook — implemented as a stub now, wired in
      the EHR integration phase.

    Healthcare compliance:
      - Patient initiates the visit (QR scan = consent for Tier 1)
      - All Tier 2 access is explicit and logged
      - Consent expires automatically when the visit ends
      - Patient can revoke at any time during the visit
      - Audit trail is permanent even after consent expires

    FHIR R4: Consent resource (partial mapping).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    visit = models.ForeignKey(
        PatientVisit,
        on_delete=models.PROTECT,
        related_name="consent_requests",
    )

    # Denormalised for query speed — avoids join through visit every time
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="visit_consent_requests",
    )
    organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        related_name="visit_consent_requests",
    )

    requested_by = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        related_name="consent_requests_made",
        help_text="The practitioner who requested elevated access.",
    )

    # ── Scope ─────────────────────────────────────────────────────────────────
    consent_scope  = models.CharField(
        max_length=20,
        choices=ConsentScope.choices,
        default=ConsentScope.FULL_TIMELINE,
    )
    date_range_from = models.DateField(
        null=True, blank=True,
        help_text="Start of date range for date_range scope.",
    )
    date_range_to   = models.DateField(
        null=True, blank=True,
        help_text="End of date range for date_range scope.",
    )

    # ── Clinical reason ───────────────────────────────────────────────────────
    reason = models.TextField(
        help_text="Why full access is needed. Shown to patient.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=10,
        choices=ConsentRequestStatus.choices,
        default=ConsentRequestStatus.PENDING,
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    approved_at  = models.DateTimeField(null=True, blank=True)
    denied_at    = models.DateTimeField(null=True, blank=True)
    revoked_at   = models.DateTimeField(null=True, blank=True)
    expires_at   = models.DateTimeField(
        null=True, blank=True,
        help_text="Auto-set to visit.expires_at when approved.",
    )
    denial_reason = models.TextField(null=True, blank=True)

    # ── EHR export flag ───────────────────────────────────────────────────────
    # Tier 3 hook: when True, FHIR Bundle endpoint is available for this consent.
    # Set True only when consent_scope=full_timeline and status=approved.
    fhir_export_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Tier 3 EHR hook. When True, a FHIR Bundle is available "
            "for import into the organisation's EHR system."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "visits"
        db_table  = "visit_consent_requests"
        constraints = [
            # One active consent request per (visit, organisation) at a time
            models.UniqueConstraint(
                fields=["visit", "organisation"],
                condition=models.Q(status="pending"),
                name="uq_visit_consent_one_pending",
            ),
        ]
        indexes = [
            models.Index(
                fields=["patient", "status"],
                name="idx_visit_consent_patient_status",
            ),
            models.Index(
                fields=["visit", "status"],
                name="idx_visit_consent_visit_status",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"VisitConsentRequest [{self.consent_scope}] "
            f"visit={self.visit_id} status={self.status}"
        )

    @property
    def is_active_consent(self) -> bool:
        from django.utils import timezone
        return (
            self.status == ConsentRequestStatus.APPROVED
            and (self.expires_at is None or self.expires_at > timezone.now())
        )


# ===========================================================================
# MODEL: VISIT TIMELINE REQUEST
# ===========================================================================

class VisitTimelineRequestStatus(models.TextChoices):
    PENDING  = "pending",  "Pending (awaiting patient response)"
    APPROVED = "approved", "Approved"
    DENIED   = "denied",   "Denied"


class VisitTimelineRequest(models.Model):
    """
    A practitioner's request for full timeline access during an active visit.

    Flow:
      1. Practitioner has emergency_summary access (auto-granted)
      2. Practitioner needs more → submits VisitTimelineRequest with reason
      3. Patient receives notification → approves or denies
      4. If approved → PatientVisit.access_scope = full_timeline
      5. Practitioner now sees complete visible timeline

    One active request per visit — prevents request spam.
    Patient can deny and then grant manually via PatientVisit.access_scope.

    FHIR R4:
      Maps to Consent resource with:
        scope    → patient consent for data disclosure
        category → treatment (during visit)
        provision.period → visit duration
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    visit = models.ForeignKey(
        PatientVisit,
        on_delete=models.PROTECT,
        related_name="timeline_requests",
    )

    # Denormalised for query speed
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="visit_timeline_requests",
    )

    requested_by = models.ForeignKey(
        "practitioners.Practitioner",
        on_delete=models.PROTECT,
        related_name="timeline_requests_made",
    )

    reason = models.TextField(
        help_text=(
            "Why full timeline access is needed for this visit. "
            "Shown to the patient when they review the request."
        ),
    )

    status = models.CharField(
        max_length=10,
        choices=VisitTimelineRequestStatus.choices,
        default=VisitTimelineRequestStatus.PENDING,
    )

    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="visit_timeline_responses",
    )
    denial_reason = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "visits"
        db_table  = "visit_timeline_requests"
        constraints = [
            # One pending request per visit at a time
            models.UniqueConstraint(
                fields=["visit"],
                condition=models.Q(status="pending"),
                name="uq_visit_timeline_req_one_pending",
            ),
        ]
        indexes = [
            models.Index(
                fields=["patient", "status"],
                name="idx_visit_tlreq_patient_status",
            ),
            models.Index(
                fields=["visit", "status"],
                name="idx_visit_tlreq_visit_status",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"VisitTimelineRequest visit={self.visit_id} "
            f"by={self.requested_by_id} [{self.status}]"
        )