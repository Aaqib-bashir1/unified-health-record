"""
claims/models.py
================
The three-tier profile claim system.

Tiers:
  Tier 1 — National ID Match    (automated, handled in integrations/ + service layer)
  Tier 2 — Email OTP            (automated, ProfileClaimOTP)
  Tier 3 — Support Manual       (human-in-the-loop, SupportClaimRequest)

Models:
  - ProfileClaimOTP      — Tier 2: email OTP issued to the patient profile's email
  - SupportClaimRequest  — Tier 3: support-assisted manual identity verification
  - ClaimAttemptLog      — append-only log of every claim attempt across all tiers

Dependency rule:
  claims/ depends on → patients/, integrations/
  claims/ must never import from → audit/ (audit records its own events)

Note on Tier 1:
  Tier 1 (national ID match) has no model in this app.
  The match is detected by querying integrations.ExternalPatientIdentity.
  On a successful match, the service layer directly creates a PatientUserAccess
  record with claim_method=NATIONAL_ID_MATCH and claim_identity=<matched row>.
  No intermediate model is needed for Tier 1.
"""

import uuid
from django.db import models
from django.conf import settings


# ===========================================================================
# CHOICES
# ===========================================================================

class SupportClaimStatus(models.TextChoices):
    """Lifecycle states of a Tier 3 support claim request."""
    PENDING   = "pending",   "Pending — Awaiting Agent Review"
    IN_REVIEW = "in_review", "In Review — Assigned to Agent"
    APPROVED  = "approved",  "Approved"
    REJECTED  = "rejected",  "Rejected"
    WITHDRAWN = "withdrawn", "Withdrawn by Requester"


class ClaimAttemptOutcome(models.TextChoices):
    """Result of a single claim attempt across any tier."""
    SUCCESS      = "success",      "Success"
    FAILED       = "failed",       "Failed (Wrong OTP / No Match)"
    EXPIRED      = "expired",      "Expired (OTP or Session Timeout)"
    RATE_LIMITED = "rate_limited", "Rate Limited"
    BLOCKED      = "blocked",      "Blocked (Too Many Attempts)"


class ClaimTier(models.TextChoices):
    """Which tier was attempted. Used in ClaimAttemptLog."""
    NATIONAL_ID = "national_id", "Tier 1 — National ID Match"
    EMAIL_OTP   = "email_otp",   "Tier 2 — Email OTP"
    SUPPORT     = "support",     "Tier 3 — Support Manual"


# ===========================================================================
# MODEL: PROFILE CLAIM OTP
# ===========================================================================

