"""
patients/services.py
====================
Service layer for the patients app.

Patterns followed from users/services.py:
  - Plain functions (not classes)
  - transaction.atomic for all writes
  - select_for_update() for concurrent-safe reads before writes
  - Raises domain exceptions (PatientError subclasses) for expected failure states
  - Raises Django ValidationError for input-level failures
  - transaction.on_commit for any side effects (emails, audit writes)
  - Never returns HTTP responses — that is the API layer's job

Function index:
  create_patient(user, data)             → Patient + PatientUserAccess
  get_patient_for_user(user, patient_id) → (Patient, PatientUserAccess)
  list_my_patients(user)                 → QuerySet[Patient] annotated with access context
  update_patient(user, patient_id, data) → Patient
  retract_patient(user, patient_id, reason) → Patient
  list_patient_access(user, patient_id)  → QuerySet[PatientUserAccess]
  grant_access(user, patient_id, data)   → PatientUserAccess
  revoke_access(user, patient_id, access_id, reason) → PatientUserAccess
  self_exit(user, patient_id, reason)    → PatientUserAccess
"""

import logging
from datetime import date
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .exceptions import (
    AccessDenied,
    DuplicateAccessError,
    OrphanProtectionError,
    PatientNotFound,
    PatientRetracted,
)
from .models import AccessRole, ClaimMethod, Patient, PatientUserAccess, TrustLevel

logger = logging.getLogger(__name__)
User = get_user_model()


# ===========================================================================
# INTERNAL HELPERS
# ===========================================================================

def _get_active_access(user, patient_id: UUID) -> PatientUserAccess:
    """
    Fetch the requesting user's active PatientUserAccess for this patient.
    Raises PatientNotFound if the patient does not exist or the user has no access.
    Raises PatientRetracted if the profile has been soft-deleted.

    Using a single query that joins patient + access avoids two round-trips
    and prevents a race where the patient is retracted between the two checks.
    """
    try:
        access = (
            PatientUserAccess.objects
            .select_related("patient")
            .get(
                user=user,
                patient_id=patient_id,
                is_active=True,
            )
        )
    except PatientUserAccess.DoesNotExist:
        raise PatientNotFound()

    if not access.patient.is_active:
        raise PatientRetracted()

    return access


def _generate_mrn() -> str:
    """
    Generate a unique UHR-internal Medical Record Number.
    Format: UHR-YYYYMMDD-XXXXXXXX (date + 8 random hex chars).
    Collision probability is negligible but the service retries if needed.
    """
    import secrets
    from django.utils import timezone
    date_str = timezone.now().strftime("%Y%m%d")
    suffix   = secrets.token_hex(4).upper()
    return f"UHR-{date_str}-{suffix}"


def _assert_can_write(access: PatientUserAccess) -> None:
    """Raise AccessDenied if the access record does not grant write permission."""
    if not access.can_write:
        raise AccessDenied(
            f"Your role '{access.role}' does not permit writing to this profile."
        )


def _assert_can_manage(access: PatientUserAccess) -> None:
    """Raise AccessDenied if the access record does not grant access management."""
    if not access.can_manage_access:
        raise AccessDenied(
            "Only the primary holder (or a delegate on unclaimed profiles) "
            "can manage access to this profile."
        )


def _active_holder_count(patient: Patient) -> int:
    """Count of currently active access records for a patient profile."""
    return PatientUserAccess.objects.filter(
        patient=patient,
        is_active=True,
    ).count()


# ===========================================================================
# CREATE PATIENT PROFILE
# ===========================================================================

