"""
integrations/models.py
======================
All external national / international health identity system integrations.

Models:
  - ExternalPatientIdentity — links a patient profile to a national health ID

Supporting registries (not models):
  - IdentitySystem   — enum of all supported national ID systems
  - FHIR_SYSTEM_URIS — official FHIR Identifier.system URIs per system
  - SYSTEM_FORMAT_HINTS — human-readable format descriptions for validation

Dependency rule:
  integrations/ depends on → patients/
  integrations/ must never import from → claims/, audit/

Extension model:
  Adding a new national ID system requires only:
    1. A new value in IdentitySystem
    2. Its FHIR URI in FHIR_SYSTEM_URIS
    3. Optionally a format hint in SYSTEM_FORMAT_HINTS
  Zero schema changes. Zero migrations. Zero changes to patients/.
"""

import uuid
from django.db import models


# ===========================================================================
# IDENTITY SYSTEM REGISTRY
# ===========================================================================

class IdentitySystem(models.TextChoices):
    """
    All national / international health identity systems supported by UHR.

    Naming convention: <ISO_3166_ALPHA2>_<SYSTEM_ABBREVIATION>

    Coverage: 27 systems across 20+ countries.
    South Asia, Oceania, UK & Ireland, Europe (8 countries),
    Middle East, East Asia, North America, Latin America, Africa.
    """

    # ── South Asia ──────────────────────────────────────────────────────────
    # India: Ayushman Bharat Health Account
    # 14-digit number + optional @abdm address handle
    # Managed by National Health Authority (NHA) under ABDM
    IN_ABHA         = "in_abha",         "ABHA — India (Ayushman Bharat Health Account)"

    # ── Oceania ─────────────────────────────────────────────────────────────
    # Australia: Individual Healthcare Identifier
    # 16-digit number assigned by the HI Service
    AU_IHI          = "au_ihi",          "IHI — Australia (Individual Healthcare Identifier)"

    # New Zealand: National Health Index
    # LLLNNNN (legacy) or LLLNNLX (post-2022)
    NZ_NHI          = "nz_nhi",          "NHI — New Zealand (National Health Index)"

    # ── United Kingdom & Ireland ─────────────────────────────────────────────
    # England, Wales, Isle of Man: NHS Number (10-digit)
    UK_NHS          = "uk_nhs",          "NHS Number — England / Wales / Isle of Man"

    # Scotland: Community Health Index Number
    UK_CHI          = "uk_chi",          "CHI Number — Scotland"

    # Northern Ireland: Health and Care Number
    UK_HC           = "uk_hc",           "H&C Number — Northern Ireland"

    # Republic of Ireland: Individual Health Identifier
    IE_IHI          = "ie_ihi",          "IHI — Ireland (Individual Health Identifier)"

    # ── Europe ───────────────────────────────────────────────────────────────
    # Switzerland: EPR-SPID (Electronic Patient Record)
    CH_EPR_SPID     = "ch_epr_spid",     "EPR-SPID — Switzerland"

    # Germany: Krankenversichertennummer (Health Insurance Number)
    DE_KVNR         = "de_kvnr",         "KVNR — Germany (Krankenversichertennummer)"

    # France: NIR / Numéro de Sécurité Sociale (INSEE)
    FR_NIR          = "fr_nir",          "NIR — France (Numéro de Sécurité Sociale)"

    # Netherlands: BSN (Burgerservicenummer) used in healthcare
    NL_BSN          = "nl_bsn",          "BSN — Netherlands (Burgerservicenummer)"

    # Denmark: CPR-nummer (Civil Registration Number)
    DK_CPR          = "dk_cpr",          "CPR — Denmark (Civil Registration Number)"

    # Sweden: Personnummer used in healthcare
    SE_PERSONNUMMER = "se_personnummer",  "Personnummer — Sweden"

    # Norway: Fødselsnummer
    NO_FNR          = "no_fnr",          "Fødselsnummer — Norway"

    # Finland: Henkilötunnus
    FI_HETU         = "fi_hetu",         "Hetu — Finland (Henkilötunnus)"

    # Estonia: Personal Identification Code (used in e-Health / X-Road)
    EE_PIC          = "ee_pic",          "PIC — Estonia (Personal Identification Code)"

    # ── Middle East ──────────────────────────────────────────────────────────
    # Saudi Arabia: National Health ID via NHC / SEHA
    SA_NHI          = "sa_nhi",          "NHI — Saudi Arabia (National Health ID)"

    # UAE: Unified Health Identifier
    # Use identity_region for issuing authority: HAAD | DHA | DOH
    AE_UHI          = "ae_uhi",          "UHI — UAE (Unified Health Identifier)"

    # ── East Asia ────────────────────────────────────────────────────────────
    # South Korea: Resident Registration Number (used in NHI system)
    KR_RRN          = "kr_rrn",          "RRN — South Korea (Resident Registration Number)"

    # Japan: My Number (マイナンバー) — used in health insurance since 2021
    JP_MYNUMBER     = "jp_mynumber",     "My Number — Japan"

    # Singapore: NRIC / FIN (used in MOH healthcare system)
    SG_NRIC         = "sg_nric",         "NRIC/FIN — Singapore"

    # ── North America ────────────────────────────────────────────────────────
    # Canada: Provincial Health Number (no single national ID exists)
    # Use identity_region for the province: BC | ON | QC | AB | etc.
    CA_PHN          = "ca_phn",          "PHN — Canada (Provincial Health Number)"

    # USA: No national patient ID. Stored as local MRN per facility.
    # Use identity_region for the issuing hospital / health system.
    US_MRN          = "us_mrn",          "MRN — United States (Local Medical Record Number)"

    # ── Latin America ────────────────────────────────────────────────────────
    # Brazil: CNS — Cartão Nacional de Saúde (SUS card)
    BR_CNS          = "br_cns",          "CNS — Brazil (Cartão Nacional de Saúde)"

    # ── Africa ───────────────────────────────────────────────────────────────
    # South Africa: Health Patient Registration System
    ZA_HPRS         = "za_hprs",         "HPRS — South Africa (Health Patient Registration)"

    # ── Generic / Custom ─────────────────────────────────────────────────────
    HOSPITAL_MRN    = "hospital_mrn",    "Hospital MRN (Local)"
    INSURANCE_ID    = "insurance_id",    "Health Insurance Member ID"
    OTHER           = "other",           "Other / Custom"


