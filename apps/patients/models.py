"""
patients/models.py
==================
Pure medical identity layer. Nothing else.

Models:
  - Patient           — the canonical medical identity (sovereign, not user-owned)
  - PatientUserAccess — every user→patient access relationship, current and historical

Dependency rule:
  patients/ depends on → users/ (AUTH_USER_MODEL)
  patients/ is depended on by → integrations/, claims/, audit/, medical_events/

  This app must never import from integrations/, claims/, or audit/.
  Cross-app FKs from PatientUserAccess use Django string references to avoid
  circular imports while preserving DB-level referential integrity.
"""

import uuid
from datetime import date
from django.db import models
from django.db.models import Case, ExpressionWrapper, F, IntegerField, Value, When
from django.db.models.functions import ExtractDay, ExtractMonth, ExtractYear, Now
from django.conf import settings


# ===========================================================================
# CHOICES
# ===========================================================================

class Gender(models.TextChoices):
    """
    FHIR R4 AdministrativeGender values.
    https://www.hl7.org/fhir/valueset-administrative-gender.html
    """
    MALE    = "male",    "Male"
    FEMALE  = "female",  "Female"
    OTHER   = "other",   "Other"
    UNKNOWN = "unknown", "Unknown"


class AccessRole(models.TextChoices):
    """
    The role a user holds against a patient profile.

    Hierarchy (descending authority):

      primary        The patient themselves, proven via identity claim.
                     Full sovereign control: read, write, manage access,
                     approve/hide events, grant/revoke all other roles.
                     Can ONLY be assigned by the claim system — never manually.

      full_delegate  A parent or guardian who created the profile before the
                     patient could claim it. Full read + write access.
                     Can manage access ONLY while no primary holder exists.
                     Authority automatically reduces once primary is claimed.

      caregiver      Read + write. Cannot manage access or approve events.
                     Typical: adult child managing elderly parent, spouse,
                     professional carer.

      viewer         Read-only. Cannot write, cannot manage access.
                     Typical: a family member the patient wants informed.
    """
    PRIMARY       = "primary",       "Primary (Patient — Identity Verified)"
    FULL_DELEGATE = "full_delegate",  "Full Delegate (Guardian / Pre-Claim)"
    CAREGIVER     = "caregiver",      "Caregiver (Read + Write)"
    VIEWER        = "viewer",         "Viewer (Read Only)"


class ClaimMethod(models.TextChoices):
    """
    How a PatientUserAccess record was established.
    Set once at creation. Never changed.
    """
    NATIONAL_ID_MATCH = "national_id_match", "Tier 1 — National ID Match (Automated)"
    EMAIL_OTP         = "email_otp",          "Tier 2 — Email OTP (Automated)"
    SUPPORT_MANUAL    = "support_manual",     "Tier 3 — Support-Assisted Manual Verification"
    DELEGATE_TRANSFER = "delegate_transfer",  "Delegate-Initiated Transfer (Invitation)"
    SYSTEM_CREATED    = "system_created",     "System Created (Profile Creation)"


class TrustLevel(models.TextChoices):
    """
    Confidence level of the claim that established an access record.
    Persists permanently on the record.
    Used by downstream systems to assess primary holder identity reliability.
    """
    VERIFIED_IDENTITY = "verified_identity", "Verified Identity (National ID)"
    EMAIL_VERIFIED    = "email_verified",    "Email Verified (OTP)"
    SUPPORT_VERIFIED  = "support_verified",  "Support Verified (Manual)"
    DELEGATE_GRANTED  = "delegate_granted",  "Delegate Granted (No independent verification)"
    SYSTEM            = "system",            "System (Auto-created, pre-claim)"


# ===========================================================================
# MODEL: PATIENT
# ===========================================================================