@transaction.atomic
def create_patient(user, data) -> tuple[Patient, PatientUserAccess]:
    """
    Create a new patient profile and the creator's access record.

    data.is_dependent=False → caller creates their own profile
      - role=primary, claim_method=system_created, trust_level=system
      - Patient.is_claimed=True immediately (no claim flow needed)

    data.is_dependent=True → caller creates a dependent's profile (e.g. child)
      - role=full_delegate, claim_method=system_created, trust_level=delegate_granted
      - Patient.is_claimed=False (dependent must claim later)

    MRN is auto-generated if not provided.
    Returns (patient, access) tuple.
    """
    # Generate unique MRN with retry on collision
    mrn = None
    for _ in range(5):
        candidate = _generate_mrn()
        if not Patient.objects.filter(mrn=candidate).exists():
            mrn = candidate
            break
    if not mrn:
        raise ValidationError({"mrn": "Could not generate a unique MRN. Please try again."})

    # Determine claim state based on whether this is a self or dependent profile
    is_dependent = getattr(data, "is_dependent", False)
    is_claimed   = not is_dependent
    claimed_at   = timezone.now() if is_claimed else None

    patient = Patient(
        mrn                  = mrn,
        first_name           = data.first_name,
        last_name            = data.last_name,
        gender               = data.gender,
        birth_date           = data.birth_date,
        phone                = data.phone,
        email                = data.email,
        address              = data.address,
        blood_group          = data.blood_group,
        nationality          = data.nationality,
        is_deceased          = data.is_deceased,
        deceased_date        = data.deceased_date,
        is_claimed           = is_claimed,
        claimed_at           = claimed_at,
        transfer_eligible_at = data.transfer_eligible_at,
    )
    patient.save()

    # Determine access role and trust level
    if is_dependent:
        role         = AccessRole.FULL_DELEGATE
        trust_level  = TrustLevel.DELEGATE_GRANTED
    else:
        role         = AccessRole.PRIMARY
        trust_level  = TrustLevel.SYSTEM

    access = PatientUserAccess.objects.create(
        user         = user,
        patient      = patient,
        role         = role,
        claim_method = ClaimMethod.SYSTEM_CREATED,
        trust_level  = trust_level,
        granted_by   = None,    # Self-created — no explicit grantor
    )

    logger.info(
        "Patient profile created. patient_id=%s user_id=%s role=%s",
        patient.id, user.id, role,
    )

    return patient, access


# ===========================================================================
# GET SINGLE PATIENT
# ===========================================================================

def get_patient_for_user(user, patient_id: UUID) -> tuple[Patient, PatientUserAccess]:
    """
    Fetch a patient profile the requesting user has access to.
    Returns (patient, access) so the API can include access context in the response.
    Raises PatientNotFound / PatientRetracted via _get_active_access.
    """
    access = _get_active_access(user, patient_id)
    return access.patient, access


# ===========================================================================
# LIST MY PATIENT PROFILES
# ===========================================================================

def list_my_patients(user) -> list[dict]:
    """
    Return all patient profiles the requesting user currently has active access to,
    annotated with the user's access role and permissions for each profile.

    Returns a list of dicts rather than a raw queryset so the API layer
    doesn't need to perform additional joins to get role context.
    """
    accesses = (
        PatientUserAccess.objects
        .filter(user=user, is_active=True)
        .select_related("patient")
        .order_by("patient__last_name", "patient__first_name")
    )

    results = []
    for access in accesses:
        patient = access.patient
        if not patient.is_active:
            continue  # Silently skip retracted profiles

        results.append({
            "patient":    patient,
            "my_role":    access.role,
            "can_write":  access.can_write,
            "can_manage": access.can_manage_access,
        })

    return results


# ===========================================================================
# UPDATE PATIENT PROFILE
# ===========================================================================

@transaction.atomic
def update_patient(user, patient_id: UUID, data) -> Patient:
    """
    Update demographic fields on a patient profile.
    Requires write access (primary, full_delegate, caregiver).

    Uses select_for_update() to prevent concurrent partial updates
    on the same patient profile.

    Only fields explicitly provided in data are updated (PATCH semantics).
    None values are treated as "not provided" — not as "clear this field".
    """
    access = _get_active_access(user, patient_id)
    _assert_can_write(access)

    patient = (
        Patient.objects
        .select_for_update()
        .get(pk=patient_id)
    )

    # model_dump(exclude_unset=True) gives only fields the client explicitly sent.
    # Correct PATCH semantics:
    #   Field absent from payload  → not touched (old value preserved)
    #   Field sent as null         → explicitly cleared (set to None)
    #   Field sent with value      → updated to new value
    #
    # The previous getattr+None loop was wrong: it treated null and absent
    # identically, making it impossible to clear optional fields like phone/email.
    IMMUTABLE_FIELDS = {"mrn", "birth_date", "id", "created_at"}
    payload = {
        k: v for k, v in data.model_dump(exclude_unset=True).items()
        if k not in IMMUTABLE_FIELDS
    }

    if not payload:
        raise ValidationError({"detail": "No fields provided to update."})

    for field, value in payload.items():
        setattr(patient, field, value)

    update_fields = list(payload.keys()) + ["updated_at"]
    patient.save(update_fields=update_fields)

    logger.info(
        "Patient profile updated. patient_id=%s user_id=%s fields=%s",
        patient.id, user.id, update_fields,
    )

    return patient


