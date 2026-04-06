"""
practitioners/models.py
========================
Healthcare practitioners and their organisational roles.

Models:
  - Practitioner          — a doctor or health provider with a UHR account
  - PractitionerRole      — time-bound affiliation with an organisation
  - OrgMembershipRequest  — request to join an organisation

Dependency rule:
  practitioners/ depends on → users/, organisations/
  practitioners/ is depended on by → visits/, medical_events/
  practitioners/ must never import from → patients/, claims/, audit/

FHIR R4: Practitioner + PractitionerRole resources.
  https://www.hl7.org/fhir/practitioner.html
  https://www.hl7.org/fhir/practitionerrole.html
"""

import uuid

from django.conf import settings
from django.db import models


# ===========================================================================
# CHOICES
# ===========================================================================

class VerificationSource(models.TextChoices):
    """How the practitioner's licence was verified."""
    ORG_ADMIN       = "org_admin",       "Verified by Org Admin"
    REGISTRY        = "registry",        "National Registry Lookup"
    NATIONAL_GATEWAY = "national_gateway", "National Health Gateway"
    SELF_REPORTED   = "self_reported",   "Self-Reported (Unverified)"


class MembershipRequestStatus(models.TextChoices):
    PENDING  = "pending",  "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled by Requester"


# ===========================================================================
# MODEL: PRACTITIONER
# ===========================================================================

class Practitioner(models.Model):
    """
    A doctor or health provider affiliated with a UHR user account.

    Registration flow:
      1. User creates a UHR account (users app)
      2. User creates a Practitioner profile (this model)
      3. Practitioner requests to join an Organisation (OrgMembershipRequest)
      4. Org admin approves → PractitionerRole created + is_verified set to True

    Verification:
      is_verified=True means an org admin has confirmed this person
      works at their organisation and has valid credentials.
      Only verified practitioners can:
        - Submit provider_verified medical events
        - Have their access counted in visit sessions
        - Appear in practitioner search

    FHIR R4: Practitioner resource.
      full_name           → Practitioner.name[].text
      gender              → Practitioner.gender
      birth_date          → Practitioner.birthDate
      license_number      → Practitioner.qualification[].identifier
      license_authority   → Practitioner.qualification[].issuer
      license_expires_at  → Practitioner.qualification[].period.end
      specialization      → Practitioner.qualification[].code
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="practitioner_profile",
        help_text="The UHR user account for this practitioner.",
    )

    # ── Identity (FHIR: Practitioner.name) ───────────────────────────────────
    full_name = models.CharField(max_length=200)
    gender    = models.CharField(
        max_length=10,
        null=True,
        blank=True,
        help_text="FHIR AdministrativeGender: male | female | other | unknown",
    )
    birth_date = models.DateField(
        null=True,
        blank=True,
        help_text="FHIR: Practitioner.birthDate",
    )

    # ── Qualification / Licence (FHIR: Practitioner.qualification) ────────────
    license_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="National medical licence or registration number.",
    )
    license_issuing_authority = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="e.g. Medical Council of India, GMC, AHPRA.",
    )
    license_expires_at = models.DateField(
        null=True,
        blank=True,
        help_text="Licence expiry date. Null if licence is permanent.",
    )
    specialization = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="e.g. Cardiology, General Practice, Radiology.",
    )
    qualification = models.TextField(
        null=True,
        blank=True,
        help_text="Freetext qualifications e.g. MBBS, MD, FRCS.",
    )

    # ── Verification ──────────────────────────────────────────────────────────
    # Set to True by the org admin when they approve a membership request.
    # Represents: "this person works at a verified org and has valid credentials."
    is_verified = models.BooleanField(
        default=False,
        help_text=(
            "True once an org admin has approved this practitioner's membership. "
            "Required for provider_verified medical events."
        ),
    )
    verified_at         = models.DateTimeField(null=True, blank=True)
    verification_source = models.CharField(
        max_length=20,
        choices=VerificationSource.choices,
        default=VerificationSource.SELF_REPORTED,
    )

    # ── State ─────────────────────────────────────────────────────────────────
    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "practitioners"
        db_table  = "practitioners"
        indexes = [
            models.Index(fields=["is_verified"],    name="idx_prac_verified"),
            models.Index(fields=["license_number"], name="idx_prac_license"),
            models.Index(fields=["is_active"],      name="idx_prac_active"),
        ]

    def __str__(self):
        return f"{self.full_name} [{self.specialization or 'General'}]"

    @property
    def primary_organisation(self):
        """
        Returns the practitioner's current primary active organisation.
        Used by the visit access check to confirm the practitioner
        belongs to the org the patient is visiting.
        """
        role = (
            self.roles
            .filter(is_active=True, is_primary=True)
            .select_related("organisation")
            .first()
        )
        return role.organisation if role else None

    @property
    def current_organisations(self):
        """All active organisations this practitioner is affiliated with."""
        return (
            self.roles
            .filter(is_active=True)
            .select_related("organisation")
            .values_list("organisation", flat=True)
        )


# ===========================================================================
# MODEL: PRACTITIONER ROLE
# ===========================================================================

class PractitionerRole(models.Model):
    """
    A time-bound affiliation between a practitioner and an organisation.

    Created automatically when an OrgMembershipRequest is approved.

    is_primary:
      A practitioner may be affiliated with multiple orgs.
      is_primary marks their main org — used in visit access checks.
      Only one active primary role per practitioner at a time (DB constraint).

    is_org_admin:
      Marks whether this practitioner has org admin privileges at this org.
      Org admins can approve membership requests and update org details.
      Set by UHR staff or existing org admin.

    FHIR R4: PractitionerRole resource.
      practitioner → PractitionerRole.practitioner
      organisation → PractitionerRole.organization
      role_title   → PractitionerRole.code
      department   → PractitionerRole.specialty
      start_date   → PractitionerRole.period.start
      end_date     → PractitionerRole.period.end
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    practitioner = models.ForeignKey(
        Practitioner,
        on_delete=models.PROTECT,
        related_name="roles",
    )
    organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        related_name="practitioner_roles",
    )

    # FHIR: PractitionerRole.code
    role_title = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="e.g. 'Consultant Cardiologist', 'Resident', 'GP'.",
    )

    # FHIR: PractitionerRole.specialty
    department = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="e.g. 'Cardiology', 'Emergency', 'Radiology'.",
    )

    # ── Period (FHIR: PractitionerRole.period) ────────────────────────────────
    start_date = models.DateField()
    end_date   = models.DateField(null=True, blank=True)
    is_active  = models.BooleanField(default=True)

    # ── Flags ─────────────────────────────────────────────────────────────────
    is_primary   = models.BooleanField(
        default=False,
        help_text="Primary affiliation. Used in visit access checks.",
    )
    is_org_admin = models.BooleanField(
        default=False,
        help_text=(
            "Org admin privileges at this organisation. "
            "Can approve membership requests and update org details."
        ),
    )

    # ── Provenance ────────────────────────────────────────────────────────────
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_practitioner_roles",
        help_text="The org admin who approved the membership request.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "practitioners"
        db_table  = "practitioner_roles"
        constraints = [
            # One active record per (practitioner, organisation)
            models.UniqueConstraint(
                fields=["practitioner", "organisation"],
                condition=models.Q(is_active=True),
                name="uq_prac_role_active_per_org",
            ),
            # Only one active primary role per practitioner
            models.UniqueConstraint(
                fields=["practitioner", "is_primary"],
                condition=models.Q(is_active=True, is_primary=True),
                name="uq_prac_role_one_primary",
            ),
        ]
        indexes = [
            models.Index(
                fields=["practitioner", "is_active"],
                name="idx_prac_role_active",
            ),
            models.Index(
                fields=["organisation", "is_active"],
                name="idx_prac_role_org_active",
            ),
            models.Index(
                fields=["organisation", "is_org_admin", "is_active"],
                name="idx_prac_role_org_admin",
            ),
        ]

    def __str__(self):
        return (
            f"{self.practitioner.full_name} @ "
            f"{self.organisation.name} "
            f"[{self.role_title or 'No title'}]"
            f"{' (admin)' if self.is_org_admin else ''}"
        )


