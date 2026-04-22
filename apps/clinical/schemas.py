"""clinical/schemas.py — schemas for test orders."""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field


class CreateTestOrderSchema(Schema):
    """Input for placing a test order."""
    patient_id:           UUID
    test_name:            str  = Field(..., min_length=2, max_length=255)
    category:             str  = Field(default="laboratory",
                                        description="laboratory | imaging | cardiology | pulmonology | neurology | microbiology | histology | specialist | other")
    coding_system:        Optional[str]  = None
    coding_code:          Optional[str]  = None
    coding_display:       Optional[str]  = None
    priority:             str            = Field(default="routine",
                                                  description="routine | urgent | stat | asap")
    clinical_reason:      str            = Field(..., min_length=5,
                                                  description="Why this test is being ordered.")
    special_instructions: Optional[str]  = None
    specimen_type:        Optional[str]  = None
    due_date:             Optional[date] = None


class UpdateOrderStatusSchema(Schema):
    """Input for updating order status."""
    status:              str            = Field(..., description="active | specimen_collected | in_lab | resulted | cancelled | on_hold")
    cancellation_reason: Optional[str]  = None
    notes:               Optional[str]  = None


class LinkResultSchema(Schema):
    """Input for linking a result ObservationEvent to a TestOrder."""
    resulting_event_id: UUID = Field(
        ...,
        description="The ObservationEvent UUID created from the test result.",
    )


class TestOrderResponseSchema(Schema):
    id:                     UUID
    patient_id:             UUID
    ordering_practitioner_id: UUID
    ordering_practitioner_name: str
    ordering_organisation_id: Optional[UUID]
    test_name:              str
    category:               str
    coding_system:          Optional[str]
    coding_code:            Optional[str]
    priority:               str
    clinical_reason:        str
    special_instructions:   Optional[str]
    specimen_type:          Optional[str]
    status:                 str
    ordered_at:             datetime
    due_date:               Optional[date]
    specimen_collected_at:  Optional[datetime]
    resulted_at:            Optional[datetime]
    cancelled_at:           Optional[datetime]
    cancellation_reason:    Optional[str]
    resulting_event_id:     Optional[UUID]
    # CPOE fields — shown when populated
    cpoe_order_id:          Optional[str]
    order_set_id:           Optional[UUID]
    billing_code:           Optional[str]
    requires_auth:          bool
    created_at:             datetime

    class Config:
        from_attributes = True


class TestOrderSummarySchema(Schema):
    """Slim order for list views."""
    id:         UUID
    test_name:  str
    category:   str
    priority:   str
    status:     str
    ordered_at: datetime
    due_date:   Optional[date]

    class Config:
        from_attributes = True