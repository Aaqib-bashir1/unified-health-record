"""practitioners/schemas.py — input and response schemas for practitioners app."""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field


class CreatePractitionerSchema(Schema):
    """Input for creating a practitioner profile."""
    full_name:                 str           = Field(..., min_length=2, max_length=200)
    gender:                    Optional[str] = Field(default=None, description="male | female | other | unknown")
    birth_date:                Optional[date] = None
    license_number:            Optional[str] = Field(default=None, max_length=100)
    license_issuing_authority: Optional[str] = Field(default=None, max_length=200)
    license_expires_at:        Optional[date] = None
    specialization:            Optional[str] = Field(default=None, max_length=100)
    qualification:             Optional[str] = None


class UpdatePractitionerSchema(Schema):
    """PATCH input for updating practitioner profile."""
    full_name:                 Optional[str]  = Field(default=None, min_length=2, max_length=200)
    gender:                    Optional[str]  = None
    license_number:            Optional[str]  = Field(default=None, max_length=100)
    license_issuing_authority: Optional[str]  = Field(default=None, max_length=200)
    license_expires_at:        Optional[date] = None
    specialization:            Optional[str]  = Field(default=None, max_length=100)
    qualification:             Optional[str]  = None


class PractitionerResponseSchema(Schema):
    id:                        UUID
    user_id:                   UUID
    full_name:                 str
    gender:                    Optional[str]
    birth_date:                Optional[date]
    license_number:            Optional[str]
    license_issuing_authority: Optional[str]
    license_expires_at:        Optional[date]
    specialization:            Optional[str]
    qualification:             Optional[str]
    is_verified:               bool
    verified_at:               Optional[datetime]
    verification_source:       str
    is_active:                 bool
    created_at:                datetime

    class Config:
        from_attributes = True


class PractitionerSummarySchema(Schema):
    """Minimal practitioner info for embedding in other responses."""
    id:            UUID
    full_name:     str
    specialization: Optional[str]
    is_verified:   bool

    class Config:
        from_attributes = True


class JoinOrgSchema(Schema):
    """Input for requesting to join an organisation."""
    organisation_id:      UUID
    requested_role_title: Optional[str] = Field(default=None, max_length=100)
    requested_department: Optional[str] = Field(default=None, max_length=100)
    message:              Optional[str] = Field(default=None, max_length=1000)


class MembershipRequestResponseSchema(Schema):
    id:                   UUID
    practitioner_id:      UUID
    practitioner_name:    str
    organisation_id:      UUID
    organisation_name:    str
    requested_role_title: Optional[str]
    requested_department: Optional[str]
    message:              Optional[str]
    status:               str
    responded_at:         Optional[datetime]
    rejection_reason:     Optional[str]
    created_at:           datetime

    class Config:
        from_attributes = True


class RejectMembershipSchema(Schema):
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional reason shown to the practitioner.",
    )


class PractitionerRoleResponseSchema(Schema):
    id:            UUID
    practitioner_id: UUID
    organisation_id: UUID
    organisation_name: str
    role_title:    Optional[str]
    department:    Optional[str]
    start_date:    date
    end_date:      Optional[date]
    is_active:     bool
    is_primary:    bool
    is_org_admin:  bool

    class Config:
        from_attributes = True