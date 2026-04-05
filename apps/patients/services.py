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
from .models import AccessRequest, AccessRequestStatus, AccessRole, ClaimMethod, Patient, PatientUserAccess, TrustLevel

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


def user_has_self_profile(user) -> bool:
    """
    Return True if the user already holds an active primary role on any patient profile.

    Used to enforce two rules in create_patient:
      1. Block a second self-profile — one primary identity per user.
      2. Gate dependent creation — must have own identity before managing others.

    Checks role=PRIMARY only, not claim_method.
    A user who became primary via national ID claim, OTP, or support ticket
    is still primary. Role is the authority, not how it was established.
    """
    return PatientUserAccess.objects.filter(
        user=user,
        role=AccessRole.PRIMARY,
        is_active=True,
    ).exists()


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
    # Lock the user row to serialise concurrent profile creation.
    #
    # Locking PatientUserAccess rows would NOT work for new users — if no rows
    # exist yet, select_for_update().filter() returns an empty queryset and
    # PostgreSQL acquires nothing. Two concurrent requests would both pass
    # user_has_self_profile() → False and both create primary profiles.
    #
    # Locking the user row is correct because:
    #   - It always exists (user is authenticated, therefore active)
    #   - It's guaranteed to lock exactly one row
    #   - It serialises all identity-creating operations for this user
    #
    # Second concurrent transaction blocks here until the first commits,
    # then re-evaluates user_has_self_profile() and sees True → clean failure.
    User.objects.select_for_update().get(pk=user.pk)

    # Generate unique MRN with retry on collision
    mrn = None
    for _ in range(5):
        candidate = _generate_mrn()
        if not Patient.objects.filter(mrn=candidate).exists():
            mrn = candidate
            break
    if not mrn:
        raise ValidationError({"mrn": "Could not generate a unique MRN. Please try again."})

    is_dependent = getattr(data, "is_dependent", False)

    if not is_dependent:
        # Rule 1: one personal profile per user.
        # Checked here — not at schema level — because it requires a DB read.
        # claim_method is deliberately excluded from this check: a user who became
        # primary via a claim flow is still a primary. Role is the authority.
        if user_has_self_profile(user):
            raise ValidationError({
                "detail": "You already have a personal patient profile. "
                          "You can only hold one primary identity in UHR."
            })
    else:
        # Rule 2: must have own profile before creating dependents.
        # Prevents anonymous delegate chains with no accountable owner.
        # A user who manages a child's profile must themselves exist in UHR.
        if not user_has_self_profile(user):
            raise ValidationError({
                "detail": "You must create your own patient profile before "
                          "adding profiles for dependents."
            })

    # Determine claim state
    is_claimed = not is_dependent
    claimed_at = timezone.now() if is_claimed else None

    # Soft duplicate detection — same name + DOB already under this user's account.
    # Does NOT block — returns a warning so the frontend can ask for confirmation.
    # Pass force_create=True to override (e.g. twins, same-name siblings).
    # Scoped to this user's profiles only — not a global uniqueness check.
    # Email is intentionally excluded: it is a contact channel, not an identity
    # guarantee (ExternalPatientIdentity owns identity proof).
    if is_dependent:
        force_create = getattr(data, "force_create", False)
        possible_duplicate = Patient.objects.filter(
            first_name__iexact=data.first_name,
            last_name__iexact=data.last_name,
            birth_date=data.birth_date,
            user_accesses__user=user,
            user_accesses__is_active=True,
        ).exists()

        if possible_duplicate and not force_create:
            raise DuplicateProfileWarning()

        if possible_duplicate and force_create:
            logger.warning(
                "Duplicate profile warning overridden with force_create. "
                "user_id=%s first_name=%s last_name=%s birth_date=%s",
                user.id, data.first_name, data.last_name, data.birth_date,
            )

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
        trust_level  = TrustLevel.UNVERIFIED

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


# ===========================================================================
# MERGE PATIENT PROFILES (ADMIN ONLY)
# ===========================================================================

