"""
share/api.py
============
Endpoints for share link management and anonymous timeline access.

Two router groups:
  authenticated_router  — requires JWT (patient manages their share links)
  public_router         — no JWT (anonymous doctor verifies + accesses timeline)

The public router uses the share link token + session token as credentials,
not a JWT. These are registered separately on the main API.
"""

import logging
from uuid import UUID

from django.conf import settings
from ninja import Router

from apps.users.schemas import ErrorSchema
from core.auth import JWTBearer, get_current_user
from apps.patients.exceptions import AccessDenied, PatientNotFound, PatientRetracted

from . import services
from .exceptions import (
    InvalidValidator,
    SessionNotFound,
    ShareLinkAccessDenied,
    ShareLinkExpired,
    ShareLinkNotFound,
    ShareLinkRevoked,
)
from .schemas import (
    CreateShareLinkSchema,
    SecondOpinionSchema,
    SessionResponseSchema,
    ShareLinkResponseSchema,
    VerifyShareLinkSchema,
)

logger    = logging.getLogger(__name__)
jwt_auth  = JWTBearer()

def _build_event_summary(event) -> str:
    """Minimal summary for share link timeline — no sensitive detail."""
    ext = event.typed_extension
    if not ext:
        return event.event_type
    type_map = {
        "visit":          lambda e: e.reason or "Visit",
        "observation":    lambda e: f"{e.observation_name}: {e.value_quantity or e.value_string or ''} {e.value_unit or ''}".strip(),
        "condition":      lambda e: e.condition_name,
        "medication":     lambda e: f"{e.medication_name} ({e.dosage or '—'})",
        "procedure":      lambda e: e.procedure_name,
        "document":       lambda e: f"Document: {e.document_type}",
        "second_opinion": lambda e: f"Second opinion by {e.doctor_name}",
        "allergy":        lambda e: f"Allergy: {e.substance_name}",
        "vaccination":    lambda e: f"Vaccination: {e.vaccine_name}",
        "consultation":   lambda e: f"Consultation: {e.department}",
        "vital_signs":    lambda e: "Vital signs recorded",
    }
    fn = type_map.get(event.event_type)
    try:
        return fn(ext) if fn else event.event_type
    except Exception:
        return event.event_type


# Authenticated router — patient manages share links
# Mounted at /api/patients/{patient_id}/share-links/ in config/api.py
authenticated_router = Router(tags=["Share Links"])

# Public router — anonymous access via token
# Mounted at /api/share/ in config/api.py
public_router = Router(tags=["Share — Public Access"])


# ===========================================================================
# HELPERS
# ===========================================================================

def _build_share_link(link) -> dict:
    base_url = getattr(settings, "SHARE_LINK_BASE_URL", "https://uhr.app/share")
    return {
        "id":                link.id,
        "patient_id":        link.patient_id,
        "token":             link.token,
        "validator_type":    link.validator_type,
        "scope":             link.scope,
        "expires_at":        link.expires_at,
        "is_revoked":        link.is_revoked,
        "first_accessed_at": link.first_accessed_at,
        "access_count":      link.access_count,
        "label":             link.label,
        "created_at":        link.created_at,
        "share_url":         f"{base_url}/{link.token}",
    }


# ===========================================================================
# AUTHENTICATED ENDPOINTS (patient manages their share links)
# ===========================================================================

