"""
visits/api.py
=============
Endpoints for patient visit sessions and organisation QR generation.

Routers:
  patient_router  — patient-facing: initiate/end/list visits
  org_router      — org-facing: generate org QR code
"""

import logging
from datetime import timedelta
from uuid import UUID

import jwt
from django.conf import settings
from django.utils import timezone
from ninja import Router

from apps.users.schemas import ErrorSchema
from core.auth import JWTBearer, get_current_user
from apps.patients.exceptions import AccessDenied, PatientNotFound, PatientRetracted

from . import services
from .exceptions import (
    InvalidOrgQRToken,
    OrganisationNotFound,
    VisitAlreadyActive,
    VisitAlreadyEnded,
    VisitNotFound,
)
from .schemas import (
    EndVisitSchema,
    InitiateVisitSchema,
    OrgQRResponseSchema,
    VisitResponseSchema,
)

logger   = logging.getLogger(__name__)
jwt_auth = JWTBearer()

patient_router = Router(tags=["Visits — Patient"])
org_router     = Router(tags=["Visits — Organisation"])

_ORG_QR_EXPIRY_MINUTES = 15
_ORG_QR_TOKEN_TYPE     = "org_qr"


# ===========================================================================
# ORG QR ENDPOINT
# ===========================================================================

@org_router.get(
    "/{org_id}/qr/",
    auth=jwt_auth,
    response={
        200: dict,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Generate an organisation QR token",
    description=(
        "Generate a short-lived signed QR token for an organisation. "
        "The frontend renders this as a QR code displayed at reception. "
        "Patients scan this code to initiate a visit session. "
        "Token expires in 15 minutes — reception should auto-refresh. "
        "Caller must be a verified practitioner at this organisation "
        "or an org admin."
    ),
)
def get_org_qr(request, org_id: UUID):
    """
    Generate a signed org QR token.

    Token payload:
        {
            "type":   "org_qr",
            "org_id": "<uuid>",
            "exp":    <unix timestamp 15min from now>,
            "iat":    <unix timestamp now>
        }

    The token is signed with SECRET_KEY (HS256) and verified by
    visits.services._verify_org_qr_token() when the patient scans it.
    """
    user = get_current_user(request)

    # Verify the organisation exists
    try:
        from django.apps import apps
        Organisation = apps.get_model("integrations", "Organisation")
        org = Organisation.objects.get(pk=org_id, verified=True)
    except LookupError:
        return 404, ErrorSchema(detail="integrations app not yet configured.", status_code=404)
    except Organisation.DoesNotExist:
        return 404, ErrorSchema(detail="Organisation not found or not verified.", status_code=404)

    # Check caller is associated with this org
    # (full practitioner role check deferred to integrations app build)
    # For now: any authenticated user can generate an org QR
    # TODO: restrict to practitioners/admins at this org once integrations is built

    now        = timezone.now()
    expires_at = now + timedelta(minutes=_ORG_QR_EXPIRY_MINUTES)

    payload = {
        "type":   _ORG_QR_TOKEN_TYPE,
        "org_id": str(org_id),
        "exp":    int(expires_at.timestamp()),
        "iat":    int(now.timestamp()),
    }

    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

    return 200, {
        "token":              token,
        "org_id":             str(org_id),
        "org_name":           org.name,
        "expires_at":         expires_at.isoformat(),
        "expires_in_seconds": _ORG_QR_EXPIRY_MINUTES * 60,
        "qr_data":            f"uhr://visit/{token}",
    }


# ===========================================================================
# PATIENT VISIT ENDPOINTS
# ===========================================================================

@patient_router.post(
    "/",
    auth=jwt_auth,
    response={
        201: dict,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
    },
    summary="Initiate a visit by scanning an org QR",
    description=(
        "Patient scans an organisation's QR code to initiate a visit session. "
        "All verified practitioners at that organisation gain access to the "
        "patient's timeline for the duration of the visit (24hr default). "
        "Access is created lazily — a record is only written when a practitioner "
        "actually opens the patient's record."
    ),
)
def initiate_visit(request, patient_id: UUID, data: InitiateVisitSchema):
    user = get_current_user(request)
    try:
        visit = services.initiate_visit(user, patient_id, data.org_qr_token, data)
        return 201, _build_visit(visit)
    except (InvalidOrgQRToken, OrganisationNotFound) as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except VisitAlreadyActive as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except PatientRetracted:
        return 400, ErrorSchema(detail="Patient profile is retracted.", status_code=400)


@patient_router.post(
    "/{visit_id}/end/",
    auth=jwt_auth,
    response={
        200: dict,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="End a visit session early",
    description=(
        "Patient ends a visit session before it expires. "
        "All practitioner access derived from this visit is revoked immediately."
    ),
)
def end_visit(request, patient_id: UUID, visit_id: UUID):
    user = get_current_user(request)
    try:
        visit = services.end_visit(user, patient_id, visit_id)
        return 200, _build_visit(visit)
    except VisitNotFound:
        return 404, ErrorSchema(detail="Visit not found.", status_code=404)
    except VisitAlreadyEnded as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@patient_router.get(
    "/",
    auth=jwt_auth,
    response={
        200: list,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="List all visits for a patient",
)
def list_visits(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        visits = services.list_patient_visits(user, patient_id)
        return 200, [_build_visit(v) for v in visits]
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient not found.", status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# HELPERS
# ===========================================================================

def _build_visit(visit) -> dict:
    return {
        "id":                str(visit.id),
        "patient_id":        str(visit.patient_id),
        "organisation_id":   str(visit.organisation_id),
        "organisation_name": visit.organisation.name,
        "initiated_at":      visit.initiated_at.isoformat(),
        "expires_at":        visit.expires_at.isoformat(),
        "ended_at":          visit.ended_at.isoformat() if visit.ended_at else None,
        "is_active":         visit.is_currently_active,
        "visit_reason":      visit.visit_reason,
    }


# ===========================================================================
# PRACTITIONER-FACING VISIT ENDPOINTS
# ===========================================================================

from .exceptions import (
    FullTimelineNotApproved,
    TimelineRequestAlreadyExists,
    TimelineRequestNotFound,
    TimelineRequestNotPending,
)


@org_router.get(
    "/{visit_id}/patient-summary/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get emergency summary for a patient (Tier 1 — auto-granted)",
    description=(
        "Returns the International Patient Summary for the visiting patient. "
        "Auto-granted to all verified practitioners at the visiting organisation. "
        "Contains: blood group, allergies (HIGH criticality first), "
        "active medications, latest vitals, active conditions. "
        "No patient approval required — available immediately on visit initiation."
    ),
)
def get_patient_emergency_summary(request, visit_id: UUID):
    user = get_current_user(request)
    try:
        from practitioners.models import Practitioner
        practitioner = Practitioner.objects.get(user=user, is_verified=True)
        from . import services
        summary = services.get_visit_emergency_summary(practitioner, visit_id)
        return 200, summary
    except Practitioner.DoesNotExist:
        return 403, ErrorSchema(
            detail="Verified practitioner profile required.",
            status_code=403,
        )
    except Exception as e:
        if "DoesNotExist" in type(e).__name__:
            return 404, ErrorSchema(detail="Visit not found.", status_code=404)
        if "AccessDenied" in type(e).__name__ or "PractitionerNotAtOrg" in type(e).__name__:
            return 403, ErrorSchema(detail=str(e), status_code=403)
        raise


@org_router.get(
    "/{visit_id}/timeline/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get full patient timeline (Tier 2 — requires patient approval)",
    description=(
        "Returns the patient's complete visible timeline. "
        "Requires PatientVisit.access_scope = full_timeline, "
        "which is set when the patient approves a VisitTimelineRequest. "
        "Returns 403 with code=full_timeline_not_approved if not yet approved."
    ),
)
def get_visit_full_timeline(request, visit_id: UUID):
    user = get_current_user(request)
    try:
        from practitioners.models import Practitioner
        practitioner = Practitioner.objects.get(user=user, is_verified=True)
        from . import services
        timeline = services.get_visit_full_timeline(practitioner, visit_id)
        return 200, timeline
    except FullTimelineNotApproved as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except Practitioner.DoesNotExist:
        return 403, ErrorSchema(detail="Verified practitioner profile required.", status_code=403)
    except Exception as e:
        if "DoesNotExist" in type(e).__name__:
            return 404, ErrorSchema(detail="Visit not found.", status_code=404)
        raise


@org_router.post(
    "/{visit_id}/request-full-timeline/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 409: ErrorSchema},
    summary="Request full timeline access from patient",
    description=(
        "Practitioner requests patient approval for full timeline access. "
        "Patient receives notification and can approve or deny. "
        "One pending request per visit allowed."
    ),
)
def request_full_timeline(request, visit_id: UUID, reason: str):
    user = get_current_user(request)
    try:
        from practitioners.models import Practitioner
        from django.core.exceptions import ValidationError
        practitioner = Practitioner.objects.get(user=user, is_verified=True)
        from . import services
        req = services.request_full_timeline(practitioner, visit_id, reason)
        return 201, {
            "id":             str(req.id),
            "visit_id":       str(req.visit_id),
            "status":         req.status,
            "reason":         req.reason,
            "requested_by":   req.requested_by.full_name,
            "created_at":     req.created_at.isoformat(),
        }
    except TimelineRequestAlreadyExists as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except ValidationError as e:
        return 400, ErrorSchema(detail=str(e), status_code=400)
    except Practitioner.DoesNotExist:
        return 403, ErrorSchema(detail="Verified practitioner profile required.", status_code=403)
    except Exception as e:
        if "AccessDenied" in type(e).__name__:
            return 403, ErrorSchema(detail=str(e), status_code=403)
        raise


# Patient-facing: review and respond to timeline requests
@patient_router.get(
    "/timeline-requests/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema},
    summary="List pending timeline requests from organisations",
    description=(
        "Returns pending requests from practitioners asking for full timeline access. "
        "Patient approves or denies each request."
    ),
)
def list_timeline_requests(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        from . import services
        reqs = services.list_pending_timeline_requests(user, patient_id)
        return 200, [
            {
                "id":              str(r.id),
                "visit_id":        str(r.visit_id),
                "organisation":    r.visit.organisation.name,
                "practitioner":    r.requested_by.full_name,
                "reason":          r.reason,
                "status":          r.status,
                "created_at":      r.created_at.isoformat(),
            }
            for r in reqs
        ]
    except Exception as e:
        if "AccessDenied" in type(e).__name__:
            return 403, ErrorSchema(detail=str(e), status_code=403)
        raise


@patient_router.post(
    "/timeline-requests/{request_id}/respond/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema, 409: ErrorSchema},
    summary="Approve or deny a full timeline request",
)
def respond_to_timeline_request(
    request,
    patient_id: UUID,
    request_id: UUID,
    approve: bool,
    denial_reason: str = None,
):
    user = get_current_user(request)
    try:
        from . import services
        req = services.respond_to_timeline_request(
            user, patient_id, request_id, approve, denial_reason
        )
        return 200, {
            "id":     str(req.id),
            "status": req.status,
            "message": (
                "Full timeline access granted to the visiting practitioner."
                if approve else
                "Request denied."
            ),
        }
    except TimelineRequestNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except TimelineRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except Exception as e:
        if "AccessDenied" in type(e).__name__:
            return 403, ErrorSchema(detail=str(e), status_code=403)
        raise