"""
practitioners/services.py
=========================
Service layer for practitioner registration and org membership.

Function index:
  create_practitioner(user, data)                          → Practitioner
  get_practitioner_for_user(user)                          → Practitioner
  update_practitioner(user, data)                          → Practitioner
  request_membership(user, data)                           → OrgMembershipRequest
  list_membership_requests(user, org_id)                   → QuerySet
  approve_membership(user, org_id, request_id)             → OrgMembershipRequest
  reject_membership(user, org_id, request_id, reason)      → OrgMembershipRequest
  cancel_membership_request(user, request_id)              → OrgMembershipRequest
  list_my_roles(user)                                      → QuerySet[PractitionerRole]
"""

import logging
from datetime import date
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.organisations.exceptions import OrgNotFound, OrgNotVerified
from apps.organisations.models import Organisation
from apps.organisations.services import is_org_admin

from .exceptions import (
    AlreadyMember,
    DuplicatePendingMembershipRequest,
    MembershipRequestNotFound,
    MembershipRequestNotPending,
    NotOrgAdmin,
    PractitionerNotFound,
    PractitionerProfileExists,
)
from .models import (
    MembershipRequestStatus,
    OrgMembershipRequest,
    Practitioner,
    PractitionerRole,
    VerificationSource,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# HELPERS
# ===========================================================================

def _get_practitioner_for_user(user) -> Practitioner:
    try:
        return Practitioner.objects.select_related("user").get(user=user, is_active=True)
    except Practitioner.DoesNotExist:
        raise PractitionerNotFound(
            "No practitioner profile found. Create one first at POST /practitioners/."
        )


def _assert_org_admin(user, org_id: UUID) -> None:
    if not is_org_admin(user, org_id) and not user.is_staff:
        raise NotOrgAdmin()


# ===========================================================================
# CREATE PRACTITIONER PROFILE
# ===========================================================================

@transaction.atomic
def create_practitioner(user, data) -> Practitioner:
    """
    Create a practitioner profile for the requesting user.
    One profile per user — raises if one already exists.
    """
    if Practitioner.objects.filter(user=user).exists():
        raise PractitionerProfileExists()

    practitioner = Practitioner.objects.create(
        user                      = user,
        full_name                 = data.full_name,
        gender                    = data.gender,
        birth_date                = data.birth_date,
        license_number            = data.license_number,
        license_issuing_authority = data.license_issuing_authority,
        license_expires_at        = data.license_expires_at,
        specialization            = data.specialization,
        qualification             = data.qualification,
        verification_source       = VerificationSource.SELF_REPORTED,
    )

    logger.info(
        "Practitioner profile created. practitioner_id=%s user_id=%s",
        practitioner.id, user.id,
    )
    return practitioner


# ===========================================================================
# GET / UPDATE PRACTITIONER
# ===========================================================================

def get_practitioner_for_user(user) -> Practitioner:
    return _get_practitioner_for_user(user)


@transaction.atomic
def update_practitioner(user, data) -> Practitioner:
    """Update own practitioner profile. PATCH semantics."""
    practitioner = Practitioner.objects.select_for_update().get(user=user, is_active=True)

    IMMUTABLE = {"user", "is_verified", "verified_at", "verification_source"}
    payload = {
        k: v for k, v in data.model_dump(exclude_unset=True).items()
        if k not in IMMUTABLE
    }

    for field, value in payload.items():
        setattr(practitioner, field, value)

    practitioner.save(update_fields=list(payload.keys()) + ["updated_at"])
    return practitioner


# ===========================================================================
# REQUEST TO JOIN ORG
# ===========================================================================

@transaction.atomic
def request_membership(user, data) -> OrgMembershipRequest:
    """
    Practitioner requests to join an organisation.

    Rules:
      - Must have a practitioner profile
      - Org must exist and be verified
      - Cannot request if already an active member
      - Only one pending request per (practitioner, org)
    """
    practitioner = _get_practitioner_for_user(user)

    try:
        org = Organisation.objects.get(pk=data.organisation_id, is_active=True)
    except Organisation.DoesNotExist:
        raise OrgNotFound()

    if not org.verified:
        raise OrgNotVerified(
            "This organisation has not been verified by UHR yet. "
            "You cannot request to join an unverified organisation."
        )

    # Block if already an active member
    if PractitionerRole.objects.filter(
        practitioner=practitioner,
        organisation=org,
        is_active=True,
    ).exists():
        raise AlreadyMember()

    # Block duplicate pending request
    if OrgMembershipRequest.objects.filter(
        practitioner=practitioner,
        organisation=org,
        status=MembershipRequestStatus.PENDING,
    ).exists():
        raise DuplicatePendingMembershipRequest()

    req = OrgMembershipRequest.objects.create(
        practitioner         = practitioner,
        organisation         = org,
        requested_role_title = data.requested_role_title,
        requested_department = data.requested_department,
        message              = data.message,
    )

    logger.info(
        "Membership request created. request_id=%s practitioner=%s org=%s",
        req.id, practitioner.id, org.id,
    )
    return req


# ===========================================================================
# LIST MEMBERSHIP REQUESTS
# ===========================================================================

def list_membership_requests(user, org_id: UUID, status_filter: str = None):
    """
    List membership requests for an org. Org admin or UHR staff only.
    """
    _assert_org_admin(user, org_id)

    qs = (
        OrgMembershipRequest.objects
        .filter(organisation_id=org_id)
        .select_related("practitioner__user", "organisation")
        .order_by("-created_at")
    )

    if status_filter:
        qs = qs.filter(status=status_filter)

    return qs


# ===========================================================================
# APPROVE MEMBERSHIP
# ===========================================================================

@transaction.atomic
def approve_membership(user, org_id: UUID, request_id: UUID) -> OrgMembershipRequest:
    """
    Org admin approves a membership request.

    Atomically:
      1. Creates PractitionerRole (practitioner joins org)
      2. Sets practitioner.is_verified = True
      3. Sets practitioner.verification_source = org_admin
      4. Marks request as approved + links resulting role
      5. If this is practitioner's first role → set is_primary = True
    """
    _assert_org_admin(user, org_id)

    try:
        req = (
            OrgMembershipRequest.objects
            .select_for_update()
            .select_related("practitioner", "organisation")
            .get(id=request_id, organisation_id=org_id)
        )
    except OrgMembershipRequest.DoesNotExist:
        raise MembershipRequestNotFound()

    if not req.is_pending:
        raise MembershipRequestNotPending(
            f"This request is already {req.status}."
        )

    practitioner = req.practitioner
    now          = timezone.now()

    # Determine if this should be the primary role
    has_existing_role = PractitionerRole.objects.filter(
        practitioner=practitioner,
        is_active=True,
    ).exists()
    is_primary = not has_existing_role

    # Create the role
    role = PractitionerRole.objects.create(
        practitioner = practitioner,
        organisation = req.organisation,
        role_title   = req.requested_role_title,
        department   = req.requested_department,
        start_date   = now.date(),
        is_active    = True,
        is_primary   = is_primary,
        is_org_admin = False,
        approved_by  = user,
    )

    # Verify the practitioner
    practitioner.is_verified         = True
    practitioner.verified_at         = now
    practitioner.verification_source = VerificationSource.ORG_ADMIN
    practitioner.save(update_fields=[
        "is_verified", "verified_at", "verification_source", "updated_at"
    ])

    # Stamp request as approved
    req.status         = MembershipRequestStatus.APPROVED
    req.responded_at   = now
    req.responded_by   = user
    req.resulting_role = role
    req.save(update_fields=[
        "status", "responded_at", "responded_by", "resulting_role", "updated_at"
    ])

    logger.info(
        "Membership approved. request_id=%s practitioner=%s org=%s role_id=%s",
        request_id, practitioner.id, org_id, role.id,
    )
    return req


# ===========================================================================
# REJECT MEMBERSHIP
# ===========================================================================

@transaction.atomic
def reject_membership(user, org_id: UUID, request_id: UUID, reason: str = None) -> OrgMembershipRequest:
    """Org admin rejects a membership request."""
    _assert_org_admin(user, org_id)

    try:
        req = (
            OrgMembershipRequest.objects
            .select_for_update()
            .get(id=request_id, organisation_id=org_id)
        )
    except OrgMembershipRequest.DoesNotExist:
        raise MembershipRequestNotFound()

    if not req.is_pending:
        raise MembershipRequestNotPending(f"This request is already {req.status}.")

    now              = timezone.now()
    req.status           = MembershipRequestStatus.REJECTED
    req.responded_at     = now
    req.responded_by     = user
    req.rejection_reason = reason
    req.save(update_fields=[
        "status", "responded_at", "responded_by", "rejection_reason", "updated_at"
    ])

    logger.info(
        "Membership rejected. request_id=%s org=%s by user=%s",
        request_id, org_id, user.id,
    )
    return req


# ===========================================================================
# CANCEL OWN REQUEST
# ===========================================================================

@transaction.atomic
def cancel_membership_request(user, request_id: UUID) -> OrgMembershipRequest:
    """Practitioner cancels their own pending membership request."""
    try:
        req = OrgMembershipRequest.objects.select_for_update().get(
            id=request_id,
            practitioner__user=user,
        )
    except OrgMembershipRequest.DoesNotExist:
        raise MembershipRequestNotFound()

    if not req.is_pending:
        raise MembershipRequestNotPending(
            f"Only pending requests can be cancelled. This request is {req.status}."
        )

    req.status = MembershipRequestStatus.CANCELLED
    req.save(update_fields=["status", "updated_at"])
    return req


# ===========================================================================
# LIST MY ROLES
# ===========================================================================

def list_my_roles(user):
    """List all active PractitionerRoles for the requesting user."""
    return (
        PractitionerRole.objects
        .filter(practitioner__user=user)
        .select_related("organisation")
        .order_by("-is_primary", "-created_at")
    )