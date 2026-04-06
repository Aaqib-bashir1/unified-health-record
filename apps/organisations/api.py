"""
organisations/api.py
====================
Organisation management endpoints.

All endpoints mounted at /api/organisations/ in config/api.py.
"""

import logging
from uuid import UUID

from django.core.exceptions import ValidationError
from ninja import Router

from apps.users.schemas import ErrorSchema
from core.auth import JWTBearer, get_current_user

from . import services
from .exceptions import (
    OrgAccessDenied,
    OrgAlreadyVerified,
    OrgNotActive,
    OrgNotFound,
)
from .schemas import (
    CreateOrgSchema,
    OrgResponseSchema,
    OrgSummarySchema,
    UpdateOrgSchema,
)

logger   = logging.getLogger(__name__)
jwt_auth = JWTBearer()
router   = Router(tags=["Organisations"])


# ===========================================================================
# HELPERS
# ===========================================================================

def _build_org(org) -> dict:
    return {
        "id":                  org.id,
        "name":                org.name,
        "type":                org.type,
        "registration_number": org.registration_number,
        "description":         org.description,
        "website":             org.website,
        "email":               org.email,
        "phone":               org.phone,
        "address":             org.address,
        "country":             org.country,
        "parent_id":           org.parent_id,
        "verified":            org.verified,
        "verified_at":         org.verified_at,
        "is_active":           org.is_active,
        "created_at":          org.created_at,
    }


# ===========================================================================
# ENDPOINTS
# ===========================================================================

@router.post(
    "/",
    auth=jwt_auth,
    response={201: OrgResponseSchema, 400: ErrorSchema, 401: ErrorSchema},
    summary="Register an organisation",
    description=(
        "Register a new healthcare organisation. "
        "Starts as unverified — UHR staff must verify before it becomes operational. "
        "Any authenticated user can register on behalf of a facility."
    ),
)
def create_organisation(request, data: CreateOrgSchema):
    user = get_current_user(request)
    try:
        org = services.create_organisation(user, data)
        return 201, _build_org(org)
    except ValidationError as e:
        detail = "; ".join(
            f"{k}: {v}" for k, v in
            (e.message_dict if hasattr(e, "message_dict") else {"detail": str(e)}).items()
        )
        return 400, ErrorSchema(detail=detail, status_code=400)


@router.get(
    "/",
    auth=None,
    response={200: list[OrgSummarySchema]},
    summary="List verified organisations",
    description="Public endpoint. Returns verified + active organisations only.",
)
def list_organisations(
    request,
    country:  str = None,
    type:     str = None,
):
    orgs = services.list_organisations(
        verified_only=True,
        country=country,
        org_type=type,
    )
    return 200, [_build_org(o) for o in orgs]


@router.get(
    "/{org_id}/",
    auth=None,
    response={200: OrgResponseSchema, 404: ErrorSchema},
    summary="Get organisation detail",
)
def get_organisation(request, org_id: UUID):
    try:
        org = services.get_organisation(org_id)
        return 200, _build_org(org)
    except OrgNotFound:
        return 404, ErrorSchema(detail="Organisation not found.", status_code=404)


@router.patch(
    "/{org_id}/",
    auth=jwt_auth,
    response={
        200: OrgResponseSchema,
        400: ErrorSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
    },
    summary="Update organisation details",
    description="Org admin or UHR staff only. Name, contact, description updatable.",
)
def update_organisation(request, org_id: UUID, data: UpdateOrgSchema):
    user = get_current_user(request)
    try:
        org = services.update_organisation(user, org_id, data)
        return 200, _build_org(org)
    except OrgNotFound:
        return 404, ErrorSchema(detail="Organisation not found.", status_code=404)
    except OrgNotActive as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except OrgAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/{org_id}/verify/",
    auth=jwt_auth,
    response={
        200: OrgResponseSchema,
        401: ErrorSchema,
        403: ErrorSchema,
        404: ErrorSchema,
        409: ErrorSchema,
    },
    summary="Verify an organisation (UHR staff only)",
    description=(
        "Mark an organisation as verified. UHR staff (is_staff=True) only. "
        "Once verified, the org can host visit sessions and generate QR codes."
    ),
)
def verify_organisation(request, org_id: UUID):
    user = get_current_user(request)
    try:
        org = services.verify_organisation(user, org_id)
        return 200, _build_org(org)
    except OrgNotFound:
        return 404, ErrorSchema(detail="Organisation not found.", status_code=404)
    except OrgAlreadyVerified as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except OrgNotActive as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except OrgAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/{org_id}/practitioners/",
    auth=jwt_auth,
    response={
        200: list,
        401: ErrorSchema,
        404: ErrorSchema,
    },
    summary="List practitioners at an organisation",
)
def list_org_practitioners(request, org_id: UUID, active_only: bool = True):
    try:
        roles = services.list_org_practitioners(org_id, active_only=active_only)
        return 200, [
            {
                "practitioner_id":   r.practitioner_id,
                "full_name":         r.practitioner.full_name,
                "specialization":    r.practitioner.specialization,
                "is_verified":       r.practitioner.is_verified,
                "role_title":        r.role_title,
                "department":        r.department,
                "is_org_admin":      r.is_org_admin,
                "start_date":        str(r.start_date),
            }
            for r in roles
        ]
    except Exception as e:
        return 404, ErrorSchema(detail=str(e), status_code=404)