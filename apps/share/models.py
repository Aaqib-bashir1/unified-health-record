"""
share/models.py
===============
Patient-initiated temporary access via secure share links.

Models:
  - ShareLink        — a patient-generated secure link for anonymous doctor access
  - ShareLinkSession — a server-side session created after successful DOB/PIN verification

Design rules:
  - Share links are always patient-initiated (patient sovereignty)
  - Anonymous doctor (no UHR account required) accesses via token in URL
  - Validation challenge (year of birth OR PIN) prevents misuse of intercepted links
  - Validator is always bcrypt-hashed — raw DOB/PIN never stored
  - Sessions are server-side and revocable at any time
  - Revoking the parent ShareLink immediately invalidates all its sessions
  - All submissions via share link default to visibility_status=pending_approval
    (schema invariant 12.7 — staging safety)
  - Scope is fixed: read-only timeline + can submit second opinion

Dependency rule:
  share/ depends on → patients/
  share/ must never be imported by → patients/, audit/, claims/
"""

import secrets
import uuid

from django.conf import settings
from django.db import models


# ===========================================================================
# CHOICES
# ===========================================================================

class ValidatorType(models.TextChoices):
    """
    The type of secondary challenge the anonymous doctor must pass
    before gaining access to a share link.
    """
    YEAR_OF_BIRTH = "year_of_birth", "Year of Birth"
    PIN           = "pin",           "4–6 Digit PIN"


class ShareLinkScope(models.TextChoices):
    """
    What the anonymous accessor can do via this share link.
    Fixed to timeline_with_second_opinion per product decision.
    Modelled as a choice field for future extensibility.
    """
    TIMELINE_WITH_OPINION = "timeline_with_opinion", "Read Timeline + Submit Second Opinion"


# ===========================================================================
# MODEL: SHARE LINK
# ===========================================================================

class ShareLink(models.Model):
    """
    A patient-generated secure link granting temporary anonymous access
    to their medical timeline.

    Flow:
      1. Patient creates a share link (POST /patients/{id}/share-links/)
      2. Patient receives a URL: https://uhr.app/share/<token>
      3. Patient shares URL out-of-band (WhatsApp, email, in-person QR)
      4. Anonymous doctor opens URL, submits DOB or PIN challenge
      5. System verifies challenge → creates ShareLinkSession
      6. Doctor accesses timeline for session duration
      7. Doctor may submit a second opinion (staging: pending_approval)
      8. Session expires or patient revokes

    Security:
      - token: 32 random URL-safe bytes, unique, not guessable
      - validator_hash: bcrypt of the raw DOB (YYYY) or PIN — never stored raw
      - expires_at: patient-chosen or system default (48 hours)
      - is_revoked: patient can kill the link instantly at any time

    FHIR: No direct mapping. UHR-native consent mechanism.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="share_links",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_share_links",
        help_text="The user who generated this share link (must have manage access).",
    )

    # ── Token ─────────────────────────────────────────────────────────────────
    token = models.CharField(
        max_length=64,
        unique=True,
        help_text="URL-safe random token. Never reused. Included in the share URL.",
    )

    # ── Validation challenge ──────────────────────────────────────────────────
    validator_type = models.CharField(
        max_length=20,
        choices=ValidatorType.choices,
        help_text="What the doctor must provide to pass the challenge.",
    )
    validator_hash = models.CharField(
        max_length=128,
        help_text=(
            "bcrypt hash of the raw validator (year of birth as string, or PIN). "
            "Never store the raw value."
        ),
    )

    # ── Scope ─────────────────────────────────────────────────────────────────
    scope = models.CharField(
        max_length=32,
        choices=ShareLinkScope.choices,
        default=ShareLinkScope.TIMELINE_WITH_OPINION,
    )

    # ── Expiry ────────────────────────────────────────────────────────────────
    expires_at = models.DateTimeField(
        help_text="When this share link expires. After this, verification will fail.",
    )

    # ── Revocation ────────────────────────────────────────────────────────────
    is_revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # ── Access tracking ───────────────────────────────────────────────────────
    # first_accessed_at is set on first successful verification.
    # Useful for patient visibility: "This link was first used on..."
    first_accessed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of first successful verification. Null if never used.",
    )
    access_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of successful verifications against this link.",
    )

    # ── Label ─────────────────────────────────────────────────────────────────
    label = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Optional patient-assigned label e.g. 'For Dr Ahmed at Apollo'.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "share"
        db_table  = "share_links"
        indexes = [
            models.Index(fields=["token"],      name="idx_share_link_token"),
            models.Index(fields=["patient", "is_revoked"], name="idx_share_link_patient"),
            models.Index(fields=["expires_at"], name="idx_share_link_expiry"),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"ShareLink {self.token[:8]}... patient={self.patient_id} revoked={self.is_revoked}"

    @property
    def is_active(self) -> bool:
        """True if the link is not revoked and has not expired."""
        from django.utils import timezone
        return not self.is_revoked and self.expires_at > timezone.now()

    @staticmethod
    def generate_token() -> str:
        """Generate a unique URL-safe token for a new share link."""
        return secrets.token_urlsafe(32)


# ===========================================================================
# MODEL: SHARE LINK SESSION
# ===========================================================================

class ShareLinkSession(models.Model):
    """
    A server-side session created when an anonymous doctor successfully
    passes the share link validation challenge (DOB or PIN).

    Why server-side instead of JWT:
      - Revocable at any time — patient can kill all sessions instantly
        by revoking the parent ShareLink
      - Healthcare data requires guaranteed revocability
      - Session token is looked up on every request — O(1) with the index

    Session lifecycle:
      POST /share/{token}/verify/ → creates ShareLinkSession → returns session_token
      GET  /share/{token}/timeline/ → validates session_token → serves timeline
      POST /share/{token}/second-opinion/ → validates session_token → stages submission

    Session is invalid if:
      - expires_at has passed
      - is_revoked is True
      - parent ShareLink is revoked or expired

    FHIR: No mapping. UHR-native session construct.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    share_link = models.ForeignKey(
        ShareLink,
        on_delete=models.PROTECT,
        related_name="sessions",
    )

    # Separate token from the share link token.
    # The share link token is in the URL (semi-public).
    # The session token is returned only after successful verification
    # and must be kept private by the client.
    session_token = models.CharField(
        max_length=64,
        unique=True,
        help_text="Short-lived session credential returned after verification.",
    )

    # ── Expiry ────────────────────────────────────────────────────────────────
    # Short — 2 hours default. Doctor has time to review but not indefinite access.
    expires_at = models.DateTimeField()

    # ── Revocation ────────────────────────────────────────────────────────────
    is_revoked = models.BooleanField(default=False)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # ── Network metadata ──────────────────────────────────────────────────────
    # Stored for audit purposes — not used for access control.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "share"
        db_table  = "share_link_sessions"
        indexes = [
            models.Index(fields=["session_token"], name="idx_share_session_token"),
            models.Index(
                fields=["share_link", "is_revoked"],
                name="idx_share_session_link",
            ),
        ]

    def __str__(self):
        return f"ShareLinkSession {self.session_token[:8]}... link={self.share_link_id}"

    @property
    def is_valid(self) -> bool:
        """
        True if this session is usable for timeline access.
        Checks session-level expiry and revocation.
        Parent ShareLink validity is checked separately in the service.
        """
        from django.utils import timezone
        return not self.is_revoked and self.expires_at > timezone.now()

    @staticmethod
    def generate_token() -> str:
        return secrets.token_urlsafe(32)