# Official FHIR R4 Identifier.system URIs.
# Source: HL7 Terminology Registry + national FHIR Implementation Guides.
# Auto-populated into ExternalPatientIdentity.fhir_system_uri on save.
FHIR_SYSTEM_URIS: dict[str, str] = {
    IdentitySystem.IN_ABHA:         "https://healthid.ndhm.gov.in",
    IdentitySystem.AU_IHI:          "http://ns.electronichealth.net.au/id/hi/ihi/1.0",
    IdentitySystem.NZ_NHI:          "https://standards.digital.health.nz/ns/nhi-id",
    IdentitySystem.UK_NHS:          "https://fhir.nhs.uk/Id/nhs-number",
    IdentitySystem.UK_CHI:          "http://www.nhs.uk/Id/scottish-chi-number",
    IdentitySystem.UK_HC:           "https://fhir.hscni.net/Id/hcn",
    IdentitySystem.IE_IHI:          "https://standards.digital.health.ie/ns/ihi-id",
    IdentitySystem.CH_EPR_SPID:     "urn:oid:2.16.756.5.30.1.127.3.10.3",
    IdentitySystem.DE_KVNR:         "http://fhir.de/sid/gkv/kvid-10",
    IdentitySystem.FR_NIR:          "urn:oid:1.2.250.1.213.1.4.8",
    IdentitySystem.NL_BSN:          "https://fhir.nl/fhir/NamingSystem/bsn",
    IdentitySystem.DK_CPR:          "urn:oid:1.2.208.176.1.2",
    IdentitySystem.SE_PERSONNUMMER: "https://www.datainspektionen.se/Se/personnummer",
    IdentitySystem.NO_FNR:          "urn:oid:2.16.578.1.12.4.1.4.1",
    IdentitySystem.FI_HETU:         "urn:oid:1.2.246.21",
    IdentitySystem.EE_PIC:          "https://fhir.ee/sid/pid/est/ni",
    IdentitySystem.SA_NHI:          "https://fhir.hl7.sa/identifier/patient/nhi",
    IdentitySystem.AE_UHI:          "https://www.haad.ae/identifier/patient",
    IdentitySystem.KR_RRN:          "urn:oid:2.16.840.1.113883.2.8.1",
    IdentitySystem.JP_MYNUMBER:     "urn:oid:1.2.392.200119.6.102",
    IdentitySystem.SG_NRIC:         "https://id.moh.gov.sg/nric",
    IdentitySystem.CA_PHN:          "https://fhir.infoway-inforoute.ca/NamingSystem/ca-patient-healthcare-id",
    IdentitySystem.BR_CNS:          "http://rnds.saude.gov.br/fhir/r4/NamingSystem/cns",
}

