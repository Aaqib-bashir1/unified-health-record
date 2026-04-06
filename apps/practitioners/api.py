"""
practitioners/api.py
====================
Practitioner registration and org membership endpoints.

Mounted at /api/practitioners/ in config/api.py.
"""

import logging
from uuid import UUID

from ninja import Router

from apps.users.schemas import ErrorSchema
from core.auth import JWTBearer, get_current_user

from . import services
from .exceptions import (
    AlreadyMember,
    DuplicatePendingMembershipRequest,
    MembershipRequestNotFound,
    MembershipRequestNotPending,
    NotOrgAdmin,
    PractitionerNotFound,
    PractitionerProfileExists,
)
from .schemas import (
    CreatePractitionerSchema,
    JoinOrgSchema,
    MembershipRequestResponseSchema,
    PractitionerResponseSchema,
    PractitionerRoleResponseSchema,
    RejectMembershipSchema,
    UpdatePractitionerSchema,
)
from apps.organisations.exceptions import OrgNotFound, OrgNotVerified

logger   = logging.getLogger(__name__)
jwt_auth = JWTBearer()
router   = Router(tags=["Practitioners"])


# ===========================================================================
# HELPERS
# ===========================================================================

def _build_practitioner(p) -> dict:
    return {
        "id":                        p.id,
        "user_id":                   p.user_id,
        "full_name":                 p.full_name,
        "gender":                    p.gender,
        "birth_date":                p.birth_date,
        "license_number":            p.license_number,
        "license_issuing_authority": p.license_issuing_authority,
        "license_expires_at":        p.license_expires_at,
        "specialization":            p.specialization,
        "qualification":             p.qualification,
        "is_verified":               p.is_verified,
        "verified_at":               p.verified_at,
        "verification_source":       p.verification_source,
        "is_active":                 p.is_active,
        "created_at":                p.created_at,
    }


def _build_membership_request(req) -> dict:
    return {
        "id":                   req.id,
        "practitioner_id":      req.practitioner_id,
        "practitioner_name":    req.practitioner.full_name,
        "organisation_id":      req.organisation_id,
        "organisation_name":    req.organisation.name,
        "requested_role_title": req.requested_role_title,
        "requested_department": req.requested_department,
        "message":              req.message,
        "status":               req.status,
        "responded_at":         req.responded_at,
        "rejection_reason":     req.rejection_reason,
        "created_at":           req.created_at,
    }


def _build_role(role) -> dict:
    return {
        "id":                role.id,
        "practitioner_id":   role.practitioner_id,
        "organisation_id":   role.organisation_id,
        "organisation_name": role.organisation.name,
        "role_title":        role.role_title,
        "department":        role.department,
        "start_date":        role.start_date,
        "end_date":          role.end_date,
        "is_active":         role.is_active,
        "is_primary":        role.is_primary,
        "is_org_admin":      role.is_org_admin,
    }


# ===========================================================================
# PRACTITIONER PROFILE ENDPOINTS
# ===========================================================================

@router.post(
    "/",
    auth=jwt_auth,
    response={
        201: PractitionerResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Create a practitioner profile",
    description=(
        "Create a practitioner profile for the logged-in user. "
        "One profile per user. After creating, request to join an organisation."
    ),
)
def create_practitioner(request, data: CreatePractitionerSchema):
    user = get_current_user(request)
    try:
        practitioner = services.create_practitioner(user, data)
        return 201, _build_practitioner(practitioner)
    except PractitionerProfileExists as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)


@router.get(
    "/me/",
    auth=jwt_auth,
    response={
        200: PractitionerResponseSchema,
        401: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Get my practitioner profile",
)
def get_my_practitioner(request):
    user = get_current_user(request)
    try:
        practitioner = services.get_practitioner_for_user(user)
        return 200, _build_practitioner(practitioner)
    except PractitionerNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


@router.patch(
    "/me/",
    auth=jwt_auth,
    response={
        200: PractitionerResponseSchema,
        401: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Update my practitioner profile",
)
def update_my_practitioner(request, data: UpdatePractitionerSchema):
    user = get_current_user(request)
    try:
        practitioner = services.update_practitioner(user, data)
        return 200, _build_practitioner(practitioner)
    except PractitionerNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


@router.get(
    "/me/roles/",
    auth=jwt_auth,
    response={
        200: list[PractitionerRoleResponseSchema],
        401: ErrorSchema,
        404: ErrorSchema,
    },
    summary="List my organisational roles",
)
def list_my_roles(request):
    user = get_current_user(request)
    try:
        roles = services.list_my_roles(user)
        return 200, [_build_role(r) for r in roles]
    except PractitionerNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# MEMBERSHIP REQUEST ENDPOINTS
# ===========================================================================

@router.post(
    "/join/",
    auth=jwt_auth,
    response={
        201: MembershipRequestResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Request to join an organisation",
    description=(
        "Submit a request to join an organisation. "
        "The org admin will review and approve or reject. "
        "On approval, a PractitionerRole is created and your profile is marked verified."
    ),
)
def request_membership(request, data: JoinOrgSchema):
    user = get_current_user(request)
    try:
        req = services.request_membership(user, data)
        return 201, _build_membership_request(req)
    except PractitionerNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except OrgNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except OrgNotVerified as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except AlreadyMember as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except DuplicatePendingMembershipRequest as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)


@router.delete(
    "/join/{request_id}/",
    auth=jwt_auth,
    response={
        200: MembershipRequestResponseSchema,
        401: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Cancel a pending membership request",
)
def cancel_membership_request(request, request_id: UUID):
    user = get_current_user(request)
    try:
        req = services.cancel_membership_request(user, request_id)
        return 200, _build_membership_request(req)
    except MembershipRequestNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except MembershipRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)


@router.get(
    "/requests/",
    auth=jwt_auth,
    response={
        200: list[MembershipRequestResponseSchema],
        401: ErrorSchema,
        403: ErrorSchema,
    },
    summary="List membership requests for an org (org admin only)",
    description="Pass ?org_id=<uuid>&status=pending to filter.",
)
def list_membership_requests(request, org_id: UUID, status: str = None):
    user = get_current_user(request)
    try:
        reqs = services.list_membership_requests(user, org_id, status_filter=status)
        return 200, [_build_membership_request(r) for r in reqs]
    except NotOrgAdmin as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/requests/{request_id}/approve/",
    auth=jwt_auth,
    response={
        200: MembershipRequestResponseSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Approve a membership request (org admin only)",
    description=(
        "Approve a practitioner's request to join your organisation. "
        "Creates a PractitionerRole and marks the practitioner as verified."
    ),
)
def approve_membership(request, request_id: UUID, org_id: UUID):
    user = get_current_user(request)
    try:
        req = services.approve_membership(user, org_id, request_id)
        return 200, _build_membership_request(req)
    except MembershipRequestNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except MembershipRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except NotOrgAdmin as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/requests/{request_id}/reject/",
    auth=jwt_auth,
    response={
        200: MembershipRequestResponseSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Reject a membership request (org admin only)",
)
def reject_membership(request, request_id: UUID, org_id: UUID, data: RejectMembershipSchema):
    user = get_current_user(request)
    try:
        req = services.reject_membership(user, org_id, request_id, data.reason)
        return 200, _build_membership_request(req)
    except MembershipRequestNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except MembershipRequestNotPending as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except NotOrgAdmin as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)