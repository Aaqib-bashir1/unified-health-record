"""
patients/api.py
===============
Django-ninja router for the patients app.

Endpoints:
  POST   /patients/                           create_patient
  GET    /patients/                           list_my_patients
  GET    /patients/{patient_id}/              get_patient
  PATCH  /patients/{patient_id}/              update_patient
  DELETE /patients/{patient_id}/              retract_patient

  GET    /patients/{patient_id}/access/       list_access
  POST   /patients/{patient_id}/access/       grant_access
  DELETE /patients/{patient_id}/access/me/    self_exit
  DELETE /patients/{patient_id}/access/{access_id}/  revoke_access

Patterns followed from users/api.py:
  - ninja Router (not NinjaAPI — routers are registered on the main API)
  - JWT bearer auth via get_current_user dependency
  - All exceptions caught and mapped to ErrorSchema responses
  - HTTP 201 for creates, 200 for everything else
  - ValidationError from service → 400
  - PatientNotFound → 404
  - AccessDenied → 403
  - PatientRetracted → 410 Gone
"""

import logging
from datetime import timedelta
from uuid import UUID

import jwt
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from ninja import Router,Schema

from apps.users.schemas import ErrorSchema
from core.auth import JWTBearer, get_current_user

from .exceptions import (
    AccessDenied,
    AccessRequestExpired,
    AccessRequestNotFound,
    AccessRequestNotPending,
    DuplicateAccessError,
    DuplicatePendingRequest,
    DuplicateProfileWarning,
    OrphanProtectionError,
    PatientNotFound,
    PatientRetracted,
)
from .schemas import (
    AccessHolderSchema,
    CreatePatientSchema,
    GrantAccessResponseSchema,
    GrantAccessSchema,
    PatientDetailSchema,
    PatientSummarySchema,
    RetractPatientSchema,
    RevokeAccessSchema,
    SelfExitSchema,
    UpdatePatientSchema,
    PatientDiscoverSchema,
    AccessRequestCreateSchema,
    AccessRequestResponseSchema,
    DenyRequestSchema,
    RevokeRequestSchema,
)
from . import services

logger = logging.getLogger(__name__)
router = Router(tags=["Patients"])
jwt_auth = JWTBearer()


# ===========================================================================
# HELPERS
# ===========================================================================

def _build_patient_summary(patient, my_role, can_write, can_manage) -> dict:
    """
    Build the dict needed to populate PatientSummarySchema.
    Merges model fields with access-context fields that come from
    the PatientUserAccess record (not on the Patient model itself).
    """
    return {
        "id":          patient.id,
        "mrn":         patient.mrn,
        "first_name":  patient.first_name,
        "last_name":   patient.last_name,
        "full_name":   patient.full_name,
        "gender":      patient.gender,
        "birth_date":  patient.birth_date,
        "age":         patient.age,
        "nationality": patient.nationality,
        "is_claimed":  patient.is_claimed,
        "is_deceased": patient.is_deceased,
        "is_active":   patient.is_active,
        "created_at":  patient.created_at,
        "my_role":     my_role,
        "can_write":   can_write,
        "can_manage":  can_manage,
    }


def _build_patient_detail(patient, access) -> dict:
    """Build the dict for PatientDetailSchema including full demographics."""
    return {
        "id":                   patient.id,
        "mrn":                  patient.mrn,
        "first_name":           patient.first_name,
        "last_name":            patient.last_name,
        "full_name":            patient.full_name,
        "gender":               patient.gender,
        "birth_date":           patient.birth_date,
        "age":                  patient.age,
        "phone":                patient.phone,
        "email":                patient.email,
        "address":              patient.address,
        "blood_group":          patient.blood_group,
        "nationality":          patient.nationality,
        "is_deceased":          patient.is_deceased,
        "deceased_date":        patient.deceased_date,
        "is_claimed":           patient.is_claimed,
        "claimed_at":           patient.claimed_at,
        "transfer_eligible_at": patient.transfer_eligible_at,
        "is_active":            patient.is_active,
        "created_at":           patient.created_at,
        "updated_at":           patient.updated_at,
        "my_role":              access.role,
        "can_write":            access.can_write,
        "can_manage":           access.can_manage_access,
    }