@authenticated_router.post(
    "/",
    auth=jwt_auth,
    response={
        201: ShareLinkResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Create a share link",
    description=(
        "Generate a secure share link for this patient's timeline. "
        "The link includes a validation challenge (year of birth or PIN) "
        "that the anonymous doctor must pass before gaining access. "
        "Returns the full URL to share out-of-band."
    ),
)
def create_share_link(request, patient_id: UUID, data: CreateShareLinkSchema):
    user = get_current_user(request)
    try:
        link = services.create_share_link(user, patient_id, data)
        return 201, _build_share_link(link)
    except (PatientNotFound, AccessDenied) as e:
        status = 404 if isinstance(e, PatientNotFound) else 403
        return status, ErrorSchema(detail=e.message, status_code=status)
    except Exception as e:
        logger.exception("create_share_link failed. patient_id=%s", patient_id)
        return 400, ErrorSchema(detail=str(e), status_code=400)


@authenticated_router.get(
    "/",
    auth=jwt_auth,
    response={
        200: list[ShareLinkResponseSchema],
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="List share links for a patient",
)
def list_share_links(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        links = services.list_share_links(user, patient_id)
        return 200, [_build_share_link(l) for l in links]
    except PatientNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@authenticated_router.delete(
    "/{link_id}/",
    auth=jwt_auth,
    response={
        200: ShareLinkResponseSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Revoke a share link",
    description=(
        "Immediately revoke a share link. "
        "All active sessions derived from this link are also revoked. "
        "The anonymous doctor will lose access immediately."
    ),
)
def revoke_share_link(request, patient_id: UUID, link_id: UUID):
    user = get_current_user(request)
    try:
        link = services.revoke_share_link(user, patient_id, link_id)
        return 200, _build_share_link(link)
    except ShareLinkNotFound:
        return 404, ErrorSchema(detail="Share link not found.", status_code=404)
    except ShareLinkRevoked as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except (PatientNotFound, AccessDenied) as e:
        status = 404 if isinstance(e, PatientNotFound) else 403
        return status, ErrorSchema(detail=e.message, status_code=status)


# ===========================================================================
# PUBLIC ENDPOINTS (anonymous doctor — no JWT)
# ===========================================================================

@public_router.post(
    "/{token}/verify/",
    auth=None,
    response={
        200: SessionResponseSchema,
        400: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Verify a share link challenge",
    description=(
        "Submit the year of birth or PIN challenge for a share link. "
        "On success, returns a session_token valid for 2 hours. "
        "Use this session_token in subsequent requests to access the timeline. "
        "No authentication required — the share link token is the credential."
    ),
)
def verify_share_link(request, token: str, data: VerifyShareLinkSchema):
    try:
        session = services.verify_share_link(token, data.validator_value, request)
        return 200, {
            "session_token": session.session_token,
            "expires_at":    session.expires_at,
            "patient_id":    session.share_link.patient_id,
            "scope":         session.share_link.scope,
        }
    except ShareLinkNotFound:
        # Return 404 for both "not found" and "wrong PIN" to prevent
        # attackers distinguishing between the two states
        return 404, ErrorSchema(
            detail="Share link not found or challenge incorrect.",
            status_code=404,
        )
    except InvalidValidator:
        return 404, ErrorSchema(
            detail="Share link not found or challenge incorrect.",
            status_code=404,
        )
    except ShareLinkRevoked:
        return 410, ErrorSchema(
            detail="This share link has been revoked.",
            status_code=410,
        )
    except ShareLinkExpired:
        return 410, ErrorSchema(
            detail="This share link has expired.",
            status_code=410,
        )


@public_router.get(
    "/{token}/timeline/",
    auth=None,
    response={
        200: dict,
        401: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Read patient timeline via share link session",
    description=(
        "Access the patient's medical timeline using a session_token obtained "
        "from the verify endpoint. Pass session_token as a query parameter. "
        "Returns read-only timeline data."
    ),
)
def get_timeline(request, token: str, session_token: str):
    try:
        patient, link = services.get_timeline_via_session(session_token)
    except SessionNotFound as e:
        return 401, ErrorSchema(detail=e.message, status_code=401)
    except PatientRetracted:
        return 410, ErrorSchema(
            detail="This patient profile is no longer active.",
            status_code=410,
        )

    # Verify the token in the URL matches the session's share link
    # Prevents a valid session from being used against a different link URL
    if link.token != token:
        return 401, ErrorSchema(
            detail="Session token does not match this share link.",
            status_code=401,
        )

    from medical_events.services import get_timeline
    from medical_events.models import VisibilityStatus

    # Share link sessions get read-only access to visible events only.
    # Hidden and pending_approval events are never exposed via share links
    # regardless of what the patient has approved — share links are for
    # external parties who should only see what the patient has made visible.
    qs = get_timeline(
        user        = link.created_by,  # use creator's access context
        patient_id  = patient.id,
        filters     = {
            "include_hidden":  False,
            "include_pending": False,
        }
    )

    events = [
        {
            "id":                 str(e.id),
            "event_type":         e.event_type,
            "clinical_timestamp": e.clinical_timestamp.isoformat(),
            "verification_level": e.verification_level,
            "source_type":        e.source_type,
            "summary":            _build_event_summary(e),
        }
        for e in qs
    ]

    return 200, {
        "patient": {
            "id":          str(patient.id),
            "full_name":   patient.full_name,
            "birth_date":  str(patient.birth_date),
            "gender":      patient.gender,
            "blood_group": patient.blood_group,
        },
        "scope":  link.scope,
        "events": events,
    }


@public_router.post(
    "/{token}/second-opinion/",
    auth=None,
    response={
        201: dict,
        400: ErrorSchema,
        401: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Submit a second opinion via share link session",
    description=(
        "Submit a second opinion note for the patient's timeline. "
        "Requires a valid session_token from the verify endpoint. "
        "Submitted opinions default to visibility_status=pending_approval "
        "per schema invariant 12.7 — the patient must approve before it "
        "appears in the main timeline."
    ),
)
def submit_second_opinion(
    request,
    token: str,
    session_token: str,
    data: SecondOpinionSchema,
):
    try:
        patient, link = services.get_timeline_via_session(session_token)
    except SessionNotFound as e:
        return 401, ErrorSchema(detail=e.message, status_code=401)
    except PatientRetracted:
        return 410, ErrorSchema(
            detail="This patient profile is no longer active.",
            status_code=410,
        )

    if link.token != token:
        return 401, ErrorSchema(
            detail="Session token does not match this share link.",
            status_code=401,
        )

    from medical_events.services import create_event
    from medical_events.models import EventType, SourceType

    source_context = {
        "source_type":    SourceType.PATIENT,  # anonymous — no practitioner account
        "via_share_link": True,                # triggers pending_approval
    }

    try:
        event = create_event(
            user           = link.created_by,   # use patient's own user as the actor
            patient_id     = patient.id,
            event_type     = EventType.SECOND_OPINION,
            data           = data,
            source_context = source_context,
        )
    except Exception as e:
        logger.error(
            "Failed to create second opinion event. link_id=%s error=%s",
            link.id, str(e),
        )
        return 400, ErrorSchema(
            detail="Failed to record second opinion. Please try again.",
            status_code=400,
        )

    logger.info(
        "Second opinion submitted via share link. "
        "event_id=%s patient_id=%s link_id=%s doctor=%s",
        event.id, patient.id, link.id, data.doctor_name,
    )

    return 201, {
        "event_id":          str(event.id),
        "status":            "staged",
        "visibility_status": "pending_approval",
        "message": (
            f"Second opinion from {data.doctor_name} has been submitted "
            "and is pending patient approval before appearing on the timeline."
        ),
    }