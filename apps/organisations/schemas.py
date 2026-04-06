"""organisations/schemas.py — input and response schemas for organisations app."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field, field_validator


class CreateOrgSchema(Schema):
    """Input for registering a new organisation."""
    name:                str  = Field(..., min_length=2, max_length=255)
    type:                str  = Field(..., description="hospital | clinic | lab | pharmacy | telehealth | imaging | dental | mental_health | other")
    registration_number: Optional[str]  = Field(default=None, max_length=100)
    description:         Optional[str]  = None
    website:             Optional[str]  = None
    email:               Optional[str]  = None
    phone:               Optional[str]  = Field(default=None, max_length=20)
    address:             Optional[str]  = None
    country:             Optional[str]  = Field(default=None, max_length=2, description="ISO 3166-1 alpha-2")
    parent_id:           Optional[UUID] = Field(default=None, description="Parent organisation ID for branches")

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"hospital","clinic","lab","pharmacy","telehealth","imaging","dental","mental_health","other"}
        if v not in allowed:
            raise ValueError(f"type must be one of: {', '.join(sorted(allowed))}")
        return v

    @field_validator("country")
    @classmethod
    def validate_country(cls, v):
        if v and len(v) != 2:
            raise ValueError("country must be a 2-letter ISO 3166-1 alpha-2 code.")
        return v.upper() if v else v


class UpdateOrgSchema(Schema):
    """PATCH input — all fields optional."""
    name:        Optional[str] = Field(default=None, min_length=2, max_length=255)
    description: Optional[str] = None
    website:     Optional[str] = None
    email:       Optional[str] = None
    phone:       Optional[str] = Field(default=None, max_length=20)
    address:     Optional[str] = None


class OrgResponseSchema(Schema):
    """Full organisation response."""
    id:                  UUID
    name:                str
    type:                str
    registration_number: Optional[str]
    description:         Optional[str]
    website:             Optional[str]
    email:               Optional[str]
    phone:               Optional[str]
    address:             Optional[str]
    country:             Optional[str]
    parent_id:           Optional[UUID]
    verified:            bool
    verified_at:         Optional[datetime]
    is_active:           bool
    created_at:          datetime

    class Config:
        from_attributes = True


class OrgSummarySchema(Schema):
    """Minimal org info for embedding in other responses."""
    id:       UUID
    name:     str
    type:     str
    verified: bool
    country:  Optional[str]

    class Config:
        from_attributes = True