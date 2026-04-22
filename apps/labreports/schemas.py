"""
lab_reports/schemas.py
======================
Input and response schemas for lab reports.
Field names match the actual models exactly.

Model field reference:
  LabReport:      source, integration, uploading_organisation, lab_name,
                  report_date, report_id, panel, document_event,
                  ocr_provider, ocr_confidence, ocr_completed_at,
                  status, confirmed_at, resulted_at

  LabReportField: test_name, loinc_code, loinc_display,
                  extracted_value, extracted_unit,
                  patient_corrected_value, patient_corrected_unit,
                  reference_range, is_abnormal, abnormal_flag,
                  field_confidence, status, reviewed_at, resulting_event

  LabIntegration: name, protocol, endpoint, auto_import, is_active
  LabPanel:       name, display_name, loinc_code
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import UUID

from ninja import Schema
from pydantic import Field


# ===========================================================================
# INPUT SCHEMAS
# ===========================================================================

class UploadLabReportSchema(Schema):
    """
    Metadata submitted alongside a file upload (multipart/form-data).
    File bytes are handled separately in the API layer.
    """
    lab_name:   Optional[str]  = Field(default=None, max_length=255,
                                        description="Lab name e.g. 'Apollo Diagnostics'")
    report_date: Optional[date] = Field(
        default=None,
        description="Date printed on the report — not the upload date.",
    )
    report_id:  Optional[str]  = Field(default=None, max_length=100,
                                        description="Lab's own reference/accession number.")
    notes:      Optional[str]  = None


class ManualFieldSchema(Schema):
    """A single manually-entered lab value."""
    test_name:        str            = Field(..., min_length=1, max_length=255)
    loinc_code:       Optional[str]  = Field(default=None, max_length=20)
    loinc_display:    Optional[str]  = Field(default=None, max_length=255)
    value:            str            = Field(..., min_length=1, max_length=500,
                                             description="The measured value e.g. '5.4'")
    unit:             Optional[str]  = Field(default=None, max_length=50)
    reference_range:  Optional[str]  = Field(default=None, max_length=200,
                                              description="e.g. '4.5–5.5 g/dL'")
    is_abnormal:      Optional[bool] = None
    abnormal_flag:    Optional[str]  = Field(default=None, max_length=10,
                                              description="H | L | HH | LL | N")
    display_order:    int            = Field(default=0)


class ManualLabReportSchema(Schema):
    """Create a lab report by typing values directly — no file upload needed."""
    lab_name:    Optional[str]  = Field(default=None, max_length=255)
    report_date: Optional[date] = None
    report_id:   Optional[str]  = None
    notes:       Optional[str]  = None
    fields:      List[ManualFieldSchema] = Field(
        ..., min_length=1,
        description="At least one lab value required.",
    )


class ReviewFieldSchema(Schema):
    """Patient reviews a single OCR-extracted field."""
    confirmed_value: Optional[str]  = Field(
        default=None, max_length=500,
        description=(
            "Corrected value if OCR extracted it wrong. "
            "Omit (or pass null) to accept the extracted value as-is."
        ),
    )
    confirmed_unit:  Optional[str]  = Field(default=None, max_length=50)
    reject:          bool           = Field(
        default=False,
        description="True to exclude this field — it will not become an ObservationEvent.",
    )
    rejection_reason: Optional[str] = Field(default=None, max_length=500)


class BulkConfirmSchema(Schema):
    """
    Patient confirms all unreviewed fields at once.
    confirm_all=True accepts all extracted values without per-field review.
    Per-field overrides can still be passed in fields dict.
    """
    confirm_all: bool = Field(
        default=True,
        description="Accept all extracted values as-is.",
    )
    fields: Optional[Dict[str, ReviewFieldSchema]] = Field(
        default=None,
        description="Optional per-field overrides: {field_id: ReviewFieldSchema}",
    )


class ReceiveFromOrgSchema(Schema):
    """
    Organisation or lab pushes structured results for a patient.
    Used by the org-facing integration endpoint.
    """
    patient_mrn:      Optional[str]  = Field(
        default=None,
        description="Patient MRN at this organisation — used to find the patient.",
    )
    patient_id:       Optional[UUID] = Field(
        default=None,
        description="UHR patient UUID — takes precedence over MRN if provided.",
    )
    lab_name:         Optional[str]  = None
    report_date:      Optional[date] = None
    report_id:        Optional[str]  = Field(
        default=None,
        description="Lab's own accession number — used for deduplication.",
    )
    notes:            Optional[str]  = None
    fields:           List[ManualFieldSchema] = Field(..., min_length=1)


class OCRFieldSchema(Schema):
    """A single field extracted by an OCR engine."""
    test_name:       str
    value:           str
    unit:            Optional[str]  = None
    loinc_code:      Optional[str]  = None
    loinc_display:   Optional[str]  = None
    reference_range: Optional[str]  = None
    is_abnormal:     Optional[bool] = None
    abnormal_flag:   Optional[str]  = None
    confidence:      Optional[float] = None
    display_order:   int            = 0


class OCRResultSchema(Schema):
    """
    Posted by the background OCR task when extraction completes.
    Internal endpoint — not exposed to patients.
    """
    ocr_provider:   str
    ocr_confidence: Optional[float] = None
    ocr_raw_output: Optional[dict]  = None
    error_message:  Optional[str]   = None
    fields:         List[OCRFieldSchema] = Field(default=[])


# ===========================================================================
# RESPONSE SCHEMAS
# ===========================================================================

class LabFieldResponse(Schema):
    """Response for a single LabReportField."""
    id:                      UUID
    test_name:               str
    loinc_code:              Optional[str]
    loinc_display:           Optional[str]
    extracted_value:         str
    extracted_unit:          Optional[str]
    patient_corrected_value: Optional[str]
    patient_corrected_unit:  Optional[str]
    effective_value:         str            # confirmed_value property
    effective_unit:          Optional[str]  # confirmed_unit property
    reference_range:         Optional[str]
    is_abnormal:             Optional[bool]
    abnormal_flag:           Optional[str]
    field_confidence:        Optional[float]
    status:                  str
    resulting_event_id:      Optional[UUID]
    display_order:           int

    class Config:
        from_attributes = True


class LabPanelResponse(Schema):
    id:           UUID
    name:         str
    display_name: str
    loinc_code:   Optional[str]
    fields:       List[LabFieldResponse] = []

    class Config:
        from_attributes = True


class LabReportResponse(Schema):
    id:                    UUID
    patient_id:            UUID
    source:                str
    status:                str
    lab_name:              Optional[str]
    report_date:           Optional[date]
    report_id:             Optional[str]
    integration_id:        Optional[UUID]
    uploading_org_id:      Optional[UUID]
    uploading_org_name:    Optional[str]
    ocr_provider:          Optional[str]
    ocr_confidence:        Optional[float]
    ocr_completed_at:      Optional[datetime]
    ocr_error_message:     Optional[str]
    confirmed_at:          Optional[datetime]
    resulted_at:           Optional[datetime]
    pending_fields:        int
    total_fields:          int
    abnormal_field_count:  int
    panel:                 Optional[LabPanelResponse]
    ungrouped_fields:      List[LabFieldResponse] = []
    notes:                 Optional[str]
    created_at:            datetime

    class Config:
        from_attributes = True


class LabReportSummarySchema(Schema):
    """Slim response for patient's report list."""
    id:           UUID
    source:       str
    status:       str
    lab_name:     Optional[str]
    report_date:  Optional[date]
    total_fields: int
    pending_fields: int
    abnormal_count: int
    resulted_at:  Optional[datetime]
    created_at:   datetime

    class Config:
        from_attributes = True


class LabIntegrationResponse(Schema):
    id:           UUID
    organisation_id: UUID
    name:         str
    protocol:     str
    auto_import:  bool
    is_active:    bool
    last_sync_at: Optional[datetime]
    created_at:   datetime

    class Config:
        from_attributes = True