@transaction.atomic
def merge_patients(
    admin_user,
    patient_a_id: UUID,
    patient_b_id: UUID,
    reason: str,
) -> Patient:
    """
    Merge two duplicate patient profiles.

    This is an irreversible admin operation. Only users with the
    patients.merge_patient permission may call this. Django superusers
    pass this check automatically via has_perm().

    Source/target determination is automatic — no manual decision required:
      - Earlier created profile (lower created_at) → TARGET (survives)
      - Later created profile  (higher created_at) → SOURCE (retracted)

    Rationale: the older profile is more likely to have the more complete
    medical history and to be the canonical identity in the system.

    What happens atomically:
      1. Both rows locked (select_for_update, UUID-order to prevent deadlock)
      2. Older profile assigned as target, newer as source
      3. All PatientUserAccess records on source revoked with merge reason
      4. All medical events on source reassigned to target
      5. Source soft-retracted, retraction_reason references target
      6. AuditLog entry written via on_commit

    What does NOT happen:
      - Target's access records are NOT modified
      - No records are deleted anywhere — append-only invariant preserved
      - Medical event content and timestamps are unchanged

    Raises:
      PermissionError  — caller lacks patients.merge_patient permission
      ValidationError  — both IDs are the same profile
      PatientNotFound  — either profile does not exist
      PatientRetracted — either profile is already retracted
    """
    # Permission check — enforced here, not only in admin view, because
    # the service can be called from management commands and scripts too.
    # Django superusers pass has_perm() automatically without explicit grant.
    if not admin_user.has_perm("patients.merge_patient"):
        raise PermissionError(
            "You do not have permission to merge patient profiles. "
            "The 'patients.merge_patient' permission is required."
        )

    if patient_a_id == patient_b_id:
        raise ValidationError({
            "detail": "Cannot merge a profile with itself."
        })

    # Lock both rows in consistent UUID order to prevent deadlocks.
    # Two concurrent merges involving the same patients in different order
    # would each hold one lock and wait for the other without this ordering.
    first_id, second_id = sorted([patient_a_id, patient_b_id])
    locked = {
        p.id: p for p in
        Patient.objects.select_for_update().filter(id__in=[first_id, second_id])
    }

    if len(locked) < 2:
        raise PatientNotFound("One or both patient profiles do not exist.")

    patient_a = locked[patient_a_id]
    patient_b = locked[patient_b_id]

    if not patient_a.is_active:
        raise PatientRetracted(f"Profile '{patient_a.full_name}' has already been retracted.")
    if not patient_b.is_active:
        raise PatientRetracted(f"Profile '{patient_b.full_name}' has already been retracted.")

    # Determine source and target by creation date.
    # Earlier created = target (canonical, survives).
    # Later created   = source (duplicate, retracted).
    if patient_a.created_at <= patient_b.created_at:
        target, source = patient_a, patient_b
    else:
        target, source = patient_b, patient_a

    source_id = source.id
    target_id = target.id

    logger.info(
        "Merge determined: target=%s (created %s) source=%s (created %s)",
        target_id, target.created_at.date(),
        source_id, source.created_at.date(),
    )

    now = timezone.now()

    # Step 1 — Revoke all active access records on the source profile.
    # Each record is updated individually so revoked_at is stamped correctly.
    # A bulk update would set revoked_at but lose the per-record granularity
    # needed for accurate audit queries.
    source_accesses = PatientUserAccess.objects.filter(
        patient=source,
        is_active=True,
    ).select_for_update()

    revoke_reason = (
        f"Profile merged into patient {target_id} by admin {admin_user.email} "
        f"on {now.date().isoformat()}. Reason: {reason}"
    )

    for access in source_accesses:
        access.is_active         = False
        access.revoked_at        = now
        access.revoked_by        = admin_user
        access.revocation_reason = revoke_reason
        access.save(update_fields=[
            "is_active", "revoked_at", "revoked_by", "revocation_reason", "updated_at"
        ])

    # Step 2 — Reassign medical events from source to target.
    # Uses a direct queryset update — efficient for large event histories.
    # The medical_events app is not yet built, so this targets the table
    # directly by app_label. When medical_events models exist this resolves
    # to their patient FK. Events themselves (content, timestamps) are unchanged.
    try:
        from django.apps import apps
        MedicalEvent = apps.get_model("medical_events", "MedicalEvent")
        events_reassigned = MedicalEvent.objects.filter(
            patient=source
        ).update(patient=target)
    except LookupError:
        # medical_events app not yet registered — safe to skip for now.
        # Once the app exists this branch will never be reached.
        events_reassigned = 0
        logger.warning(
            "medical_events app not found during merge. "
            "No events reassigned. patient_id=%s", source_id
        )

    # Step 3 — Soft-retract the source profile.
    source.deleted_at        = now
    source.retraction_reason = (
        f"MERGED: This profile was merged into patient {target_id} "
        f"by admin {admin_user.email} on {now.date().isoformat()}. "
        f"Reason: {reason}"
    )
    source.save(update_fields=["deleted_at", "retraction_reason", "updated_at"])

    # Step 4 — Write audit log entry on commit.
    # Imported here to respect the dependency rule:
    # patients/ must never import audit/ at module level.
    def _write_audit():
        try:
            from audit.models import AuditLog, AuditAction
            AuditLog.objects.create(
                user          = admin_user,
                patient       = target,
                action        = AuditAction.MERGE,
                resource_type = "Patient",
                resource_id   = source_id,
                metadata      = {
                    "source_patient_id":  str(source_id),
                    "target_patient_id":  str(target_id),
                    "events_reassigned":  events_reassigned,
                    "accesses_revoked":   source_accesses.count() if hasattr(source_accesses, "count") else "unknown",
                    "admin_email":        admin_user.email,
                    "reason":             reason,
                },
                description=(
                    f"Patient profile {source_id} merged into {target_id} "
                    f"by {admin_user.email}. {events_reassigned} event(s) reassigned."
                ),
            )
        except Exception as e:
            logger.error("Failed to write merge audit log: %s", e)

    transaction.on_commit(_write_audit)

    logger.info(
        "Patient profiles merged. source=%s target=%s events_reassigned=%s admin=%s",
        source_id, target_id, events_reassigned, admin_user.email,
    )

    return target


