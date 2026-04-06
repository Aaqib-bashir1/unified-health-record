"""
visits/schemas.py
=================
Input and response schemas for the visits app.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field


# ===========================================================================
# INPUT SCHEMAS
# ===========================================================================

class InitiateVisitSchema(Schema):
    """
    Input for initiating a visit session.
    Patient scans the organisation's QR code and submits the encoded token.
    """
    org_qr_token: str = Field(
        ...,
        description=(
            "The signed JWT encoded in the organisation's QR code. "
            "Scan the QR at reception to obtain this token."
        ),
    )
    visit_reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional reason for the visit e.g. 'Annual checkup', 'Follow-up'.",
    )
    expiry_hours: Optional[int] = Field(
        default=None,
        ge=1,
        le=72,
        description=(
            "How many hours this visit session should last. "
            "Defaults to 24 hours if not provided. Maximum 72 hours."
        ),
    )


class EndVisitSchema(Schema):
    """Optional input when ending a visit early."""
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional reason for ending the visit early.",
    )


# ===========================================================================
# RESPONSE SCHEMAS
# ===========================================================================

class OrganisationSummarySchema(Schema):
    """Minimal organisation info embedded in visit responses."""
    id:   UUID
    name: str
    type: str

    class Config:
        from_attributes = True


class VisitResponseSchema(Schema):
    """
    Full visit session response.
    Returned by initiate, end, and list endpoints.
    """
    id:                UUID
    patient_id:        UUID
    organisation_id:   UUID
    organisation_name: str
    initiated_at:      datetime
    expires_at:        datetime
    ended_at:          Optional[datetime]
    is_active:         bool
    visit_reason:      Optional[str]

    class Config:
        from_attributes = True


class VisitAccessRecordSchema(Schema):
    """
    A single practitioner visit access record.
    Shown in audit views — represents who actually accessed the patient during a visit.
    """
    id:                UUID
    visit_id:          UUID
    patient_id:        UUID
    practitioner_id:   UUID
    first_accessed_at: datetime
    last_accessed_at:  datetime
    is_active:         bool
    revoked_at:        Optional[datetime]
    revocation_reason: Optional[str]

    class Config:
        from_attributes = True


class OrgQRResponseSchema(Schema):
    """
    Response from the org QR generation endpoint.
    The frontend renders `qr_data` as a QR code image.
    """
    token:              str
    org_id:             UUID
    org_name:           str
    expires_at:         datetime
    expires_in_seconds: int
    qr_data:            str   # uhr://visit/<token> — rendered as QR by frontend