# ===========================================================================
# RETRACT (SOFT DELETE) PATIENT PROFILE
# ===========================================================================

@transaction.atomic
def retract_patient(user, patient_id: UUID, reason: str) -> Patient:
    """
    Soft-retract a patient profile (schema invariant 12.1).
    Only the primary holder or an admin can retract a profile.
    Sets deleted_at + retraction_reason. Never physically deletes.

    After retraction:
      - Profile is excluded from all standard list queries
      - GET by ID returns PatientRetracted
      - All medical events remain intact and are preserved
    """
    from django.utils import timezone

    access = _get_active_access(user, patient_id)

    # Only primary holder can retract
    if access.role != AccessRole.PRIMARY:
        raise AccessDenied("Only the primary holder can retract a patient profile.")

    patient = (
        Patient.objects
        .select_for_update()
        .get(pk=patient_id)
    )

    patient.deleted_at        = timezone.now()
    patient.retraction_reason = reason
    patient.save(update_fields=["deleted_at", "retraction_reason", "updated_at"])

    logger.info(
        "Patient profile retracted. patient_id=%s by user_id=%s reason=%s",
        patient.id, user.id, reason[:50],
    )

    return patient


# ===========================================================================
# LIST ACCESS HOLDERS
# ===========================================================================

def list_patient_access(user, patient_id: UUID, include_history: bool = False):
    """
    List all users who have (or have had) access to a patient profile.
    Only the primary holder (or delegate on unclaimed profiles) can see the full list.
    Caregivers and viewers can only see their own access record.

    include_history=True → include revoked records (full audit view)
    include_history=False → active records only (default)
    """
    access = _get_active_access(user, patient_id)

    if access.can_manage_access:
        # Primary / managing delegate sees all access records
        qs = (
            PatientUserAccess.objects
            .filter(patient_id=patient_id)
            .select_related("user", "granted_by", "revoked_by")
            .order_by("-granted_at")
        )
        if not include_history:
            qs = qs.filter(is_active=True)
    else:
        # Caregiver / viewer sees only their own record
        qs = PatientUserAccess.objects.filter(
            patient_id=patient_id,
            user=user,
        ).select_related("user")

    return qs


# ===========================================================================
# GRANT ACCESS
# ===========================================================================