# Human-readable format hints. Used in validation error messages and admin UI.
SYSTEM_FORMAT_HINTS: dict[str, str] = {
    IdentitySystem.IN_ABHA:         "14-digit number (e.g. 12-3456-7890-1234)",
    IdentitySystem.AU_IHI:          "16-digit number (e.g. 8003608833357361)",
    IdentitySystem.NZ_NHI:          "LLLNNNN or LLLNNLX (e.g. ABC1234 or ABC12DV)",
    IdentitySystem.UK_NHS:          "10-digit, 3-3-4 format (e.g. 485 777 3456)",
    IdentitySystem.UK_CHI:          "10-digit, DOB-prefixed (e.g. 1011671234)",
    IdentitySystem.DE_KVNR:         "10-character alphanumeric (e.g. A123456789)",
    IdentitySystem.FR_NIR:          "15-digit number",
    IdentitySystem.DK_CPR:          "DDMMYY-XXXX (e.g. 010180-1234)",
    IdentitySystem.SE_PERSONNUMMER: "YYYYMMDD-XXXX",
    IdentitySystem.CA_PHN:          "Format varies by province. Specify province in identity_region.",
    IdentitySystem.AE_UHI:          "Format varies by emirate. Specify authority in identity_region.",
}


# ===========================================================================
# CHOICES
# ===========================================================================

class VerificationMethod(models.TextChoices):
    """
    How an ExternalPatientIdentity was verified with its issuing authority.
    Each method has a different trust weight — used in claim trust assessment.
    """
    OTP             = "otp",             "OTP (One-Time Password)"
    AADHAAR_OTP     = "aadhaar_otp",     "Aadhaar OTP (India — ABDM)"
    FACE_AUTH       = "face_auth",       "Face Authentication"
    DEMOGRAPHICS    = "demographics",    "Demographic Match"
    DOCUMENT_UPLOAD = "document_upload", "Document Upload (Manual Review)"
    BIOMETRIC       = "biometric",       "Biometric Verification"
    OAUTH           = "oauth",           "OAuth / SMART-on-FHIR"
    MANUAL_ADMIN    = "manual_admin",    "Manual (Admin Verified)"
    UNVERIFIED      = "unverified",      "Not Yet Verified"


# ===========================================================================
# MODEL: EXTERNAL PATIENT IDENTITY
# ===========================================================================

