"""
patients/schemas.py
===================
All input and response schemas for the patients API.

Patterns followed from users/schemas.py:
  - ninja.Schema base class
  - Pydantic field_validator / model_validator for business rules
  - Separate input schemas (what the client sends) from response schemas (what we return)
  - Optional fields use Optional[T] = None
  - All response schemas use Config: from_attributes = True
  - ErrorSchema is shared — imported from users app or redefined here consistently
"""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from ninja import Schema
from pydantic import EmailStr, Field, field_validator, model_validator

from .models import AccessRole, ClaimMethod, Gender, TrustLevel

# ErrorSchema is defined once in users.schemas and reused across all apps.
# Never redefine it — keeps error responses consistent across the entire API.
from users.schemas import ErrorSchema


# ===========================================================================
# PATIENT — INPUT SCHEMAS
# ===========================================================================

class CreatePatientSchema(Schema):
    """
    Input schema for creating a new patient profile.

    Used for both:
      - Creating your own profile (caller becomes role=primary, claim_method=system_created)
      - Creating a dependent's profile (caller becomes role=full_delegate)

    is_dependent flag drives which PatientUserAccess role is assigned.
    When is_dependent=True, transfer_eligible_at should be provided if the
    dependent is a minor — set it to their 18th birthday.
    """
    first_name:  str  = Field(..., min_length=1, max_length=100)
    last_name:   str  = Field(..., min_length=1, max_length=100)
    birth_date:  date
    gender:      str  = Gender.UNKNOWN

    # Optional demographics
    phone:       Optional[str]      = None
    email:       Optional[EmailStr] = None
    address:     Optional[str]      = None
    blood_group: Optional[str]      = None
    nationality: Optional[str]      = Field(None, min_length=2, max_length=2)

    # Deceased — both required together if patient is deceased
    is_deceased:   bool           = False
    deceased_date: Optional[date] = None

    # Dependent profile flag
    # True  → caller becomes full_delegate (e.g. parent creating child's profile)
    # False → caller becomes primary (creating their own profile)
    is_dependent: bool = False

    # Only relevant when is_dependent=True and patient is a minor.
    # Set to the patient's 18th birthday.
    transfer_eligible_at: Optional[date] = None

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_names(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty or whitespace.")
        return v

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v: str) -> str:
        valid = {choice[0] for choice in Gender.choices}
        if v not in valid:
            raise ValueError(f"Invalid gender. Must be one of: {', '.join(valid)}")
        return v

    @field_validator("nationality")
    @classmethod
    def validate_nationality(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip().upper()
            if not v.isalpha() or len(v) != 2:
                raise ValueError("Nationality must be a valid ISO 3166-1 alpha-2 code (e.g. IN, AU, GB).")
        return v

    @field_validator("birth_date")
    @classmethod
    def validate_birth_date(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Birth date cannot be in the future.")
        return v

    @field_validator("deceased_date")
    @classmethod
    def validate_deceased_date(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > date.today():
            raise ValueError("Deceased date cannot be in the future.")
        return v

    @model_validator(mode="after")
    def validate_deceased_consistency(self) -> "CreatePatientSchema":
        """
        Mirror the DB CheckConstraint at schema level for early feedback.
        If is_deceased=True, deceased_date must be provided.
        """
        if self.is_deceased and not self.deceased_date:
            raise ValueError("deceased_date is required when is_deceased is True.")
        if not self.is_deceased and self.deceased_date:
            raise ValueError("deceased_date should not be set when is_deceased is False.")
        return self

    @model_validator(mode="after")
    def validate_deceased_after_birth(self) -> "CreatePatientSchema":
        if self.deceased_date and self.birth_date:
            if self.deceased_date < self.birth_date:
                raise ValueError("deceased_date cannot be before birth_date.")
        return self

    @model_validator(mode="after")
    def validate_transfer_eligible(self) -> "CreatePatientSchema":
        """
        transfer_eligible_at only makes sense for dependent profiles.
        """
        if self.transfer_eligible_at and not self.is_dependent:
            raise ValueError("transfer_eligible_at is only valid for dependent profiles (is_dependent=True).")
        if self.transfer_eligible_at and self.birth_date:
            if self.transfer_eligible_at <= self.birth_date:
                raise ValueError("transfer_eligible_at must be after birth_date.")
        return self


class UpdatePatientSchema(Schema):
    """
    Input schema for updating a patient profile.
    All fields are optional — only provided fields are updated (PATCH semantics).
    Immutable fields (birth_date, mrn) are excluded — they cannot be changed via API.
    """
    first_name:  Optional[str]      = Field(None, min_length=1, max_length=100)
    last_name:   Optional[str]      = Field(None, min_length=1, max_length=100)
    gender:      Optional[str]      = None
    phone:       Optional[str]      = None
    email:       Optional[EmailStr] = None
    address:     Optional[str]      = None
    blood_group: Optional[str]      = None
    nationality: Optional[str]      = Field(None, min_length=2, max_length=2)

    # Deceased update — must always be provided together
    is_deceased:   Optional[bool] = None
    deceased_date: Optional[date] = None

    # Transfer eligibility — only updatable by full_delegate or primary
    transfer_eligible_at: Optional[date] = None

    @field_validator("first_name", "last_name")
    @classmethod
    def strip_names(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Name cannot be empty or whitespace.")
        return v

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            valid = {choice[0] for choice in Gender.choices}
            if v not in valid:
                raise ValueError(f"Invalid gender. Must be one of: {', '.join(valid)}")
        return v

    @field_validator("nationality")
    @classmethod
    def validate_nationality(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            v = v.strip().upper()
            if not v.isalpha() or len(v) != 2:
                raise ValueError("Nationality must be a valid ISO 3166-1 alpha-2 code.")
        return v

    @model_validator(mode="after")
    def validate_deceased_pair(self) -> "UpdatePatientSchema":
        """
        If either deceased field is provided, both must be provided together.
        Prevents partial updates that would violate the DB constraint.
        """
        deceased_provided = self.is_deceased is not None
        date_provided     = self.deceased_date is not None

        if deceased_provided and not date_provided and self.is_deceased:
            raise ValueError("deceased_date is required when setting is_deceased=True.")
        if date_provided and not deceased_provided:
            raise ValueError("is_deceased must be explicitly set when providing deceased_date.")
        return self


class RetractPatientSchema(Schema):
    """Input schema for soft-retracting (soft-deleting) a patient profile."""
    retraction_reason: str = Field(..., min_length=10, max_length=1000)


# ===========================================================================
# PATIENT ACCESS — INPUT SCHEMAS
# ===========================================================================

class GrantAccessSchema(Schema):
    """
    Input schema for granting another user access to a patient profile.
    Only the primary holder (or full_delegate on unclaimed profiles) can grant access.
    """
    user_email: EmailStr = Field(..., description="Email of the user to grant access to.")
    role: str = Field(..., description="Access role to grant.")
    notes: Optional[str] = Field(None, max_length=500)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        # primary cannot be granted manually — only via the claim system
        grantable_roles = {
            AccessRole.CAREGIVER,
            AccessRole.VIEWER,
            AccessRole.FULL_DELEGATE,
        }
        if v not in grantable_roles:
            raise ValueError(
                f"Role '{v}' cannot be granted manually. "
                f"Grantable roles: {', '.join(grantable_roles)}"
            )
        return v

    @field_validator("user_email", mode="before")
    @classmethod
    def normalise_email(cls, v: str) -> str:
        return v.strip().lower()


class RevokeAccessSchema(Schema):
    """Input schema for revoking a user's access to a patient profile."""
    revocation_reason: str = Field(..., min_length=5, max_length=500)


class SelfExitSchema(Schema):
    """
    Input schema for a caregiver removing their own access (self-exit).
    Reason is optional for self-exits but encouraged.
    """
    reason: Optional[str] = Field(None, max_length=500)


# ===========================================================================
# PATIENT — RESPONSE SCHEMAS
# ===========================================================================

class PatientSummarySchema(Schema):
    """
    Lightweight patient response — used in list endpoints.
    Does not include access history or sensitive claim state.
    """
    id:         UUID
    mrn:        Optional[str]
    first_name: str
    last_name:  str
    full_name:  str
    gender:     str
    birth_date: date
    age:        Optional[int]    # Python property — None for deceased
    nationality: Optional[str]
    is_claimed:  bool
    is_deceased: bool
    is_active:   bool            # False if retracted
    created_at:  datetime

    # The requesting user's role on this profile (injected by service)
    my_role:       str
    can_write:     bool
    can_manage:    bool

    class Config:
        from_attributes = True


class PatientDetailSchema(Schema):
    """
    Full patient response — used in single-record endpoints.
    Includes all demographics and claim state.
    """
    id:           UUID
    mrn:          Optional[str]
    first_name:   str
    last_name:    str
    full_name:    str
    gender:       str
    birth_date:   date
    age:          Optional[int]
    phone:        Optional[str]
    email:        Optional[str]
    address:      Optional[str]
    blood_group:  Optional[str]
    nationality:  Optional[str]
    is_deceased:  bool
    deceased_date: Optional[date]
    is_claimed:   bool
    claimed_at:   Optional[datetime]
    transfer_eligible_at: Optional[date]
    is_active:    bool
    created_at:   datetime
    updated_at:   datetime

    # Requesting user's access context
    my_role:       str
    can_write:     bool
    can_manage:    bool

    class Config:
        from_attributes = True


# ===========================================================================
# PATIENT ACCESS — RESPONSE SCHEMAS
# ===========================================================================

class AccessHolderSchema(Schema):
    """
    One entry in the patient's access list.
    Shows who has access and what role they hold.
    Returned by GET /patients/{id}/access/
    """
    id:          UUID
    user_id:     UUID
    user_email:  str
    user_name:   str
    role:        str
    claim_method: str
    trust_level:  str
    is_active:   bool
    granted_at:  datetime
    granted_by_id: Optional[UUID]

    # Revocation info — only populated if is_active=False
    revoked_at:        Optional[datetime]
    revocation_reason: Optional[str]

    notes: Optional[str]

    class Config:
        from_attributes = True


class GrantAccessResponseSchema(Schema):
    """Response after successfully granting access to a user."""
    id:         UUID
    user_id:    UUID
    user_email: str
    role:       str
    granted_at: datetime

    class Config:
        from_attributes = True