@transaction.atomic
def grant_access(user, patient_id: UUID, data) -> PatientUserAccess:
    """
    Grant another user access to a patient profile.
    Only the primary holder (or delegate on unclaimed profiles) can grant access.
    role=primary cannot be granted — only the claim system assigns it.

    Raises DuplicateAccessError if the target user already has active access.
    Raises ValidationError if the target user does not exist.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_manage(access)

    # Resolve target user by email
    try:
        target_user = User.objects.get(email=data.user_email, is_active=True)
    except User.DoesNotExist:
        raise ValidationError({
            "user_email": f"No active user found with email '{data.user_email}'."
        })

    # Prevent self-grant
    if target_user == user:
        raise ValidationError({"user_email": "You cannot grant access to yourself."})

    # Check for existing active access
    existing = PatientUserAccess.objects.filter(
        user=target_user,
        patient_id=patient_id,
        is_active=True,
    ).first()

    if existing:
        raise DuplicateAccessError(
            f"User '{data.user_email}' already has active {existing.role} access to this profile."
        )

    new_access = PatientUserAccess.objects.create(
        user         = target_user,
        patient_id   = patient_id,
        role         = data.role,
        claim_method = ClaimMethod.SYSTEM_CREATED,
        trust_level  = TrustLevel.DELEGATE_GRANTED,
        granted_by   = user,
        notes        = data.notes,
    )

    logger.info(
        "Access granted. patient_id=%s target_user=%s role=%s by user_id=%s",
        patient_id, target_user.id, data.role, user.id,
    )

    return new_access


# ===========================================================================
# REVOKE ACCESS
# ===========================================================================

@transaction.atomic
def revoke_access(user, patient_id: UUID, access_id: UUID, reason: str) -> PatientUserAccess:
    """
    Revoke another user's access to a patient profile.
    Only the primary holder (or delegate on unclaimed profiles) can revoke.
    Cannot revoke the primary holder's own access (use self_exit for that).
    Cannot revoke an access record that would orphan the profile.

    Uses select_for_update() to prevent concurrent revocations racing each other.
    """
    from django.utils import timezone

    actor_access = _get_active_access(user, patient_id)
    _assert_can_manage(actor_access)

    # Lock the target access record
    try:
        target_access = (
            PatientUserAccess.objects
            .select_for_update()
            .select_related("patient")
            .get(
                id=access_id,
                patient_id=patient_id,
                is_active=True,
            )
        )
    except PatientUserAccess.DoesNotExist:
        raise PatientNotFound("Access record not found or already revoked.")

    # Cannot revoke primary holder via this endpoint
    if target_access.role == AccessRole.PRIMARY:
        raise AccessDenied(
            "Cannot revoke the primary holder's access via this endpoint. "
            "Use the profile transfer or support claim flow."
        )

    # Cannot revoke your own record via this endpoint (use self_exit)
    if target_access.user == user:
        raise AccessDenied("Use the self-exit endpoint to remove your own access.")

    # Orphan protection with row-level lock on the patient.
    # We must lock the patient row BEFORE counting active holders.
    # Without this, two concurrent revocations can both read count=2,
    # both pass the check, and both revoke — leaving the profile with zero holders.
    # select_for_update() on the patient row serialises concurrent revocations.
    Patient.objects.select_for_update().get(pk=patient_id)
    if _active_holder_count(target_access.patient) <= 1:
        raise OrphanProtectionError()

    now = timezone.now()
    target_access.is_active        = False
    target_access.revoked_at       = now
    target_access.revoked_by       = user
    target_access.revocation_reason = reason
    target_access.save(update_fields=[
        "is_active", "revoked_at", "revoked_by", "revocation_reason", "updated_at"
    ])

    logger.info(
        "Access revoked. patient_id=%s access_id=%s by user_id=%s",
        patient_id, access_id, user.id,
    )

    return target_access


# ===========================================================================
# SELF-EXIT
# ===========================================================================

@transaction.atomic
def self_exit(user, patient_id: UUID, reason: str = None) -> PatientUserAccess:
    """
    A user removes their own access to a patient profile (voluntary exit).
    Anyone can self-exit except the primary holder of a claimed profile
    (that would leave the profile without a sovereign, which is never allowed).

    For unclaimed profiles: full_delegate can self-exit only if another
    active delegate exists (orphan protection).
    """
    from django.utils import timezone

    try:
        access = (
            PatientUserAccess.objects
            .select_for_update()
            .select_related("patient")
            .get(
                user=user,
                patient_id=patient_id,
                is_active=True,
            )
        )
    except PatientUserAccess.DoesNotExist:
        raise PatientNotFound("No active access found for this profile.")

    # Orphan protection: lock patient row before counting, same as revoke_access.
    Patient.objects.select_for_update().get(pk=patient_id)

    # Primary holder of a claimed profile cannot self-exit
    if access.role == AccessRole.PRIMARY and access.patient.is_claimed:
        raise AccessDenied(
            "The primary holder of a claimed profile cannot remove their own access. "
            "Transfer primary ownership first via the claim flow."
        )

    # Orphan protection
    if _active_holder_count(access.patient) <= 1:
        raise OrphanProtectionError(
            "Cannot exit: you are the only active holder of this profile. "
            "Grant access to another user before exiting."
        )

    now = timezone.now()
    access.is_active         = False
    access.revoked_at        = now
    access.revoked_by        = user   # Self — revoked_by == user is the self-exit signal
    access.revocation_reason = reason or "Self-exit."
    access.save(update_fields=[
        "is_active", "revoked_at", "revoked_by", "revocation_reason", "updated_at"
    ])

    logger.info(
        "Self-exit completed. patient_id=%s user_id=%s",
        patient_id, user.id,
    )

    return access