# ===========================================================================
# ACCESS REQUESTS
# ===========================================================================

_ACCESS_REQUEST_EXPIRY_DAYS = 7   # Patient has 7 days to respond before auto-expiry


@transaction.atomic
def request_access(user, patient_id: UUID, data) -> AccessRequest:
    """
    A user requests access to a patient's medical timeline.

    Rules:
      - requested_role must be caregiver or viewer — never primary/full_delegate
      - Only one pending request per (user, patient) pair at a time
      - Request itself expires after 7 days if patient does not respond
      - is_permanent=False requires access_duration_days to be set
      - User cannot request access to their own profile
      - User cannot request access if they already have active access

    Does NOT check patient consent — that is the patient's approval step.
    """
    # Verify patient exists and is active (no access required to find the patient —
    # the request IS the ask for access, so we only check the patient exists)
    try:
        patient = Patient.objects.get(pk=patient_id, deleted_at__isnull=True)
    except Patient.DoesNotExist:
        raise PatientNotFound()

    # Cannot request access to your own profile
    existing_own = PatientUserAccess.objects.filter(
        user=user,
        patient=patient,
        is_active=True,
    ).first()
    if existing_own:
        raise DuplicateAccessError(
            f"You already have active {existing_own.role} access to this profile."
        )

    # One pending request per (user, patient) at a time
    if AccessRequest.objects.filter(
        patient=patient,
        requested_by=user,
        status=AccessRequestStatus.PENDING,
    ).exists():
        raise DuplicatePendingRequest()

    now = timezone.now()
    request_obj = AccessRequest.objects.create(
        patient              = patient,
        requested_by         = user,
        requested_role       = data.requested_role,
        reason               = data.reason,
        is_permanent         = data.is_permanent,
        access_duration_days = data.access_duration_days if not data.is_permanent else None,
        request_expires_at   = now + timezone.timedelta(days=_ACCESS_REQUEST_EXPIRY_DAYS),
        status               = AccessRequestStatus.PENDING,
    )

    logger.info(
        "Access request created. patient_id=%s requested_by=%s role=%s permanent=%s",
        patient_id, user.id, data.requested_role, data.is_permanent,
    )

    return request_obj


