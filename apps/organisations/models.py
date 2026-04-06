"""
organisations/models.py
========================
Healthcare organisations — hospitals, clinics, labs, and pharmacies.

Models:
  - Organisation — a verified healthcare entity (FHIR R4: Organization)

Dependency rule:
  organisations/ depends on → users/ only
  organisations/ is depended on by → practitioners/, visits/, share/
  organisations/ must never import from → patients/, claims/, audit/

FHIR R4: Organization resource.
  https://www.hl7.org/fhir/organization.html
"""

import uuid

from django.conf import settings
from django.db import models


# ===========================================================================
# CHOICES
# ===========================================================================

class OrganisationType(models.TextChoices):
    """
    FHIR R4: Organization.type (CodeableConcept)
    Aligned with HL7 OrganizationType value set.
    """
    HOSPITAL      = "hospital",      "Hospital"
    CLINIC        = "clinic",        "Clinic / Outpatient Centre"
    LAB           = "lab",           "Diagnostic Laboratory"
    PHARMACY      = "pharmacy",      "Pharmacy"
    TELEHEALTH    = "telehealth",    "Telehealth Provider"
    IMAGING       = "imaging",       "Imaging / Radiology Centre"
    DENTAL        = "dental",        "Dental Practice"
    MENTAL_HEALTH = "mental_health", "Mental Health Centre"
    OTHER         = "other",         "Other"


# ===========================================================================
# MODEL: ORGANISATION
# ===========================================================================

class Organisation(models.Model):
    """
    A verified healthcare entity — hospital, clinic, lab, or pharmacy.

    Lifecycle:
      Registration → unverified (verified=False)
      UHR admin reviews → verified=True
      Org can now:
        - Accept practitioner membership requests
        - Appear in patient visit flows
        - Generate org QR codes

    Org admin:
      Any practitioner with an active PractitionerRole at this org
      who has is_org_admin=True on their role record.
      Org admins can approve/reject practitioner membership requests
      and update org contact details.
      Only UHR admin (is_staff=True) can verify the org itself.

    Parent organisation:
      Supports hospital branch hierarchies.
      e.g. Apollo Hospital (parent) → Apollo Clinic Bandra (child)

    FHIR R4: Organization resource.
      name            → Organization.name
      type            → Organization.type
      registration_number → Organization.identifier[].value
      email/phone     → Organization.telecom[]
      address         → Organization.address[]
      country         → Organization.address[].country
      parent          → Organization.partOf
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── Identity ──────────────────────────────────────────────────────────────
    name = models.CharField(
        max_length=255,
        help_text="Official registered name of the organisation.",
    )
    type = models.CharField(
        max_length=20,
        choices=OrganisationType.choices,
        default=OrganisationType.HOSPITAL,
    )
    registration_number = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Official government or health authority registration number.",
    )
    description = models.TextField(null=True, blank=True)
    website     = models.URLField(null=True, blank=True)

    # ── Hierarchy (FHIR: Organization.partOf) ─────────────────────────────────
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="branches",
        help_text="Parent organisation for branch hierarchies. Null for standalone.",
    )

    # ── Contact ───────────────────────────────────────────────────────────────
    email   = models.EmailField(null=True, blank=True)
    phone   = models.CharField(max_length=20, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    country = models.CharField(
        max_length=2,
        null=True,
        blank=True,
        help_text="ISO 3166-1 alpha-2 country code (e.g. IN, AU, GB).",
    )

    # ── Verification (UHR admin only) ─────────────────────────────────────────
    verified    = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verified_organisations",
        help_text="The UHR staff user who verified this organisation.",
    )

    # ── Soft delete ───────────────────────────────────────────────────────────
    is_active           = models.BooleanField(default=True)
    deactivated_at      = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "organisations"
        db_table  = "organisations"
        constraints = [
            models.UniqueConstraint(
                fields=["registration_number", "country"],
                condition=models.Q(registration_number__isnull=False),
                name="uq_org_reg_number_country",
            ),
        ]
        indexes = [
            models.Index(fields=["verified", "is_active"], name="idx_org_verified_active"),
            models.Index(fields=["country"],               name="idx_org_country"),
            models.Index(fields=["type"],                  name="idx_org_type"),
            models.Index(fields=["parent"],                name="idx_org_parent"),
        ]
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} [{self.get_type_display()}]"

    @property
    def is_operational(self) -> bool:
        """True if the org is active and verified — can host visits and appear in QR flows."""
        return self.is_active and self.verified