class Patient(models.Model):
    """
    The canonical medical identity in UHR.

    Architectural rules:
      - The profile is sovereign. It is not owned by the user who created it.
      - The real patient proves their identity (via the claims app) to become primary.
      - All medical events reference patient_id only. Never user_id.
      - No hard deletes. Retract via deleted_at + retraction_reason (invariant 12.1).
      - External / national health IDs live in integrations.ExternalPatientIdentity.
        This table has no columns for specific national ID systems.

    Claim state:
      is_claimed=False  → profile exists but the patient themselves has not yet
                          taken primary control (e.g. parent-created child profile).
      is_claimed=True   → the patient has proven their identity and holds
                          role=primary in PatientUserAccess.

    transfer_eligible_at:
      For minor profiles: set to the child's 18th birthday at creation time.
      The system surfaces a transfer prompt to both parties on this date,
      enabling a smooth handover without any manual admin intervention.

    FHIR R4: Patient resource.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Internal UHR-assigned MRN. Separate from any national health ID.
    # All external / national IDs live in integrations.ExternalPatientIdentity.
    mrn = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text=(
            "UHR-internal Medical Record Number. "
            "National / external IDs belong in integrations.ExternalPatientIdentity."
        ),
    )

    # ── Demographics ─────────────────────────────────────────────────────────
    first_name = models.CharField(max_length=100)
    last_name  = models.CharField(max_length=100)
    gender     = models.CharField(max_length=10, choices=Gender.choices, default=Gender.UNKNOWN)
    birth_date = models.DateField()

    # ── Extended Demographics (optional) ─────────────────────────────────────
    phone       = models.CharField(max_length=20, null=True, blank=True)

    # IMPORTANT: This email belongs to the PATIENT PROFILE, not the user's login.
    # It is the target for Tier 2 OTP during a claim flow.
    # When a parent creates a child's profile, they should update this to the
    # child's own email to enable the child to self-claim via Tier 2 later.
    email       = models.EmailField(null=True, blank=True)

    address     = models.TextField(null=True, blank=True)
    blood_group = models.CharField(max_length=10, null=True, blank=True)

    # ISO 3166-1 alpha-2 country code.
    # Used in FHIR exports and to surface relevant identity systems in the UI.
    nationality = models.CharField(
        max_length=2,
        null=True,
        blank=True,
        help_text="ISO 3166-1 alpha-2 country code (e.g. IN, AU, GB, DE, US).",
    )

    # ── Deceased Status ───────────────────────────────────────────────────────
    # FHIR: Patient.deceased[x]
    is_deceased   = models.BooleanField(default=False)
    deceased_date = models.DateField(null=True, blank=True)

    # ── Claim State ───────────────────────────────────────────────────────────
    is_claimed = models.BooleanField(
        default=False,
        help_text=(
            "True once the real patient has proven their identity "
            "and holds role=primary in PatientUserAccess."
        ),
    )
    claimed_at = models.DateTimeField(null=True, blank=True)

    transfer_eligible_at = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Date from which the patient may independently claim this profile. "
            "For minor profiles: set to the patient's 18th birthday at creation. "
            "DateField (not DateTime) — eligibility is calendar-date based, "
            "not time-of-day precise. Compare against date.today() in service layer. "
            "System surfaces a transfer prompt to both parties on this date."
        ),
    )

    # ── Soft Delete (Schema Invariant 12.1) ───────────────────────────────────
    # Records are NEVER physically deleted.
    # On retraction: set both fields together in a single atomic update.
    deleted_at        = models.DateTimeField(null=True, blank=True)
    retraction_reason = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "patients"
        db_table  = "patients"
        constraints = [
            # Deceased consistency:
            # If is_deceased=True, deceased_date must not be null.
            # Enforced at DB level so no code path — service layer, admin,
            # raw queryset update, or migration — can leave an inconsistent row.
            models.CheckConstraint(
                check=(
                    models.Q(is_deceased=False)
                    | models.Q(is_deceased=True, deceased_date__isnull=False)
                ),
                name="chk_patient_deceased_consistency",
            ),
        ]
        indexes   = [
            models.Index(fields=["birth_date"],           name="idx_patient_birth_date"),
            models.Index(fields=["deleted_at"],           name="idx_patient_deleted_at"),
            models.Index(fields=["nationality"],          name="idx_patient_nationality"),
            models.Index(fields=["is_claimed"],           name="idx_patient_is_claimed"),
            models.Index(fields=["transfer_eligible_at"], name="idx_patient_transfer_eligible"),
            # Partial index on email — only indexes rows where email is set.
            # Patient email is sparse (many profiles have no email), so a full
            # index would waste space and slow writes for the majority of rows.
            # Used for Tier 2 claim OTP lookup (send OTP to patient profile email).
            models.Index(
                fields=["email"],
                name="idx_patient_email",
                condition=models.Q(email__isnull=False),
            ),
        ]
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name} [{self.id}]"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def is_active(self) -> bool:
        """False if the profile has been soft-deleted / retracted."""
        return self.deleted_at is None

    @property
    def age(self) -> int | None:
        """
        Calculates the patient's current age in years from birth_date.

        Returns None if:
          - birth_date is not set
          - the patient is deceased (age at death is not meaningful for active queries)

        Uses Python date arithmetic — correct across leap years and timezone edges.
        The has_had_birthday_this_year check ensures the age is exact:
          e.g. a patient born on Dec 15 2000 is still 23 on Dec 14 2024, not 24.

        For DB-level age filtering (search / queryset), use
        Patient.objects.with_age() which annotates age at the database level.
        Python-level property is for display only — never use it inside a loop
        over a large queryset (N+1 issue).
        """
        if not self.birth_date:
            return None
        if self.is_deceased:
            return None

        today = date.today()
        has_had_birthday_this_year = (
            today.month,
            today.day,
        ) >= (
            self.birth_date.month,
            self.birth_date.day,
        )
        return today.year - self.birth_date.year - (not has_had_birthday_this_year)

    @property
    def age_at_death(self) -> int | None:
        """
        Age the patient was when they died.
        Only meaningful when is_deceased=True and deceased_date is set.
        Useful for clinical and epidemiological reporting.
        """
        if not self.is_deceased or not self.deceased_date or not self.birth_date:
            return None
        has_had_birthday = (
            self.deceased_date.month,
            self.deceased_date.day,
        ) >= (
            self.birth_date.month,
            self.birth_date.day,
        )
        return self.deceased_date.year - self.birth_date.year - (not has_had_birthday)

    @classmethod
    def with_age(cls):
        """
        Returns a queryset annotated with `age_years` computed at the DB level.

        Use this for all age-based filtering and searching — it pushes the
        calculation into PostgreSQL so it works correctly across large datasets
        without loading records into Python.

        ⚠️  Deceased patient behaviour:
            This annotation computes age for ALL patients regardless of
            is_deceased status — it does not exclude deceased profiles.
            This is intentional: age_years on deceased patients represents
            "age as of today" not "age at death".

            Differences from the Python age property:
              Python property  → returns None for deceased patients
              DB annotation    → computes age regardless of deceased status

            For queries where deceased patients should be excluded:
              Patient.with_age().filter(is_deceased=False, age_years__lt=18)

            For age-at-death, use the Python age_at_death property on
            individual records — a DB-level age_at_death annotation is not
            provided here as it is not a search field.

        Usage examples:

            # All minors (living only)
            Patient.with_age().filter(is_deceased=False, age_years__lt=18)

            # Unclaimed profiles eligible for transfer
            Patient.with_age().filter(
                is_deceased=False,
                is_claimed=False,
                age_years__gte=18,
            )

            # Age bracket search
            Patient.with_age().filter(age_years__gte=40, age_years__lte=60)

            # Order by age
            Patient.with_age().order_by("age_years")

        How it works:
          PostgreSQL DATE_PART / EXTRACT gives us the year difference.
          We subtract 1 if the patient hasn't had their birthday yet this year,
          mirroring the exact same logic as the Python age property above.

        Note: age_years is an approximation at the DB level — it uses
        EXTRACT(year) arithmetic which is correct for integer age in years.
        For day-precise calculations (e.g. age in days for neonates),
        use a raw annotation with DATE_PART('day', NOW() - birth_date).
        """
        return cls.objects.annotate(
            # Step 1: raw year difference
            _year_diff=ExpressionWrapper(
                ExtractYear(Now()) - ExtractYear(F("birth_date")),
                output_field=IntegerField(),
            ),
            # Step 2: has the patient had their birthday yet this year?
            # True (1) = not yet had birthday → subtract 1 from year diff
            # False (0) = already had birthday → year diff is correct
            _birthday_correction=Case(
                When(
                    # Current month < birth month → definitely not had birthday yet
                    **{"birth_date__month__gt": ExtractMonth(Now())},
                    then=Value(1),
                ),
                When(
                    # Same month but current day < birth day → not had birthday yet
                    **{
                        "birth_date__month": ExtractMonth(Now()),
                        "birth_date__day__gt": ExtractDay(Now()),
                    },
                    then=Value(1),
                ),
                default=Value(0),
                output_field=IntegerField(),
            ),
            # Step 3: exact age = year difference minus birthday correction
            age_years=ExpressionWrapper(
                F("_year_diff") - F("_birthday_correction"),
                output_field=IntegerField(),
            ),
        )


# ===========================================================================
# MODEL: PATIENT USER ACCESS
# ===========================================================================

class PatientUserAccess(models.Model):
    """
    Every user→patient access relationship that has ever existed.

    This is the complete, permanent, append-aware history of who had access
    to which patient profile, under what role, how that access was established,
    and when / why it ended.

    Core rules:
      - is_active=False means revoked. Record is NEVER deleted.
      - role=primary can ONLY be assigned via the claims system (claim_method set).
        It must never be manually written by another user or admin.
      - role=full_delegate is auto-created when a parent/guardian creates a profile.
        It degrades in authority once a primary holder exists (see can_manage_access).
      - Only role=primary (or an admin) can grant / revoke other users' access.
      - Revocation must always record revoked_at + revoked_by + revocation_reason.
      - Re-granting after revocation creates a NEW record; old record stays intact.

    Orphan protection (enforced at service layer):
      - An unclaimed profile must always have at least one active full_delegate.
      - A claimed profile must always have exactly one active primary.
      - No revocation should proceed if it would violate these constraints.

    Claim evidence FKs:
      Exactly one of the three claim_* FKs is populated depending on claim_method.
      Tier 1 (national_id_match)  → claim_identity populated
      Tier 2 (email_otp)          → claim_otp populated
      Tier 3 (support_manual)     → claim_ticket populated
      system_created / delegate   → all null

      Cross-app FKs use Django string references ("app.Model") to maintain
      DB-level referential integrity without circular Python imports.

    FHIR: No mapping. UHR-native access-control construct.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="patient_accesses",
    )
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="user_accesses",
    )
    role = models.CharField(
        max_length=20,
        choices=AccessRole.choices,
        default=AccessRole.FULL_DELEGATE,
    )

    # ── Access State ──────────────────────────────────────────────────────────
    is_active = models.BooleanField(
        default=True,
        help_text="Set to False on revocation. Never delete this record.",
    )

    # ── Claim Provenance (immutable after creation) ───────────────────────────
    claim_method = models.CharField(
        max_length=32,
        choices=ClaimMethod.choices,
        default=ClaimMethod.SYSTEM_CREATED,
        help_text="How this access was established. Set once. Never changed.",
    )
    trust_level = models.CharField(
        max_length=32,
        choices=TrustLevel.choices,
        default=TrustLevel.SYSTEM,
        help_text="Confidence level of the establishing claim. Set once. Never changed.",
    )

    # ── Claim Evidence FKs (cross-app, one populated per record) ─────────────
    claim_identity = models.ForeignKey(
        "integrations.ExternalPatientIdentity",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="access_claims",
        help_text="Tier 1: the ExternalPatientIdentity match that proved this claim.",
    )
    claim_otp = models.ForeignKey(
        "claims.ProfileClaimOTP",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="access_claims",
        help_text="Tier 2: the ProfileClaimOTP record that verified this claim.",
    )
    claim_ticket = models.ForeignKey(
        "claims.SupportClaimRequest",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="access_claims",
        help_text="Tier 3: the SupportClaimRequest that authorised this claim.",
    )

    # ── Grant Metadata ────────────────────────────────────────────────────────
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="granted_patient_accesses",
        null=True,
        blank=True,
        help_text="Who granted this access. Null for claim-initiated or self-created.",
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    # ── Revocation Metadata ───────────────────────────────────────────────────
    # Who revoked determines the type:
    #   revoked_by == self           → self-exit
    #   revoked_by == primary holder → patient-initiated revocation
    #   revoked_by == admin          → admin force-revoke (reason mandatory)
    revoked_at        = models.DateTimeField(null=True, blank=True)
    revoked_by        = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="revoked_patient_accesses",
        null=True,
        blank=True,
    )
    revocation_reason = models.TextField(null=True, blank=True)

    notes      = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "patients"
        db_table  = "patient_user_access"
        constraints = [
            # One ACTIVE record per (user, patient).
            # Multiple revoked records for the same pair are allowed.
            models.UniqueConstraint(
                fields=["user", "patient"],
                condition=models.Q(is_active=True),
                name="uq_pua_user_patient_active",
            ),
            # Only one active PRIMARY holder per patient at any time.
            models.UniqueConstraint(
                fields=["patient", "role"],
                condition=models.Q(role="primary", is_active=True),
                name="uq_pua_one_primary_per_patient",
            ),
            # Revocation consistency:
            # Active records must have no revoked_at.
            # Revoked records must have revoked_at set.
            # The service layer enforces revocation_reason — the DB enforces structure.
            # This prevents silent revocations (is_active=False with no timestamp)
            # which would be undetectable in audit queries.
            models.CheckConstraint(
                check=(
                    models.Q(is_active=True,  revoked_at__isnull=True)
                    | models.Q(is_active=False, revoked_at__isnull=False)
                ),
                name="chk_pua_revocation_consistency",
            ),
        ]
        indexes = [
            # All patients a user currently accesses
            models.Index(fields=["user", "is_active"],           name="idx_pua_user_active"),
            # All users who currently access a patient
            models.Index(fields=["patient", "is_active"],        name="idx_pua_patient_active"),
            # Role-filtered queries (e.g. find the primary holder)
            models.Index(fields=["patient", "role", "is_active"], name="idx_pua_patient_role"),
        ]

    def __str__(self):
        return (
            f"User {self.user_id} → Patient {self.patient_id} "
            f"[{self.role}] active={self.is_active}"
        )

    @property
    def is_primary(self) -> bool:
        return self.role == AccessRole.PRIMARY and self.is_active

    @property
    def can_manage_access(self) -> bool:
        """
        Whether this record grants authority to grant / revoke
        other users' access to this patient profile.
        """
        if self.role == AccessRole.PRIMARY:
            return True
        if self.role == AccessRole.FULL_DELEGATE:
            # Delegates can manage access only while the profile is unclaimed.
            return not self.patient.is_claimed
        return False

    @property
    def can_write(self) -> bool:
        """Whether this record grants write access to medical events."""
        return self.is_active and self.role in (
            AccessRole.PRIMARY,
            AccessRole.FULL_DELEGATE,
            AccessRole.CAREGIVER,
        )

    @property
    def can_read(self) -> bool:
        return self.is_active