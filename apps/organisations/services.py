"""
organisations/services.py
=========================
Service layer for organisation management.

Function index:
  create_organisation(user, data)            → Organisation
  get_organisation(org_id)                   → Organisation
  list_organisations(verified_only, country) → QuerySet
  update_organisation(user, org_id, data)    → Organisation
  verify_organisation(admin_user, org_id)    → Organisation
  list_org_practitioners(org_id)             → QuerySet[PractitionerRole]
  is_org_admin(user, org_id)                 → bool
"""

import logging
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from .exceptions import (
    OrgAccessDenied,
    OrgAlreadyVerified,
    OrgNotActive,
    OrgNotFound,
    OrgNotVerified,
)
from .models import Organisation

logger = logging.getLogger(__name__)


# ===========================================================================
# HELPERS
# ===========================================================================

def _get_org(org_id: UUID) -> Organisation:
    try:
        return Organisation.objects.get(pk=org_id)
    except Organisation.DoesNotExist:
        raise OrgNotFound()


def is_org_admin(user, org_id: UUID) -> bool:
    """
    Returns True if the user is an active org admin at this organisation.
    Checked by looking at their PractitionerRole with is_org_admin=True.
    """
    try:
        from practitioners.models import PractitionerRole
        return PractitionerRole.objects.filter(
            practitioner__user=user,
            organisation_id=org_id,
            is_active=True,
            is_org_admin=True,
        ).exists()
    except Exception:
        return False


def _assert_org_admin(user, org_id: UUID) -> None:
    if not user.is_staff and not is_org_admin(user, org_id):
        raise OrgAccessDenied(
            "Only org admins or UHR staff can perform this action."
        )


# ===========================================================================
# CREATE ORGANISATION
# ===========================================================================

@transaction.atomic
def create_organisation(user, data) -> Organisation:
    """
    Register a new organisation. Starts as unverified.
    Any authenticated user can register an org on behalf of a facility.
    UHR admin must verify before it becomes operational.
    """
    parent = None
    if data.parent_id:
        try:
            parent = Organisation.objects.get(pk=data.parent_id, is_active=True)
        except Organisation.DoesNotExist:
            from django.core.exceptions import ValidationError
            raise ValidationError({"parent_id": "Parent organisation not found."})

    org = Organisation.objects.create(
        name                = data.name,
        type                = data.type,
        registration_number = data.registration_number,
        description         = data.description,
        website             = data.website,
        email               = data.email,
        phone               = data.phone,
        address             = data.address,
        country             = data.country,
        parent              = parent,
    )

    logger.info(
        "Organisation registered. org_id=%s name=%s by user_id=%s",
        org.id, org.name, user.id,
    )
    return org


# ===========================================================================
# GET / LIST
# ===========================================================================

def get_organisation(org_id: UUID) -> Organisation:
    return _get_org(org_id)


def list_organisations(verified_only: bool = True, country: str = None, org_type: str = None):
    """
    List organisations. Public endpoint returns verified + active only.
    Staff can pass verified_only=False to see all.
    """
    qs = Organisation.objects.filter(is_active=True)

    if verified_only:
        qs = qs.filter(verified=True)
    if country:
        qs = qs.filter(country__iexact=country)
    if org_type:
        qs = qs.filter(type=org_type)

    return qs.order_by("name")


# ===========================================================================
# UPDATE ORGANISATION
# ===========================================================================

@transaction.atomic
def update_organisation(user, org_id: UUID, data) -> Organisation:
    """
    Update org contact details.
    Org admin or UHR staff only.
    Registration number and type cannot be changed after creation.
    """
    _assert_org_admin(user, org_id)

    org = Organisation.objects.select_for_update().get(pk=org_id)
    if not org.is_active:
        raise OrgNotActive()

    UPDATABLE = {"name", "description", "website", "email", "phone", "address"}
    payload = {
        k: v for k, v in data.model_dump(exclude_unset=True).items()
        if k in UPDATABLE and v is not None
    }

    for field, value in payload.items():
        setattr(org, field, value)

    org.save(update_fields=list(payload.keys()) + ["updated_at"])

    logger.info("Organisation updated. org_id=%s fields=%s", org_id, list(payload.keys()))
    return org


# ===========================================================================
# VERIFY ORGANISATION (UHR staff only)
# ===========================================================================

@transaction.atomic
def verify_organisation(admin_user, org_id: UUID) -> Organisation:
    """
    Mark an organisation as verified. UHR staff only (is_staff=True).
    Once verified, the org can host visit sessions and generate QR codes.
    """
    if not admin_user.is_staff:
        raise OrgAccessDenied("Only UHR staff can verify organisations.")

    org = Organisation.objects.select_for_update().get(pk=org_id)

    if not org.is_active:
        raise OrgNotActive()
    if org.verified:
        raise OrgAlreadyVerified()

    org.verified    = True
    org.verified_at = timezone.now()
    org.verified_by = admin_user
    org.save(update_fields=["verified", "verified_at", "verified_by", "updated_at"])

    logger.info(
        "Organisation verified. org_id=%s by admin=%s",
        org_id, admin_user.email,
    )
    return org


# ===========================================================================
# LIST ORG PRACTITIONERS
# ===========================================================================

def list_org_practitioners(org_id: UUID, active_only: bool = True):
    """List all practitioners at an organisation via their roles."""
    from practitioners.models import PractitionerRole
    qs = PractitionerRole.objects.filter(
        organisation_id=org_id,
    ).select_related("practitioner__user")

    if active_only:
        qs = qs.filter(is_active=True)

    return qs.order_by("practitioner__full_name")