class ExternalPatientIdentity(models.Model):
    """
    Links a patient profile to a national / international health identifier.

    One row per (patient, system) pair. A patient may hold identities in
    multiple external systems simultaneously.

    Architecture:
      - Adding a new country = adding one enum value to IdentitySystem.
        Zero schema changes to this model. Zero migrations to patients/.
      - fhir_system_uri is auto-populated from FHIR_SYSTEM_URIS on save,
        avoiding repeated dict lookups during FHIR bundle generation.
      - raw_payload preserves the full issuing authority API response for
        provenance, debugging, and future re-parsing without re-calling the API.
      - secondary_value handles systems that issue two linked identifiers:
          ABHA:  identity_value = 14-digit ABHA number
                 secondary_value = @abdm address handle
                 secondary_label = "ABHA Address"
          UAE:   identity_value = UHI number
                 secondary_value = insurance card number
      - identity_region handles region-partitioned systems:
          CA_PHN  → province (BC, ON, QC, AB …)
          AE_UHI  → emirate authority (HAAD, DHA, DOH)
          US_MRN  → hospital / health system name

    Claim integration:
      When a user links a national ID in their account, the claims app searches
      this table on (system, identity_value) across ALL patient profiles.
      A match triggers the Tier 1 claim flow.

    FHIR R4: Patient.identifier[]
      Each active row = one Identifier entry in the FHIR Patient resource.
      Use to_fhir_identifier() to serialize.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Cross-app FK: integrations depends on patients
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="external_identities",
    )

    # ── Identity System & Value ───────────────────────────────────────────────
    system = models.CharField(
        max_length=32,
        choices=IdentitySystem.choices,
        help_text="The national/external health identity system.",
    )
    identity_value = models.CharField(
        max_length=256,
        help_text="The unique ID value within the external system.",
    )

    # ── FHIR Export ───────────────────────────────────────────────────────────
    # Auto-set from FHIR_SYSTEM_URIS on save.
    fhir_system_uri = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Official FHIR Identifier.system URI. Auto-populated on save.",
    )

    # ── Secondary Identifier ─────────────────────────────────────────────────
    secondary_value = models.CharField(max_length=256, null=True, blank=True)
    secondary_label = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Label for secondary_value (e.g. 'ABHA Address', 'Card Number').",
    )

    # ── Regional Qualifier ────────────────────────────────────────────────────
    # Required for region-partitioned systems.
    identity_region = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text=(
            "Regional sub-authority. "
            "Required for CA_PHN (province), AE_UHI (emirate), US_MRN (facility)."
        ),
    )

    # ── Verification ──────────────────────────────────────────────────────────
    is_verified         = models.BooleanField(default=False)
    verified_at         = models.DateTimeField(null=True, blank=True)
    verification_method = models.CharField(
        max_length=32,
        choices=VerificationMethod.choices,
        default=VerificationMethod.UNVERIFIED,
    )

    # ── Linking Lifecycle ─────────────────────────────────────────────────────
    is_linked = models.BooleanField(default=False)
    linked_at = models.DateTimeField(null=True, blank=True)

    # Ephemeral auth token from the external linking flow.
    # MUST be cleared (set to null) once the linking flow completes.
    # Treat as sensitive. Never log or expose in API responses.
    linking_token            = models.TextField(null=True, blank=True)
    linking_token_expires_at = models.DateTimeField(null=True, blank=True)

    # ── Provenance ────────────────────────────────────────────────────────────
    # Full original API response from the issuing authority.
    # Internal / audit use only. Never exposed in patient-facing API responses.
    raw_payload = models.JSONField(
        null=True,
        blank=True,
        help_text="Full original API response from external identity system. Internal use only.",
    )

    # ── Soft Revocation ───────────────────────────────────────────────────────
    is_revoked        = models.BooleanField(default=False)
    revoked_at        = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "integrations"
        db_table  = "external_patient_identities"
        constraints = [
            # One active identity per (patient, system).
            # Revoked records are retained for audit; new ones can follow.
            models.UniqueConstraint(
                fields=["patient", "system"],
                condition=models.Q(is_revoked=False),
                name="uq_patient_system_active",
            ),
        ]
        indexes = [
            # Tier 1 claim lookup: find profiles matching a given (system, value)
            models.Index(
                fields=["system", "identity_value"],
                name="idx_ext_identity_system_value",
            ),
            # FHIR export: all active identities for a patient
            models.Index(
                fields=["patient", "is_linked", "is_revoked"],
                name="idx_ext_pat_identity_active",
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_system_display()} "
            f"[{self.identity_value}] → Patient {self.patient_id}"
        )

    def save(self, *args, **kwargs):
        # Auto-populate FHIR system URI from the registry on every save.
        if not self.fhir_system_uri:
            self.fhir_system_uri = FHIR_SYSTEM_URIS.get(self.system)
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.is_linked and not self.is_revoked

    def to_fhir_identifier(self) -> dict:
        """
        Serialize as a FHIR R4 Identifier object for Patient.identifier[].
        Called by the FHIR export layer.

        Returns:
            {
                "system": "<fhir_system_uri>",
                "value":  "<identity_value>",
                "use":    "official"
            }
        """
        return {
            "system": self.fhir_system_uri or f"urn:uhr:{self.system}",
            "value":  self.identity_value,
            "use":    "official",
        }