def list_access_requests(user, patient_id: UUID, status_filter: str = None):
    """
    List access requests for a patient profile.
    Only the primary holder (or delegate on unclaimed profiles) can see all requests.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_manage(access)

    qs = (
        AccessRequest.objects
        .filter(patient_id=patient_id)
        .select_related("requested_by", "responded_by")
        .order_by("-created_at")
    )

    if status_filter:
        qs = qs.filter(status=status_filter)

    # Mark expired requests on read — lazy expiry, no background task required
    now = timezone.now()
    expired_ids = [
        r.id for r in qs
        if r.status == AccessRequestStatus.PENDING and r.request_expires_at < now
    ]
    if expired_ids:
        AccessRequest.objects.filter(id__in=expired_ids).update(
            status=AccessRequestStatus.EXPIRED
        )

    return qs


def list_my_sent_requests(user) -> list:
    """
    List all access requests the user has sent, across all patients.
    Used by the requester to track status of their own requests.
    """
    return (
        AccessRequest.objects
        .filter(requested_by=user)
        .select_related("patient")
        .order_by("-created_at")
    )


@transaction.atomic
def approve_access_request(user, patient_id: UUID, request_id: UUID) -> AccessRequest:
    """
    Patient approves an access request.
    Creates a PatientUserAccess record with optional expiry.
    Links the resulting access back to the request for audit traceability.

    Only the primary holder (or delegate on unclaimed profiles) can approve.
    """
    actor_access = _get_active_access(user, patient_id)
    _assert_can_manage(actor_access)

    try:
        req = (
            AccessRequest.objects
            .select_for_update()
            .get(id=request_id, patient_id=patient_id)
        )
    except AccessRequest.DoesNotExist:
        raise AccessRequestNotFound()

    # Lazy expiry check
    if req.is_expired:
        req.status = AccessRequestStatus.EXPIRED
        req.save(update_fields=["status", "updated_at"])
        raise AccessRequestExpired()

    if not req.is_pending:
        raise AccessRequestNotPending(
            f"This request is already {req.status}."
        )

    now = timezone.now()

    # Calculate access expiry
    access_expires_at = None
    if not req.is_permanent and req.access_duration_days:
        access_expires_at = now + timezone.timedelta(days=req.access_duration_days)

    # Check if requester already has access (edge case — they may have gotten
    # access via another route between request creation and approval)
    existing = PatientUserAccess.objects.filter(
        user=req.requested_by,
        patient_id=patient_id,
        is_active=True,
    ).first()
    if existing:
        raise DuplicateAccessError(
            f"This user already has active {existing.role} access. "
            "Revoke it before approving this request."
        )

    # Create the access record
    new_access = PatientUserAccess.objects.create(
        user         = req.requested_by,
        patient_id   = patient_id,
        role         = req.requested_role,
        claim_method = ClaimMethod.SYSTEM_CREATED,
        trust_level  = TrustLevel.DELEGATE_GRANTED,
        granted_by   = user,
        notes        = f"Approved via access request {request_id}.",
    )

    # Stamp the request as approved and link back
    req.status           = AccessRequestStatus.APPROVED
    req.responded_at     = now
    req.responded_by     = user
    req.access_expires_at = access_expires_at
    req.resulting_access  = new_access
    req.save(update_fields=[
        "status", "responded_at", "responded_by",
        "access_expires_at", "resulting_access", "updated_at",
    ])

    logger.info(
        "Access request approved. request_id=%s patient_id=%s "
        "requester=%s role=%s permanent=%s",
        request_id, patient_id, req.requested_by_id,
        req.requested_role, req.is_permanent,
    )

    return req


@transaction.atomic
def deny_access_request(
    user, patient_id: UUID, request_id: UUID, reason: str = None
) -> AccessRequest:
    """
    Patient denies an access request.
    Optional reason is shared with the requester.
    """
    actor_access = _get_active_access(user, patient_id)
    _assert_can_manage(actor_access)

    try:
        req = (
            AccessRequest.objects
            .select_for_update()
            .get(id=request_id, patient_id=patient_id)
        )
    except AccessRequest.DoesNotExist:
        raise AccessRequestNotFound()

    if req.is_expired:
        req.status = AccessRequestStatus.EXPIRED
        req.save(update_fields=["status", "updated_at"])
        raise AccessRequestExpired()

    if not req.is_pending:
        raise AccessRequestNotPending(f"This request is already {req.status}.")

    now = timezone.now()
    req.status        = AccessRequestStatus.DENIED
    req.responded_at  = now
    req.responded_by  = user
    req.denial_reason = reason
    req.save(update_fields=[
        "status", "responded_at", "responded_by", "denial_reason", "updated_at",
    ])

    logger.info(
        "Access request denied. request_id=%s patient_id=%s requester=%s",
        request_id, patient_id, req.requested_by_id,
    )

    return req


@transaction.atomic
def cancel_access_request(user, patient_id: UUID, request_id: UUID) -> AccessRequest:
    """
    Requester cancels their own pending access request.
    Only the original requester can cancel.
    """
    try:
        req = (
            AccessRequest.objects
            .select_for_update()
            .get(id=request_id, patient_id=patient_id, requested_by=user)
        )
    except AccessRequest.DoesNotExist:
        raise AccessRequestNotFound()

    if not req.is_pending:
        raise AccessRequestNotPending(
            f"Only pending requests can be cancelled. This request is {req.status}."
        )

    req.status = AccessRequestStatus.CANCELLED
    req.save(update_fields=["status", "updated_at"])

    logger.info(
        "Access request cancelled by requester. request_id=%s user_id=%s",
        request_id, user.id,
    )

    return req


@transaction.atomic
def revoke_approved_request(
    user, patient_id: UUID, request_id: UUID, reason: str
) -> AccessRequest:
    """
    Patient revokes access that was previously approved via a request.

    This is distinct from revoke_access() — it revokes by request ID,
    finds the resulting PatientUserAccess, and closes both atomically.
    Maintains full bidirectional traceability between request and access.
    """
    actor_access = _get_active_access(user, patient_id)
    _assert_can_manage(actor_access)

    try:
        req = (
            AccessRequest.objects
            .select_for_update()
            .select_related("resulting_access")
            .get(id=request_id, patient_id=patient_id, status=AccessRequestStatus.APPROVED)
        )
    except AccessRequest.DoesNotExist:
        raise AccessRequestNotFound(
            "No approved access request found with this ID."
        )

    now = timezone.now()

    # Revoke the PatientUserAccess record if it still exists and is active
    if req.resulting_access and req.resulting_access.is_active:
        # Orphan protection
        Patient.objects.select_for_update().get(pk=patient_id)
        if _active_holder_count(req.resulting_access.patient) <= 1:
            raise OrphanProtectionError()

        pua = req.resulting_access
        pua.is_active         = False
        pua.revoked_at        = now
        pua.revoked_by        = user
        pua.revocation_reason = reason
        pua.save(update_fields=[
            "is_active", "revoked_at", "revoked_by", "revocation_reason", "updated_at"
        ])

    # Mark the request as revoked
    req.status            = AccessRequestStatus.REVOKED
    req.revoked_at        = now
    req.revoked_by        = user
    req.revocation_reason = reason
    req.save(update_fields=[
        "status", "revoked_at", "revoked_by", "revocation_reason", "updated_at",
    ])

    logger.info(
        "Approved access request revoked. request_id=%s patient_id=%s by user_id=%s",
        request_id, patient_id, user.id,
    )

    return req