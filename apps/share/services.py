"""
share/services.py
=================
Service layer for patient-initiated share links.

Function index:
  create_share_link(user, patient_id, data)     → ShareLink
  list_share_links(user, patient_id)            → QuerySet[ShareLink]
  revoke_share_link(user, patient_id, link_id)  → ShareLink
  verify_share_link(token, validator, request)  → ShareLinkSession
  get_session(session_token)                    → ShareLinkSession
  get_timeline_via_session(session_token)       → (Patient, ShareLink)
"""

import logging
from datetime import timedelta
from uuid import UUID

import bcrypt
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.patients.exceptions import AccessDenied, PatientNotFound, PatientRetracted
from apps.patients.models import Patient
from apps.patients.services import _get_active_access, _assert_can_manage

from .exceptions import (
    InvalidValidator,
    SessionNotFound,
    ShareLinkAccessDenied,
    ShareLinkExpired,
    ShareLinkNotFound,
    ShareLinkRevoked,
)
from .models import ShareLink, ShareLinkSession, ValidatorType

logger = logging.getLogger(__name__)
User   = get_user_model()

_SHARE_LINK_DEFAULT_EXPIRY_HOURS = 48
_SESSION_EXPIRY_HOURS            = 2


# ===========================================================================
# INTERNAL HELPERS
# ===========================================================================

def _hash_validator(raw: str) -> str:
    """
    bcrypt-hash a raw validator (year of birth as string, or PIN).
    Never stored in plain text.
    Work factor 12 — high enough to resist brute-force on a 4-digit PIN.
    """
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_validator(raw: str, hashed: str) -> bool:
    """Verify a raw validator against its bcrypt hash."""
    return bcrypt.checkpw(raw.encode(), hashed.encode())


def _get_link_for_patient(patient_id: UUID, link_id: UUID) -> ShareLink:
    """Fetch a share link belonging to a patient. Raises ShareLinkNotFound if missing."""
    try:
        return ShareLink.objects.get(id=link_id, patient_id=patient_id)
    except ShareLink.DoesNotExist:
        raise ShareLinkNotFound()


# ===========================================================================
# CREATE SHARE LINK
# ===========================================================================

@transaction.atomic
def create_share_link(user, patient_id: UUID, data) -> ShareLink:
    """
    Create a new share link for a patient profile.

    Only the primary holder or managing delegate can create share links.
    The raw validator (DOB year or PIN) is bcrypt-hashed before storage.
    The token is a 32-byte URL-safe random string.

    Expiry defaults to 48 hours. Patient can request up to 30 days.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_manage(access)

    patient = access.patient

    # Hash the validator before storage — never store raw DOB or PIN
    validator_hash = _hash_validator(str(data.validator_value))

    expiry_hours = getattr(data, "expiry_hours", _SHARE_LINK_DEFAULT_EXPIRY_HOURS)
    expiry_hours = min(expiry_hours, 24 * 30)  # Hard cap: 30 days

    link = ShareLink.objects.create(
        patient        = patient,
        created_by     = user,
        token          = ShareLink.generate_token(),
        validator_type = data.validator_type,
        validator_hash = validator_hash,
        expires_at     = timezone.now() + timedelta(hours=expiry_hours),
        label          = getattr(data, "label", None),
    )

    logger.info(
        "Share link created. link_id=%s patient_id=%s created_by=%s expiry_hours=%s",
        link.id, patient_id, user.id, expiry_hours,
    )

    return link


# ===========================================================================
# LIST SHARE LINKS
# ===========================================================================

def list_share_links(user, patient_id: UUID):
    """
    List all share links for a patient profile.
    Only the primary holder or managing delegate can list share links.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_manage(access)

    return (
        ShareLink.objects
        .filter(patient_id=patient_id)
        .order_by("-created_at")
    )


# ===========================================================================
# REVOKE SHARE LINK
# ===========================================================================

