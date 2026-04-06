"""share/schemas.py — schemas for share links and sessions."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field, field_validator


class CreateShareLinkSchema(Schema):
    """Input for creating a share link."""

    validator_type: str = Field(
        ...,
        description="'year_of_birth' or 'pin'",
    )
    validator_value: str = Field(
        ...,
        min_length=4,
        max_length=6,
        description=(
            "The raw value to hash as the challenge. "
            "For year_of_birth: 4-digit year e.g. '1990'. "
            "For pin: 4-6 digit PIN e.g. '1234'."
        ),
    )
    expiry_hours: int = Field(
        default=48,
        ge=1,
        le=720,   # 30 days max
        description="How many hours the link should remain valid. Default 48.",
    )
    label: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Optional label e.g. 'For Dr Ahmed at Apollo'.",
    )

    @field_validator("validator_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"year_of_birth", "pin"}
        if v not in allowed:
            raise ValueError(f"validator_type must be one of: {allowed}")
        return v

    @field_validator("validator_value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("validator_value must be numeric digits only.")
        return v


class ShareLinkResponseSchema(Schema):
    id:                UUID
    patient_id:        UUID
    token:             str
    validator_type:    str
    scope:             str
    expires_at:        datetime
    is_revoked:        bool
    first_accessed_at: Optional[datetime]
    access_count:      int
    label:             Optional[str]
    created_at:        datetime
    share_url:         str   # computed — not a model field

    class Config:
        from_attributes = True


class VerifyShareLinkSchema(Schema):
    """Input for verifying a share link challenge."""
    validator_value: str = Field(
        ...,
        min_length=4,
        max_length=6,
        description="Year of birth or PIN matching what the patient set.",
    )

    @field_validator("validator_value")
    @classmethod
    def must_be_digits(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("validator_value must be digits only.")
        return v


class SessionResponseSchema(Schema):
    """Returned after successful share link verification."""
    session_token: str
    expires_at:    datetime
    patient_id:    UUID
    scope:         str


class SecondOpinionSchema(Schema):
    """Input for submitting a second opinion via a share link session."""
    doctor_name:               str = Field(..., min_length=2, max_length=200)
    doctor_registration_number: Optional[str] = Field(default=None, max_length=100)
    opinion_text:              str = Field(..., min_length=10)