# ===========================================================================
# MODEL: ORG MEMBERSHIP REQUEST
# ===========================================================================

class OrgMembershipRequest(models.Model):
    """
    A practitioner's request to join an organisation.

    Flow:
      1. Practitioner submits request with their intended role title
      2. Org admin sees pending requests
      3. Org admin approves → PractitionerRole created atomically
         + practitioner.is_verified set to True
         + practitioner.verification_source set to org_admin
      4. Org admin rejects → request marked rejected, reason recorded

    One pending request per (practitioner, organisation) at a time.
    After rejection, practitioner can request again.
    After approval, creating a new request is blocked (already a member).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    practitioner = models.ForeignKey(
        Practitioner,
        on_delete=models.PROTECT,
        related_name="membership_requests",
    )
    organisation = models.ForeignKey(
        "organisations.Organisation",
        on_delete=models.PROTECT,
        related_name="membership_requests",
    )

    # ── Request details ───────────────────────────────────────────────────────
    requested_role_title = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="The role the practitioner intends to hold at this org.",
    )
    requested_department = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    message = models.TextField(
        null=True,
        blank=True,
        help_text="Optional message from the practitioner to the org admin.",
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=20,
        choices=MembershipRequestStatus.choices,
        default=MembershipRequestStatus.PENDING,
    )

    # ── Response ──────────────────────────────────────────────────────────────
    responded_at  = models.DateTimeField(null=True, blank=True)
    responded_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="responded_membership_requests",
        help_text="The org admin who approved or rejected this request.",
    )
    rejection_reason = models.TextField(
        null=True,
        blank=True,
        help_text="Reason shown to practitioner on rejection.",
    )

    # ── Resulting role ────────────────────────────────────────────────────────
    resulting_role = models.OneToOneField(
        PractitionerRole,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_request",
        help_text="The PractitionerRole created when this request was approved.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "practitioners"
        db_table  = "org_membership_requests"
        constraints = [
            # One pending request per (practitioner, organisation)
            models.UniqueConstraint(
                fields=["practitioner", "organisation"],
                condition=models.Q(status="pending"),
                name="uq_membership_req_one_pending",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organisation", "status"],
                name="idx_membership_req_org_status",
            ),
            models.Index(
                fields=["practitioner", "status"],
                name="idx_membership_req_prac_status",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"MembershipRequest: {self.practitioner.full_name} → "
            f"{self.organisation.name} [{self.status}]"
        )

    @property
    def is_pending(self) -> bool:
        return self.status == MembershipRequestStatus.PENDING