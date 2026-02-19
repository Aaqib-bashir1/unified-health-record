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
from uuid import UUID

from django.core.exceptions import ValidationError
from ninja import Router

from users.schemas import ErrorSchema
from core.auth import JWTBearer, get_current_user

from .exceptions import (
    AccessDenied,
    DuplicateAccessError,
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