class ProfileClaimOTP(models.Model):
    """
    A single email OTP issued for a Tier 2 profile claim attempt.

    How it works:
      - The system sends a 6-digit OTP to the email address on the Patient
        profile (NOT the requesting user's login email — this is critical).
      - Receiving the OTP proves inbox access, which is the trust basis for
        a medium-confidence claim.
      - otp_hash stores a hashed OTP (bcrypt or SHA-256). Raw OTP never stored.
      - is_used=True once successfully verified. Cannot be reused.
      - Expired and used OTPs are retained for audit. Never deleted.

    Rate limiting (enforced at service layer, evidenced here):
      - Maximum 3 OTP requests per patient profile per 24 hours.
      - Maximum 3 verification attempts per OTP record.
      - On 3 failed attempts: OTP is force-invalidated, current full_delegate
        is notified of the failed claim attempt.

    Why the OTP goes to the patient profile's email, not the user's email:
      If a parent set up the child's profile with the child's own email,
      only the child can receive the OTP and claim the profile.
      If the parent used their own email, the parent receives it — which
      is intentional: it forces an explicit handover. The parent must update
      the profile email to the child's address before the child can self-claim.
      This design prevents silent, unilateral profile takeovers.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The patient profile being claimed
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="claim_otps",
    )

    # The user attempting to claim the profile
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claim_otps_requested",
    )

    # ── OTP Security ──────────────────────────────────────────────────────────
    # bcrypt or SHA-256 hash of the raw OTP. Raw OTP is generated, sent, discarded.
    otp_hash = models.CharField(
        max_length=256,
        help_text="Hashed OTP (bcrypt or SHA-256). Raw OTP is never stored.",
    )

    # Masked email for audit display only (e.g. "a***@***.com").
    # Never store the raw target email here — it lives on the Patient profile.
    sent_to_email_masked = models.CharField(
        max_length=256,
        help_text="Masked version of the patient profile email the OTP was sent to. Audit use only.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    expires_at = models.DateTimeField(
        help_text="OTP is valid for 10 minutes from creation."
    )
    is_used    = models.BooleanField(default=False)
    used_at    = models.DateTimeField(null=True, blank=True)

    # Force-invalidated when: 3 failed attempts, or a newer OTP is issued
    # for the same patient (each new OTP invalidates the previous one).
    is_invalidated  = models.BooleanField(default=False)
    invalidated_at  = models.DateTimeField(null=True, blank=True)
    invalidation_reason = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Why this OTP was invalidated (e.g. 'max_attempts', 'superseded').",
    )

    # ── Attempt Tracking ──────────────────────────────────────────────────────
    attempt_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of verification attempts made. Max 3 before force-invalidation.",
    )

    # Immutable creation timestamp. No updated_at — state changes are tracked
    # via the specific timestamp fields above.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "claims"
        db_table  = "profile_claim_otps"
        indexes   = [
            # Rate limit check: OTPs issued for this patient in last 24h
            models.Index(
                fields=["patient", "created_at"],
                name="idx_claimotp_patient_created",
            ),
            # Find the current active OTP for a patient+requester pair
            models.Index(
                fields=["patient", "requested_by", "is_used", "is_invalidated"],
                name="idx_claimotp_active",
            ),
        ]

    def __str__(self):
        return (
            f"OTP claim on Patient {self.patient_id} "
            f"by User {self.requested_by_id} — used={self.is_used}"
        )

    @property
    def is_valid(self) -> bool:
        """True if this OTP can still be used for verification."""
        from django.utils import timezone
        return (
            not self.is_used
            and not self.is_invalidated
            and self.expires_at > timezone.now()
            and self.attempt_count < 3
        )


# ===========================================================================
# MODEL: SUPPORT CLAIM REQUEST
# ===========================================================================

class SupportClaimRequest(models.Model):
    """
    A formal request for Tier 3 support-assisted identity verification.

    Used when:
      - Tier 1: no national ID match exists.
      - Tier 2: no email on the patient profile, or the email is inaccessible.
      - Patient is locked out and cannot self-serve via either automated tier.

    Agent constraints (by design):
      - Agents approve or reject requests. They do NOT directly write
        PatientUserAccess records. The system executes the access grant.
      - This ensures the audit trail is in the system, not in the agent's
        hands, and agent authority is tightly scoped.

    Dispute window:
      - On approval, the current full_delegate is notified with a 14-day
        dispute window. If disputed, the case is escalated for review.
      - The approved access is NOT blocked during the dispute window —
        disputes trigger escalation, not automatic revocation.
      - This balances the legitimate patient's right to their record
        against protection from fraudulent support claims.

    Documents:
      - submitted_document_refs stores storage references (keys / signed URLs)
        to identity documents the user uploaded as part of the claim.
      - Documents themselves live in file storage, not in the database.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="support_claim_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_claim_requests",
    )

    # ── Request Details ───────────────────────────────────────────────────────
    reason = models.TextField(
        help_text="The requester's explanation of why they are claiming this profile.",
    )

    # JSON array of storage references to uploaded identity documents.
    # Example: ["s3://uhr-docs/claims/uuid1.pdf", "s3://uhr-docs/claims/uuid2.jpg"]
    submitted_document_refs = models.JSONField(
        default=list,
        blank=True,
        help_text="Storage references to uploaded identity documents. Not the documents themselves.",
    )

    # ── Workflow State ────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=20,
        choices=SupportClaimStatus.choices,
        default=SupportClaimStatus.PENDING,
    )

    # The support agent assigned to review this request
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_claim_requests",
        null=True,
        blank=True,
    )
    assigned_at = models.DateTimeField(null=True, blank=True)

    # ── Resolution ────────────────────────────────────────────────────────────
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="resolved_claim_requests",
        null=True,
        blank=True,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    # Mandatory on resolution — agents must justify every approval and rejection.
    resolution_note = models.TextField(
        null=True,
        blank=True,
        help_text="Agent's written justification. Mandatory on approval or rejection.",
    )

    # ── Dispute Window ────────────────────────────────────────────────────────
    # Set to resolved_at + 14 days when status transitions to APPROVED.
    dispute_window_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="14-day window for the current delegate to dispute an approved claim.",
    )
    is_disputed    = models.BooleanField(default=False)
    disputed_at    = models.DateTimeField(null=True, blank=True)
    dispute_reason = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "claims"
        db_table  = "support_claim_requests"
        indexes   = [
            # Agent queue: all pending / in-review requests ordered by submission time
            models.Index(fields=["status", "created_at"], name="idx_scr_status_created"),
            # Per-patient claim history
            models.Index(fields=["patient", "status"],    name="idx_scr_patient_status"),
        ]

    def __str__(self):
        return (
            f"Support claim on Patient {self.patient_id} "
            f"by User {self.requested_by_id} — {self.status}"
        )


