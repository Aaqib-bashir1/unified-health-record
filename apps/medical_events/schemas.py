"""
medical_events/schemas.py
=========================
Input and response schemas for all medical event types.

Pattern:
  Create<Type>Schema  → input for creating that event type
  <Type>EventResponse → response for that event type
  MedicalEventResponse → full event response including base + typed extension
  TimelineResponse    → paginated timeline list item
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field, model_validator


# ===========================================================================
# BASE EVENT FIELDS (shared across all create schemas)
# ===========================================================================

class BaseEventSchema(Schema):
    """
    Common fields for all event creation inputs.
    Subclassed by each typed event schema.
    """
    clinical_timestamp: datetime = Field(
        ...,
        description=(
            "When this event occurred in real life. "
            "Must be timezone-aware (include UTC offset). "
            "May be backdated for uploading historical records."
        ),
    )
    notes: Optional[str] = Field(default=None, max_length=5000)


# ===========================================================================
# INPUT SCHEMAS — one per event type
# ===========================================================================

class CreateVisitEventSchema(BaseEventSchema):
    reason:     Optional[str] = Field(default=None, max_length=1000)
    visit_type: Optional[str] = Field(
        default=None,
        max_length=100,
        description="outpatient | inpatient | emergency | telehealth | home_visit",
    )


class CreateObservationEventSchema(BaseEventSchema):
    observation_name: str = Field(..., min_length=1, max_length=255,
                                   description="Human-readable test name e.g. 'Blood Glucose'")

    # Coding — optional, LOINC recommended
    coding_system:  Optional[str] = Field(default=None, max_length=100)
    coding_code:    Optional[str] = Field(default=None, max_length=50)
    coding_display: Optional[str] = Field(default=None, max_length=255)

    # Value — numeric or string
    value_type:     str           = Field(default="quantity",
                                          description="quantity | string | boolean")
    value_quantity: Optional[Decimal] = None
    value_unit:     Optional[str] = Field(default=None, max_length=50)
    value_string:   Optional[str] = Field(default=None, max_length=500)

    reference_range: Optional[str] = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_value(self) -> "CreateObservationEventSchema":
        if self.value_type == "quantity" and self.value_quantity is None:
            raise ValueError("value_quantity required when value_type=quantity.")
        if self.value_type == "string" and not self.value_string:
            raise ValueError("value_string required when value_type=string.")
        return self


class CreateConditionEventSchema(BaseEventSchema):
    condition_name: str = Field(..., min_length=1, max_length=255)

    # Coding — optional, ICD-10 / SNOMED CT
    coding_system:  Optional[str] = Field(default=None, max_length=100)
    coding_code:    Optional[str] = Field(default=None, max_length=50)
    coding_display: Optional[str] = Field(default=None, max_length=255)

    clinical_status: str  = Field(default="active")
    onset_date:      Optional[date] = None
    abatement_date:  Optional[date] = None


class CreateMedicationEventSchema(BaseEventSchema):
    medication_name: str = Field(..., min_length=1, max_length=255)
    dosage:          Optional[str] = Field(default=None, max_length=100)
    frequency:       Optional[str] = Field(default=None, max_length=100)
    route:           Optional[str] = Field(default=None, max_length=100)
    start_date:      Optional[date] = None
    end_date:        Optional[date] = None
    status:          str = Field(default="active")


class CreateProcedureEventSchema(BaseEventSchema):
    procedure_name: str = Field(..., min_length=1, max_length=255)

    coding_system:  Optional[str] = Field(default=None, max_length=100)
    coding_code:    Optional[str] = Field(default=None, max_length=50)
    coding_display: Optional[str] = Field(default=None, max_length=255)

    performed_date: Optional[date] = None


class CreateDocumentEventSchema(BaseEventSchema):
    """
    For document upload the file is submitted as multipart/form-data.
    This schema covers the metadata fields only.
    The actual file bytes are handled separately in the API layer.
    """
    document_type:     str           = Field(
        default="other",
        description=(
            "lab_report | prescription | imaging | discharge_summary "
            "| referral | vaccination | insurance | other"
        ),
    )
    original_filename: Optional[str] = Field(default=None, max_length=255)


class CreateSecondOpinionSchema(BaseEventSchema):
    doctor_name:                str           = Field(..., min_length=2, max_length=200)
    doctor_registration_number: Optional[str] = Field(default=None, max_length=100)
    opinion_text:               str           = Field(..., min_length=10)


class CreateAmendmentSchema(Schema):
    """Input for amending (correcting) an existing event."""
    original_event_id: UUID
    amendment_reason:  str = Field(
        ...,
        min_length=10,
        description="Why this correction is being made. Required. Stored permanently.",
    )
    # The actual corrected data — matches the original event type's schema
    event_data:        dict = Field(
        ...,
        description="The corrected event data. Must match the original event type's schema.",
    )


class MedicationLifecycleSchema(Schema):
    """Input for a medication lifecycle event (modify or discontinue)."""
    parent_event_id: UUID
    action:          str = Field(
        ...,
        description="modified | discontinued",
    )
    # Fields to update — only relevant fields needed
    medication_name: Optional[str]  = None
    dosage:          Optional[str]  = None
    frequency:       Optional[str]  = None
    end_date:        Optional[date] = None
    notes:           Optional[str]  = None
    clinical_timestamp: datetime = Field(
        ...,
        description="When this lifecycle change occurred.",
    )


class ApproveEventSchema(Schema):
    """Input when patient approves a pending_approval event."""
    approve: bool = Field(
        ...,
        description="True to approve (make visible), False to hide.",
    )


# ===========================================================================
# RESPONSE SCHEMAS
# ===========================================================================

class MedicalEventBaseResponse(Schema):
    """Base fields returned for every event."""
    id:                 UUID
    patient_id:         UUID
    event_type:         str
    clinical_timestamp: datetime
    system_timestamp:   datetime
    source_type:        str
    source_practitioner_id: Optional[UUID]
    source_organisation_id: Optional[UUID]
    verification_level: str
    visibility_status:  str
    created_by_id:      UUID
    amends_event_id:    Optional[UUID]
    amendment_reason:   Optional[str]
    parent_event_id:    Optional[UUID]
    relationship_type:  str
    created_at:         datetime

    class Config:
        from_attributes = True


class VisitEventDetailResponse(Schema):
    reason:     Optional[str]
    visit_type: Optional[str]
    notes:      Optional[str]

    class Config:
        from_attributes = True


class ObservationEventDetailResponse(Schema):
    observation_name: str
    coding_system:    Optional[str]
    coding_code:      Optional[str]
    coding_display:   Optional[str]
    value_type:       str
    value_quantity:   Optional[Decimal]
    value_unit:       Optional[str]
    value_string:     Optional[str]
    reference_range:  Optional[str]

    class Config:
        from_attributes = True


class ConditionEventDetailResponse(Schema):
    condition_name:  str
    coding_system:   Optional[str]
    coding_code:     Optional[str]
    coding_display:  Optional[str]
    clinical_status: str
    onset_date:      Optional[date]
    abatement_date:  Optional[date]
    notes:           Optional[str]

    class Config:
        from_attributes = True


class MedicationEventDetailResponse(Schema):
    medication_name: str
    dosage:          Optional[str]
    frequency:       Optional[str]
    route:           Optional[str]
    start_date:      Optional[date]
    end_date:        Optional[date]
    status:          str
    notes:           Optional[str]

    class Config:
        from_attributes = True


class ProcedureEventDetailResponse(Schema):
    procedure_name: str
    coding_system:  Optional[str]
    coding_code:    Optional[str]
    coding_display: Optional[str]
    performed_date: Optional[date]
    notes:          Optional[str]

    class Config:
        from_attributes = True


class DocumentEventDetailResponse(Schema):
    document_type:     str
    original_filename: Optional[str]
    file_type:         str
    file_size_bytes:   Optional[int]
    storage_provider:  str
    download_url:      Optional[str]   # presigned URL, generated on request

    class Config:
        from_attributes = True


class SecondOpinionDetailResponse(Schema):
    doctor_name:                str
    doctor_registration_number: Optional[str]
    opinion_text:               str
    approved_by_patient:        bool

    class Config:
        from_attributes = True


class MedicalEventFullResponse(Schema):
    """Full event response — base fields + typed extension."""
    base:      MedicalEventBaseResponse
    extension: Optional[Any] = None   # typed based on event_type


class TimelineEventResponse(Schema):
    """Slim event response for the timeline list."""
    id:                 UUID
    event_type:         str
    clinical_timestamp: datetime
    verification_level: str
    visibility_status:  str
    source_type:        str
    summary:            str            # computed human-readable summary
    has_document:       bool

    class Config:
        from_attributes = True


class ActiveMedicationResponse(Schema):
    """Response for the active medications computed view."""
    event_id:       UUID
    medication_name: str
    dosage:          Optional[str]
    frequency:       Optional[str]
    route:           Optional[str]
    start_date:      Optional[date]
    status:          str
    verification_level: str
    source_type:     str

    class Config:
        from_attributes = True


# ===========================================================================
# NEW INPUT SCHEMAS — Allergy, Vaccination, Consultation, VitalSigns
# ===========================================================================

class CreateAllergyEventSchema(BaseEventSchema):
    substance_name:    str           = Field(..., min_length=1, max_length=255)
    coding_system:     Optional[str] = None
    coding_code:       Optional[str] = None
    coding_display:    Optional[str] = None
    allergy_type:      str           = Field(default="allergy",
                                              description="allergy | intolerance")
    category:          str           = Field(default="medication",
                                              description="food | medication | environment | biologic | other")
    criticality:       str           = Field(default="unable_to_assess",
                                              description="low | high | unable_to_assess")
    reaction_type:     Optional[str] = Field(default=None, max_length=200,
                                              description="e.g. anaphylaxis, urticaria, nausea")
    reaction_severity: Optional[str] = Field(default=None,
                                              description="mild | moderate | severe")
    clinical_status:   str           = Field(default="active",
                                              description="active | resolved | entered_in_error")
    onset_date:        Optional[date] = None


class CreateVaccinationEventSchema(BaseEventSchema):
    vaccine_name:        str           = Field(..., min_length=1, max_length=255)
    coding_system:       Optional[str] = None
    coding_code:         Optional[str] = Field(default=None, description="CVX code")
    coding_display:      Optional[str] = None
    dose_number:         Optional[str] = Field(default=None, max_length=20,
                                                description="e.g. 1, 2, Booster")
    lot_number:          Optional[str] = None
    administered_date:   Optional[date] = None
    next_dose_due_date:  Optional[date] = None
    administering_org:   Optional[str] = Field(default=None, max_length=255)
    site:                Optional[str] = Field(default=None, max_length=100,
                                                description="e.g. Left arm, Right deltoid")
    route:               Optional[str] = Field(default=None, max_length=100,
                                                description="e.g. Intramuscular, Subcutaneous")


class CreateConsultationEventSchema(BaseEventSchema):
    department:                    str            = Field(
        default="general_practice",
        description=(
            "general_practice | cardiology | neurology | oncology | orthopaedics | "
            "gastroenterology | pulmonology | nephrology | endocrinology | dermatology | "
            "psychiatry | ophthalmology | ent | urology | gynaecology | paediatrics | "
            "haematology | radiology | rheumatology | emergency | other"
        ),
    )
    sub_specialty:                 Optional[str]  = Field(default=None, max_length=100)
    consulting_practitioner_id:    Optional[UUID] = Field(
        default=None,
        description="UUID of the Practitioner who conducted this consultation.",
    )
    referred_by_id:                Optional[UUID] = Field(
        default=None,
        description="UUID of the Practitioner who made the referral.",
    )
    chief_complaint:               str            = Field(..., min_length=3)
    history_of_present_illness:    Optional[str]  = None
    examination_findings:          Optional[str]  = None
    investigations_ordered:        Optional[str]  = None
    assessment:                    Optional[str]  = None
    plan:                          Optional[str]  = None
    follow_up_date:                Optional[date] = None
    follow_up_instructions:        Optional[str]  = None


class CreateVitalSignsEventSchema(BaseEventSchema):
    # Blood pressure
    systolic_bp:      Optional[Decimal] = Field(default=None, description="mmHg")
    diastolic_bp:     Optional[Decimal] = Field(default=None, description="mmHg")
    bp_position:      Optional[str]     = Field(default=None,
                                                 description="sitting | standing | lying")
    # Heart
    heart_rate:       Optional[Decimal] = Field(default=None, description="bpm")
    heart_rhythm:     Optional[str]     = Field(default=None,
                                                 description="regular | irregular")
    # Temperature
    temperature:      Optional[Decimal] = Field(default=None, description="°C")
    temp_site:        Optional[str]     = Field(default=None,
                                                 description="oral | axillary | tympanic | rectal")
    # SpO2
    spo2:             Optional[Decimal] = Field(default=None, description="%")
    on_oxygen:        Optional[bool]    = None
    # Respiratory
    respiratory_rate: Optional[Decimal] = Field(default=None, description="breaths/min")
    # Anthropometry
    weight_kg:        Optional[Decimal] = None
    height_cm:        Optional[Decimal] = None
    bmi:              Optional[Decimal] = Field(default=None,
                                                description="Auto-computed if not provided")
    # Pain
    pain_score:       Optional[int]     = Field(default=None, ge=0, le=10)

    from pydantic import model_validator

    @model_validator(mode="after")
    def at_least_one_vital(self) -> "CreateVitalSignsEventSchema":
        vital_fields = [
            "systolic_bp", "diastolic_bp", "heart_rate", "temperature",
            "spo2", "respiratory_rate", "weight_kg", "height_cm",
        ]
        if not any(getattr(self, f) is not None for f in vital_fields):
            raise ValueError("At least one vital sign value must be provided.")
        return self


# ===========================================================================
# NEW RESPONSE SCHEMAS
# ===========================================================================

class AllergyEventDetailResponse(Schema):
    substance_name:    str
    coding_system:     Optional[str]
    coding_code:       Optional[str]
    coding_display:    Optional[str]
    allergy_type:      str
    category:          str
    criticality:       str
    reaction_type:     Optional[str]
    reaction_severity: Optional[str]
    clinical_status:   str
    onset_date:        Optional[date]
    notes:             Optional[str]

    class Config:
        from_attributes = True


class VaccinationEventDetailResponse(Schema):
    vaccine_name:        str
    coding_system:       Optional[str]
    coding_code:         Optional[str]
    dose_number:         Optional[str]
    lot_number:          Optional[str]
    administered_date:   Optional[date]
    next_dose_due_date:  Optional[date]
    administering_org:   Optional[str]
    site:                Optional[str]
    route:               Optional[str]
    notes:               Optional[str]

    class Config:
        from_attributes = True


class ConsultationEventDetailResponse(Schema):
    department:                 str
    sub_specialty:              Optional[str]
    consulting_practitioner_id: Optional[UUID]
    referred_by_id:             Optional[UUID]
    chief_complaint:            str
    history_of_present_illness: Optional[str]
    examination_findings:       Optional[str]
    investigations_ordered:     Optional[str]
    assessment:                 Optional[str]
    plan:                       Optional[str]
    follow_up_date:             Optional[date]
    follow_up_instructions:     Optional[str]

    class Config:
        from_attributes = True


class VitalSignsEventDetailResponse(Schema):
    systolic_bp:      Optional[Decimal]
    diastolic_bp:     Optional[Decimal]
    bp_position:      Optional[str]
    heart_rate:       Optional[Decimal]
    heart_rhythm:     Optional[str]
    temperature:      Optional[Decimal]
    temp_site:        Optional[str]
    spo2:             Optional[Decimal]
    on_oxygen:        Optional[bool]
    respiratory_rate: Optional[Decimal]
    weight_kg:        Optional[Decimal]
    height_cm:        Optional[Decimal]
    bmi:              Optional[Decimal]
    pain_score:       Optional[int]
    notes:            Optional[str]

    class Config:
        from_attributes = True


# ===========================================================================
# SLIM LIST RESPONSE SCHEMAS
# ===========================================================================

class AllergyListItemSchema(Schema):
    """
    Slim allergy response for list views.
    Criticality surfaced at the top level — never buried.
    """
    event_id:          UUID
    substance_name:    str
    allergy_type:      str
    category:          str
    criticality:       str           # high always shown in red on frontend
    reaction_type:     Optional[str]
    reaction_severity: Optional[str]
    clinical_status:   str
    onset_date:        Optional[date]
    coding_code:       Optional[str]
    verification_level: str
    clinical_date:     str

    class Config:
        from_attributes = True


class VaccinationListItemSchema(Schema):
    event_id:           UUID
    vaccine_name:       str
    coding_code:        Optional[str]
    dose_number:        Optional[str]
    administered_date:  Optional[date]
    next_dose_due_date: Optional[date]
    administering_org:  Optional[str]
    verification_level: str
    clinical_date:      str

    class Config:
        from_attributes = True


class ConsultationListItemSchema(Schema):
    event_id:                    UUID
    department:                  str
    sub_specialty:               Optional[str]
    consulting_practitioner_name: Optional[str]
    chief_complaint:             str
    assessment:                  Optional[str]
    follow_up_date:              Optional[date]
    verification_level:          str
    clinical_date:               str

    class Config:
        from_attributes = True


class VitalSignsListItemSchema(Schema):
    event_id:        UUID
    systolic_bp:     Optional[Decimal]
    diastolic_bp:    Optional[Decimal]
    heart_rate:      Optional[Decimal]
    temperature:     Optional[Decimal]
    spo2:            Optional[Decimal]
    respiratory_rate: Optional[Decimal]
    weight_kg:       Optional[Decimal]
    height_cm:       Optional[Decimal]
    bmi:             Optional[Decimal]
    pain_score:      Optional[int]
    verification_level: str
    source_type:     str
    clinical_date:   str

    class Config:
        from_attributes = True


class DocumentDownloadSchema(Schema):
    """Response for document download URL generation."""
    event_id:          UUID
    original_filename: Optional[str]
    document_type:     str
    file_type:         str
    file_size_bytes:   Optional[int]
    download_url:      str           # presigned S3 URL — time-limited
    expires_in_seconds: int