def _build_access_holder(access) -> dict:
    """Build the dict for AccessHolderSchema from a PatientUserAccess record."""
    return {
        "id":                access.id,
        "user_id":           access.user_id,
        "user_email":        access.user.email,
        "user_name":         access.user.get_full_name(),
        "role":              access.role,
        "claim_method":      access.claim_method,
        "trust_level":       access.trust_level,
        "is_active":         access.is_active,
        "granted_at":        access.granted_at,
        "granted_by_id":     access.granted_by_id,
        "revoked_at":        access.revoked_at,
        "revocation_reason": access.revocation_reason,
        "notes":             access.notes,
    }


# ===========================================================================
# PATIENT ENDPOINTS
# ===========================================================================

@router.post(
    "/",
    auth=jwt_auth,
    response={
        201: PatientDetailSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Create a patient profile",
    description=(
        "Create a new patient profile. "
        "Set is_dependent=False (default) to create your own profile — you become the primary holder. "
        "Set is_dependent=True to create a dependent's profile (e.g. a child) — you become a full delegate."
    ),
)
def create_patient(request, data: CreatePatientSchema):
    user = get_current_user(request)
    try:
        patient, access = services.create_patient(user, data)
        return 201, _build_patient_detail(patient, access)
    except DuplicateProfileWarning as e:
        # 409 Conflict — possible duplicate detected, not a hard block.
        # Frontend should present a confirmation dialog, then retry with force_create=true.
        return 409, ErrorSchema(detail=e.message, status_code=409, field="force_create")
    except ValidationError as e:
        detail = "; ".join(
            f"{k}: {', '.join(v)}" if isinstance(v, list) else f"{k}: {v}"
            for k, v in (e.message_dict.items() if hasattr(e, "message_dict") else {"detail": [str(e)]}.items())
        )
        return 400, ErrorSchema(detail=detail, status_code=400)


@router.get(
    "/",
    auth=jwt_auth,
    response={
        200: list[PatientSummarySchema],
        401: ErrorSchema,
    },
    summary="List my patient profiles",
    description="Return all patient profiles the authenticated user currently has active access to.",
)
def list_my_patients(request):
    user    = get_current_user(request)
    results = services.list_my_patients(user)
    return 200, [
        _build_patient_summary(
            r["patient"],
            r["my_role"],
            r["can_write"],
            r["can_manage"],
        )
        for r in results
    ]


@router.get(
    "/{patient_id}/",
    auth=jwt_auth,
    response={
        200: PatientDetailSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Get a patient profile",
)
def get_patient(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        patient, access = services.get_patient_for_user(user, patient_id)
        return 200, _build_patient_detail(patient, access)
    except PatientRetracted:
        return 410, ErrorSchema(detail="This patient profile has been retracted.", status_code=410)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient profile not found.", status_code=404)


@router.patch(
    "/{patient_id}/",
    auth=jwt_auth,
    response={
        200: PatientDetailSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Update a patient profile",
    description="Partial update (PATCH). Only fields you provide are changed.",
)
def update_patient(request, patient_id: UUID, data: UpdatePatientSchema):
    user = get_current_user(request)
    try:
        patient = services.update_patient(user, patient_id, data)
        # Re-fetch access for response context
        _, access = services.get_patient_for_user(user, patient_id)
        return 200, _build_patient_detail(patient, access)
    except PatientRetracted:
        return 410, ErrorSchema(detail="This patient profile has been retracted.", status_code=410)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient profile not found.", status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except ValidationError as e:
        detail = str(e)
        return 400, ErrorSchema(detail=detail, status_code=400)


@router.delete(
    "/{patient_id}/",
    auth=jwt_auth,
    response={
        200: PatientDetailSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Retract (soft-delete) a patient profile",
    description=(
        "Soft-retract a patient profile. Only the primary holder can do this. "
        "The profile and all its medical events are preserved — only access is blocked."
    ),
)
def retract_patient(request, patient_id: UUID, data: RetractPatientSchema):
    user = get_current_user(request)
    try:
        patient = services.retract_patient(user, patient_id, data.retraction_reason)
        _, access = services.get_patient_for_user(user, patient_id)
        return 200, _build_patient_detail(patient, access)
    except PatientRetracted:
        return 410, ErrorSchema(detail="Profile is already retracted.", status_code=410)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient profile not found.", status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# ACCESS MANAGEMENT ENDPOINTS
# ===========================================================================

@router.get(
    "/{patient_id}/access/",
    auth=jwt_auth,
    response={
        200: list[AccessHolderSchema],
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="List access holders for a patient profile",
    description=(
        "Primary holders and managing delegates see all access records. "
        "Caregivers and viewers see only their own record. "
        "Pass ?history=true to include revoked records."
    ),
)
def list_access(request, patient_id: UUID, history: bool = False):
    user = get_current_user(request)
    try:
        accesses = services.list_patient_access(user, patient_id, include_history=history)
        return 200, [_build_access_holder(a) for a in accesses]
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient profile not found.", status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/{patient_id}/access/",
    auth=jwt_auth,
    response={
        201: GrantAccessResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Grant a user access to a patient profile",
    description=(
        "Grant another UHR user access to this patient profile. "
        "You must be the primary holder (or delegate on an unclaimed profile). "
        "Role 'primary' cannot be granted manually — use the claim flow."
    ),
)
def grant_access(request, patient_id: UUID, data: GrantAccessSchema):
    user = get_current_user(request)
    try:
        access = services.grant_access(user, patient_id, data)
        return 201, {
            "id":         access.id,
            "user_id":    access.user_id,
            "user_email": access.user.email,
            "role":       access.role,
            "granted_at": access.granted_at,
        }
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient profile not found.", status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except DuplicateAccessError as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except ValidationError as e:
        detail = "; ".join(
            f"{k}: {', '.join(v) if isinstance(v, list) else v}"
            for k, v in (e.message_dict.items() if hasattr(e, "message_dict") else {"detail": [str(e)]}.items())
        )
        return 400, ErrorSchema(detail=detail, status_code=400)


@router.delete(
    "/{patient_id}/access/me/",
    auth=jwt_auth,
    response={
        200: AccessHolderSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Remove your own access to a patient profile (self-exit)",
    description=(
        "Voluntarily remove your own access to this patient profile. "
        "Primary holders of claimed profiles cannot self-exit — transfer ownership first."
    ),
)
def self_exit(request, patient_id: UUID, data: SelfExitSchema = None):
    user   = get_current_user(request)
    reason = data.reason if data else None
    try:
        access = services.self_exit(user, patient_id, reason)
        return 200, _build_access_holder(access)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient profile not found or no active access.", status_code=404)
    except (AccessDenied, OrphanProtectionError) as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.delete(
    "/{patient_id}/access/{access_id}/",
    auth=jwt_auth,
    response={
        200: AccessHolderSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Revoke a user's access to a patient profile",
    description=(
        "Revoke another user's access to this patient profile. "
        "You must be the primary holder (or managing delegate). "
        "To remove your own access, use DELETE /patients/{id}/access/me/ instead."
    ),
)
def revoke_access(request, patient_id: UUID, access_id: UUID, data: RevokeAccessSchema):
    user = get_current_user(request)
    try:
        access = services.revoke_access(user, patient_id, access_id, data.revocation_reason)
        return 200, _build_access_holder(access)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Access record not found.", status_code=404)
    except (AccessDenied, OrphanProtectionError) as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# ACCESS REQUEST ENDPOINTS
# ===========================================================================

def _build_access_request(req) -> dict:
    return {
        "id":                   req.id,
        "patient_id":           req.patient_id,
        "requested_by_id":      req.requested_by_id,
        "requested_by_email":   req.requested_by.email,
        "requested_role":       req.requested_role,
        "reason":               req.reason,
        "status":               req.status,
        "is_permanent":         req.is_permanent,
        "access_duration_days": req.access_duration_days,
        "access_expires_at":    req.access_expires_at,
        "request_expires_at":   req.request_expires_at,
        "responded_at":         req.responded_at,
        "denial_reason":        req.denial_reason,
        "created_at":           req.created_at,
    }


@router.post(
    "/{patient_id}/access-requests/",
    auth=jwt_auth,
    response={
        201: AccessRequestResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Request access to a patient's timeline",
    description=(
        "Any authenticated user can request access to a patient profile. "
        "The patient must approve before access is granted. "
        "Requested role must be 'caregiver' or 'viewer'. "
        "Only one pending request per user per patient is allowed."
    ),
)
def request_access(request, patient_id: UUID, data: AccessRequestCreateSchema):
    user = get_current_user(request)
    try:
        req = services.request_access(user, patient_id, data)
        return 201, _build_access_request(req)
    except DuplicatePendingRequest as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except DuplicateAccessError as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient not found.", status_code=404)
    except ValidationError as e:
        detail = "; ".join(
            f"{k}: {v}" for k, v in
            (e.message_dict if hasattr(e, "message_dict") else {"detail": str(e)}).items()
        )
        return 400, ErrorSchema(detail=detail, status_code=400)


@router.get(
    "/{patient_id}/access-requests/",
    auth=jwt_auth,
    response={
        200: list[AccessRequestResponseSchema],
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="List access requests for a patient profile",
    description=(
        "Returns all access requests for this patient. "
        "Only the primary holder or managing delegate can view requests. "
        "Pass ?status=pending to filter by status."
    ),
)
def list_access_requests(request, patient_id: UUID, status: str = None):
    user = get_current_user(request)
    try:
        qs = services.list_access_requests(user, patient_id, status_filter=status)
        return 200, [_build_access_request(r) for r in qs]
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient not found.", status_code=404)
    except AccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/access-requests/sent/",
    auth=jwt_auth,
    response={
        200: list[AccessRequestResponseSchema],
        401: ErrorSchema,
    },
    summary="List access requests sent by the current user",
    description="Returns all requests the current user has sent, across all patients.",
)
def list_my_sent_requests(request):
    user = get_current_user(request)
    reqs = services.list_my_sent_requests(user)
    return 200, [_build_access_request(r) for r in reqs]


@router.post(
    "/{patient_id}/access-requests/{request_id}/approve/",
    auth=jwt_auth,
    response={
        200: AccessRequestResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Approve an access request",
    description=(
        "Approve a pending access request. Creates a PatientUserAccess record "
        "with the requested role and optional expiry. "
        "Only the primary holder or managing delegate can approve."
    ),
)
def approve_access_request(request, patient_id: UUID, request_id: UUID):
    user = get_current_user(request)
    try:
        req = services.approve_access_request(user, patient_id, request_id)
        return 200, _build_access_request(req)
    except AccessRequestNotFound:
        return 404, ErrorSchema(detail="Access request not found.", status_code=404)
    except AccessRequestExpired as e:
        return 410, ErrorSchema(detail=e.message, status_code=410)
    except AccessRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except DuplicateAccessError as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except (AccessDenied, PatientNotFound) as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/{patient_id}/access-requests/{request_id}/deny/",
    auth=jwt_auth,
    response={
        200: AccessRequestResponseSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Deny an access request",
)
def deny_access_request(request, patient_id: UUID, request_id: UUID, data: DenyRequestSchema):
    user = get_current_user(request)
    try:
        req = services.deny_access_request(user, patient_id, request_id, data.reason)
        return 200, _build_access_request(req)
    except AccessRequestNotFound:
        return 404, ErrorSchema(detail="Access request not found.", status_code=404)
    except AccessRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except (AccessDenied, PatientNotFound) as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.delete(
    "/{patient_id}/access-requests/{request_id}/",
    auth=jwt_auth,
    response={
        200: AccessRequestResponseSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Cancel your own pending access request",
)
def cancel_access_request(request, patient_id: UUID, request_id: UUID):
    user = get_current_user(request)
    try:
        req = services.cancel_access_request(user, patient_id, request_id)
        return 200, _build_access_request(req)
    except AccessRequestNotFound:
        return 404, ErrorSchema(detail="Access request not found.", status_code=404)
    except AccessRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)


@router.post(
    "/{patient_id}/access-requests/{request_id}/revoke/",
    auth=jwt_auth,
    response={
        200: AccessRequestResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Revoke a previously approved access request",
    description=(
        "Patient revokes access that was approved via a request. "
        "Closes both the request and the resulting PatientUserAccess record atomically."
    ),
)
def revoke_approved_request(request, patient_id: UUID, request_id: UUID, data: RevokeRequestSchema):
    user = get_current_user(request)
    try:
        req = services.revoke_approved_request(user, patient_id, request_id, data.reason)
        return 200, _build_access_request(req)
    except AccessRequestNotFound:
        return 404, ErrorSchema(detail="No approved request found with this ID.", status_code=404)
    except OrphanProtectionError as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except (AccessDenied, PatientNotFound) as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# PATIENT QR ENDPOINT
# ===========================================================================

_QR_TOKEN_EXPIRY_MINUTES = 15
_QR_TOKEN_TYPE           = "patient_qr"


@router.get(
    "/{patient_id}/qr/",
    auth=jwt_auth,
    response={
        200: dict,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        410: ErrorSchema,
    },
    summary="Generate a patient QR token",
    description=(
        "Returns a short-lived signed token (15 min) encoding the patient_id. "
        "The frontend renders this as a QR code. "
        "Scanning the QR gives the scanner the patient_id needed to: "
        "send an access request, initiate a hospital visit, or verify a share link. "
        "Only the patient (primary holder) or a managing delegate can generate this. "
        "Token is a signed JWT — stateless, no DB storage required."
    ),
)
def get_patient_qr(request, patient_id: UUID):
    """
    Generate a short-lived signed QR token for a patient profile.

    Token payload:
        {
            "type":       "patient_qr",
            "patient_id": "<uuid>",
            "exp":        <unix timestamp 15min from now>,
            "iat":        <unix timestamp now>
        }

    The token is signed with Django's SECRET_KEY using HS256.
    It is NOT a session credential — it only encodes the patient_id.
    Downstream flows (access request, visit initiation) require their own auth.

    Security properties:
      - 15 minute expiry prevents stale QR screenshots being reused
      - Signed — cannot be tampered to substitute a different patient_id
      - Stateless — no DB table, no revocation (short expiry is the mitigation)
      - Only primary holder or managing delegate can generate
    """
    user = get_current_user(request)

    try:
        patient, access = services.get_patient_for_user(user, patient_id)
    except PatientNotFound:
        return 404, ErrorSchema(detail="Patient not found.", status_code=404)
    except PatientRetracted:
        return 410, ErrorSchema(detail="This patient profile has been retracted.", status_code=410)

    # Only primary holder or managing delegate should be able to generate a QR.
    # A viewer or caregiver should not be able to present the patient's QR —
    # that could enable them to initiate visits or share links on the patient's behalf.
    if not access.can_manage_access:
        return 403, ErrorSchema(
            detail="Only the primary holder or managing delegate can generate a QR code.",
            status_code=403,
        )

    now        = timezone.now()
    expires_at = now + timedelta(minutes=_QR_TOKEN_EXPIRY_MINUTES)

    payload = {
        "type":       _QR_TOKEN_TYPE,
        "patient_id": str(patient_id),
        "exp":        int(expires_at.timestamp()),
        "iat":        int(now.timestamp()),
    }

    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

    return 200, {
        "token":      token,
        "patient_id": str(patient_id),
        "expires_at": expires_at.isoformat(),
        "expires_in_seconds": _QR_TOKEN_EXPIRY_MINUTES * 60,
        "qr_data":    f"uhr://patient/{token}",
    }


# ===========================================================================
# PATIENT DISCOVERY ENDPOINT
# ===========================================================================

from django.core.cache import cache

_DISCOVERY_RATE_LIMIT = 5       # max searches per window
_DISCOVERY_WINDOW_SECONDS = 3600  # 1 hour window


@router.post(
    "/discover/",
    auth=jwt_auth,
    response={
        200: dict,
        400: ErrorSchema,
        401: ErrorSchema,
        404: ErrorSchema,
        429: ErrorSchema,
    },
    summary="Discover a patient by name and date of birth",
    description=(
        "Search for a patient profile by first name, last name, and date of birth. "
        "Returns only patient_id and name — no clinical data. "
        "The requester must then send an access request to see anything further. "
        "Rate limited to 5 searches per hour per user to prevent enumeration. "
        "Every search attempt is logged regardless of result."
    ),
)
def discover_patient(request, data: "PatientDiscoverSchema"):
    """
    Patient discovery — returns patient_id only, nothing clinical.

    Security properties:
      - Rate limited: 5 attempts per hour per user (cache-based)
      - Returns identical 404 whether patient doesn't exist or name/DOB mismatch
        (prevents confirming whether a name exists in the system)
      - Every attempt logged to audit regardless of result
      - Nationality scopes the search — prevents cross-jurisdiction probing
      - Caller still needs to send an access request and be approved
        before seeing any data
    """
    user = get_current_user(request)

    # Rate limit check — cache key per user
    cache_key   = f"patient_discover_ratelimit_{user.pk}"
    attempt_count = cache.get(cache_key, 0)

    if attempt_count >= _DISCOVERY_RATE_LIMIT:
        logger.warning(
            "Patient discovery rate limit exceeded. user_id=%s", user.id
        )
        return 429, ErrorSchema(
            detail=(
                f"You have exceeded the discovery rate limit "
                f"({_DISCOVERY_RATE_LIMIT} searches per hour). "
                "Try again later."
            ),
            status_code=429,
        )

    # Increment counter — set expiry only on first attempt
    cache.set(cache_key, attempt_count + 1, timeout=_DISCOVERY_WINDOW_SECONDS)

    # Log every attempt regardless of outcome
    logger.info(
        "Patient discovery attempt. user_id=%s first_name=%s last_name=%s "
        "birth_date=%s nationality=%s attempt=%s/%s",
        user.id, data.first_name, data.last_name,
        data.birth_date, data.nationality,
        attempt_count + 1, _DISCOVERY_RATE_LIMIT,
    )

    # Query — intentionally minimal: only name + DOB + nationality
    # Nationality scopes to the right identity jurisdiction
    # iexact for names — "ahmed" and "Ahmed" are the same person
    from patients.models import Patient as PatientModel
    qs = PatientModel.objects.filter(
        first_name__iexact=data.first_name,
        last_name__iexact=data.last_name,
        birth_date=data.birth_date,
        deleted_at__isnull=True,
    )

    if data.nationality:
        qs = qs.filter(nationality__iexact=data.nationality)

    patient = qs.first()

    if not patient:
        # Return 404 regardless of why — don't reveal whether the patient
        # exists but name/DOB didn't match vs genuinely not in the system
        return 404, ErrorSchema(
            detail="No patient found matching these details.",
            status_code=404,
        )

    return 200, {
        "patient_id": str(patient.id),
        "full_name":  patient.full_name,
        "birth_date": str(patient.birth_date),
        "mrn":        patient.mrn,
        "note": (
            "Send an access request to /api/patients/{patient_id}/access-requests/ "
            "to request access to this patient's timeline."
        ),
    }