# ===========================================================================
# MODEL: CLAIM ATTEMPT LOG
# ===========================================================================

class ClaimAttemptLog(models.Model):
    """
    Immutable, append-only record of every profile claim attempt.

    Logs every attempt across all three tiers regardless of outcome:
    success, failure, expiry, rate limit, or block.

    Why separate from the main audit log:
      - Different retention policy: failed OTP attempts may be purged at 90 days;
        successful claims are kept permanently.
      - High-frequency events (OTP retries) would bloat the main audit log.
      - Primary tool for detecting abuse patterns (brute force, IP flooding).

    This table is strictly append-only.
    No record is ever updated or deleted within its retention window.
    There is no updated_at field.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="claim_attempt_logs",
    )
    attempted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claim_attempt_logs",
    )

    # Which tier was attempted
    tier    = models.CharField(max_length=20, choices=ClaimTier.choices)
    outcome = models.CharField(max_length=20, choices=ClaimAttemptOutcome.choices)

    # ── Evidence FKs (one populated per relevant tier) ────────────────────────
    otp_record = models.ForeignKey(
        ProfileClaimOTP,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attempt_logs",
        help_text="Populated for Tier 2 attempts.",
    )
    support_request = models.ForeignKey(
        SupportClaimRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attempt_logs",
        help_text="Populated for Tier 3 attempts.",
    )
    external_identity = models.ForeignKey(
        "integrations.ExternalPatientIdentity",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attempt_logs",
        help_text="Populated for Tier 1 attempts (national ID match).",
    )

    # ── Network Metadata (for abuse detection) ────────────────────────────────
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)

    # Machine-readable failure detail
    failure_reason = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        help_text="Machine-readable failure detail (e.g. 'otp_expired', 'max_attempts_exceeded').",
    )

    # Immutable timestamp. No updated_at on this table.
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "claims"
        db_table  = "claim_attempt_logs"
        indexes   = [
            # Rate limit check: attempts on this patient in last 24h
            models.Index(fields=["patient", "attempted_at"],      name="idx_cal_patient_time"),
            # Abuse detection: attempts from a given IP in a time window
            models.Index(fields=["ip_address", "attempted_at"],   name="idx_cal_ip_time"),
            # Per-user attempt history
            models.Index(fields=["attempted_by", "attempted_at"], name="idx_cal_user_time"),
        ]

    def __str__(self):
        return (
            f"Claim attempt [{self.tier}] on Patient {self.patient_id} "
            f"by User {self.attempted_by_id} — {self.outcome} @ {self.attempted_at}"
        )