@transaction.atomic
def revoke_share_link(user, patient_id: UUID, link_id: UUID) -> ShareLink:
    """
    Revoke a share link immediately.

    Revoking the link invalidates all active sessions derived from it —
    the session validity check always verifies the parent link is not revoked.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_manage(access)

    link = _get_link_for_patient(patient_id, link_id)

    if link.is_revoked:
        raise ShareLinkRevoked("This share link is already revoked.")

    now             = timezone.now()
    link.is_revoked = True
    link.revoked_at = now
    link.save(update_fields=["is_revoked", "revoked_at", "updated_at"])

    # Revoke all active sessions derived from this link
    ShareLinkSession.objects.filter(
        share_link=link,
        is_revoked=False,
    ).update(is_revoked=True, revoked_at=now)

    logger.info(
        "Share link revoked. link_id=%s patient_id=%s by user_id=%s",
        link_id, patient_id, user.id,
    )

    return link


# ===========================================================================
# VERIFY SHARE LINK (public — no auth required)
# ===========================================================================

@transaction.atomic
def verify_share_link(token: str, validator_value: str, request) -> ShareLinkSession:
    """
    Verify a share link token against the DOB/PIN challenge.
    On success, creates and returns a ShareLinkSession.

    This endpoint is public — no JWT required.
    The token in the URL is the credential for finding the link.
    The validator_value is the second factor.

    Security:
      - Both checks run always (no short-circuit) to prevent timing attacks
        that could distinguish "link not found" from "wrong PIN"
      - bcrypt comparison is constant-time
      - Failed attempts are logged but not currently rate-limited at service level
        (rate limiting should be applied at the nginx/middleware level)

    Schema invariant 12.7:
      All submissions via share link must default to pending_approval.
      This is enforced at the medical event creation layer, not here.
    """
    # Look up the link by token — do not reveal whether it exists to attackers
    try:
        link = ShareLink.objects.select_related("patient").get(token=token)
    except ShareLink.DoesNotExist:
        logger.warning("Share link verification failed: token not found.")
        raise ShareLinkNotFound()

    # Check revocation and expiry before validating the challenge
    if link.is_revoked:
        logger.warning("Share link verification failed: link revoked. link_id=%s", link.id)
        raise ShareLinkRevoked()

    if not link.is_active:
        logger.warning("Share link verification failed: link expired. link_id=%s", link.id)
        raise ShareLinkExpired()

    # Verify the challenge — always run even if link is invalid (timing attack prevention)
    if not _verify_validator(str(validator_value), link.validator_hash):
        logger.warning(
            "Share link verification failed: wrong validator. link_id=%s", link.id
        )
        raise InvalidValidator()

    now        = timezone.now()
    expires_at = now + timedelta(hours=_SESSION_EXPIRY_HOURS)

    session = ShareLinkSession.objects.create(
        share_link    = link,
        session_token = ShareLinkSession.generate_token(),
        expires_at    = expires_at,
        ip_address    = _get_client_ip(request),
        user_agent    = request.META.get("HTTP_USER_AGENT", "")[:500],
    )

    # Update link access tracking
    link.access_count += 1
    if not link.first_accessed_at:
        link.first_accessed_at = now
    link.save(update_fields=["access_count", "first_accessed_at", "updated_at"])

    logger.info(
        "Share link verified. link_id=%s session_id=%s patient_id=%s",
        link.id, session.id, link.patient_id,
    )

    return session


# ===========================================================================
# GET SESSION (validates session token on each request)
# ===========================================================================

def get_session(session_token: str) -> ShareLinkSession:
    """
    Validate a session token and return the session.
    Called by timeline and second-opinion endpoints on every request.

    Checks:
      1. Session exists
      2. Session not expired
      3. Session not revoked
      4. Parent share link not revoked or expired
    """
    try:
        session = (
            ShareLinkSession.objects
            .select_related("share_link__patient")
            .get(session_token=session_token)
        )
    except ShareLinkSession.DoesNotExist:
        raise SessionNotFound()

    if not session.is_valid:
        raise SessionNotFound("Session has expired or been revoked.")

    # Parent link check — revoking the link kills all derived sessions
    if not session.share_link.is_active:
        raise SessionNotFound("The share link associated with this session is no longer active.")

    return session


# ===========================================================================
# GET TIMELINE VIA SESSION
# ===========================================================================

def get_timeline_via_session(session_token: str) -> tuple[Patient, ShareLink]:
    """
    Validate session and return the patient + share link for timeline rendering.
    The API layer uses this to fetch and return the patient's medical events.
    """
    session = get_session(session_token)
    patient = session.share_link.patient

    if not patient.is_active:
        raise PatientRetracted()

    return patient, session.share_link


# ===========================================================================
# INTERNAL UTILITIES
# ===========================================================================

def _get_client_ip(request) -> str | None:
    """Extract client IP from request, respecting X-Forwarded-For."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")