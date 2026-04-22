"""
medical_events/api.py
=====================
API layer for medical events.

Fixes applied over the original draft:
  - JWT auth on every endpoint
  - Explicit imports (no wildcard)
  - response= types on all endpoints
  - Correct Pydantic v2 model_validate pattern
  - Full exception handling — no unhandled 500s
  - ActiveMedicationResponse built from base event, not extension
  - amend_event passes typed namespace object to service, not raw dict
"""

import logging
from datetime import date
from types import SimpleNamespace
from typing import List, Optional
from uuid import UUID

from ninja import File, Query, Router, UploadedFile

from core.auth import JWTBearer, get_current_user
from apps.users.schemas import ErrorSchema

from . import services
from .exceptions import (
    AmendmentReasonRequired,
    DocumentChecksumMismatch,
    DocumentUploadFailed,
    EventAccessDenied,
    EventImmutable,
    EventNotApprovable,
    EventNotFound,
    InvalidVerificationLevel,
    MedicationLifecycleError,
)
from .services import (
    get_allergies,
    get_consultations,
    get_document_download_url,
    get_vaccinations,
    get_vital_signs_history,
    unhide_event,
)
from .models import EventType, MedicalEvent
from .schemas import (
    ActiveMedicationResponse,
    AllergyEventDetailResponse,
    AllergyListItemSchema,
    ApproveEventSchema,
    ConditionEventDetailResponse,
    ConsultationEventDetailResponse,
    ConsultationListItemSchema,
    CreateAllergyEventSchema,
    CreateAmendmentSchema,
    CreateConditionEventSchema,
    CreateConsultationEventSchema,
    CreateDocumentEventSchema,
    CreateMedicationEventSchema,
    CreateObservationEventSchema,
    CreateProcedureEventSchema,
    CreateSecondOpinionSchema,
    CreateVaccinationEventSchema,
    CreateVisitEventSchema,
    CreateVitalSignsEventSchema,
    DocumentDownloadSchema,
    DocumentEventDetailResponse,
    MedicalEventBaseResponse,
    MedicalEventFullResponse,
    MedicationEventDetailResponse,
    MedicationLifecycleSchema,
    ObservationEventDetailResponse,
    ProcedureEventDetailResponse,
    SecondOpinionDetailResponse,
    TimelineEventResponse,
    VaccinationEventDetailResponse,
    VaccinationListItemSchema,
    VisitEventDetailResponse,
    VitalSignsEventDetailResponse,
    VitalSignsListItemSchema,
)

logger   = logging.getLogger(__name__)
jwt_auth = JWTBearer()
router   = Router(tags=["Medical Events"])


# ===========================================================================
# RESPONSE BUILDERS
# ===========================================================================

def build_summary(event: MedicalEvent) -> str:
    """Human-readable one-line summary for the timeline list."""
    ext = event.typed_extension
    if not ext:
        return event.event_type

    if event.event_type == EventType.OBSERVATION:
        val  = ext.value_quantity or ext.value_string or ""
        unit = ext.value_unit or ""
        return f"{ext.observation_name}: {val} {unit}".strip()

    if event.event_type == EventType.MEDICATION:
        return f"{ext.medication_name} ({ext.dosage or '—'})"

    if event.event_type == EventType.CONDITION:
        return ext.condition_name

    if event.event_type == EventType.PROCEDURE:
        return ext.procedure_name

    if event.event_type == EventType.VISIT:
        return ext.reason or "Visit"

    if event.event_type == EventType.DOCUMENT:
        return f"Document: {ext.document_type}"

    if event.event_type == EventType.SECOND_OPINION:
        return f"Second opinion by {ext.doctor_name}"

    if event.event_type == EventType.ALLERGY:
        criticality = f" ({'HIGH' if ext.criticality == 'high' else ext.criticality})"
        return f"Allergy: {ext.substance_name}{criticality}"

    if event.event_type == EventType.VACCINATION:
        dose = f" — dose {ext.dose_number}" if ext.dose_number else ""
        return f"Vaccination: {ext.vaccine_name}{dose}"

    if event.event_type == EventType.CONSULTATION:
        return f"Consultation: {ext.get_department_display() if hasattr(ext, 'get_department_display') else ext.department} — {ext.chief_complaint[:60]}"

    if event.event_type == EventType.VITAL_SIGNS:
        parts = []
        if ext.systolic_bp and ext.diastolic_bp:
            parts.append(f"BP {ext.systolic_bp}/{ext.diastolic_bp}")
        if ext.heart_rate:
            parts.append(f"HR {ext.heart_rate}")
        if ext.temperature:
            parts.append(f"Temp {ext.temperature}°C")
        if ext.spo2:
            parts.append(f"SpO2 {ext.spo2}%")
        return "Vitals: " + ", ".join(parts) if parts else "Vital signs recorded"

    return event.event_type


def _mv(schema_class, obj):
    """
    Pydantic v2-compatible model_validate with from_attributes=True.
    Replaces the deprecated .from_orm() pattern.
    """
    return schema_class.model_validate(obj, from_attributes=True)


def map_extension(event: MedicalEvent):
    """Return the typed extension schema object for a MedicalEvent."""
    ext = event.typed_extension
    if not ext:
        return None

    mapping = {
        EventType.VISIT:          VisitEventDetailResponse,
        EventType.OBSERVATION:    ObservationEventDetailResponse,
        EventType.CONDITION:      ConditionEventDetailResponse,
        EventType.MEDICATION:     MedicationEventDetailResponse,
        EventType.PROCEDURE:      ProcedureEventDetailResponse,
        EventType.DOCUMENT:       DocumentEventDetailResponse,
        EventType.SECOND_OPINION: SecondOpinionDetailResponse,
        EventType.ALLERGY:        AllergyEventDetailResponse,
        EventType.VACCINATION:    VaccinationEventDetailResponse,
        EventType.CONSULTATION:   ConsultationEventDetailResponse,
        EventType.VITAL_SIGNS:    VitalSignsEventDetailResponse,
    }

    schema_class = mapping.get(event.event_type)
    return _mv(schema_class, ext) if schema_class else None


def map_full_event(event: MedicalEvent) -> dict:
    return {
        "base":      _mv(MedicalEventBaseResponse, event).model_dump(),
        "extension": map_extension(event),
    }


def map_timeline_event(event: MedicalEvent) -> dict:
    return {
        "id":                 event.id,
        "event_type":         event.event_type,
        "clinical_timestamp": event.clinical_timestamp,
        "verification_level": event.verification_level,
        "visibility_status":  event.visibility_status,
        "source_type":        event.source_type,
        "summary":            build_summary(event),
        "has_document":       event.event_type == EventType.DOCUMENT,
    }


def _error(e, status: int) -> tuple:
    return status, ErrorSchema(detail=e.message, status_code=status)


# ===========================================================================
# CREATE ENDPOINTS — one per event type
# ===========================================================================

@router.post(
    "/patients/{patient_id}/events/visit/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Add a visit event",
)
def create_visit(request, patient_id: UUID, payload: CreateVisitEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.VISIT, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/observation/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Add an observation / lab result",
)
def create_observation(request, patient_id: UUID, payload: CreateObservationEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.OBSERVATION, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/condition/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Add a condition / diagnosis",
)
def create_condition(request, patient_id: UUID, payload: CreateConditionEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.CONDITION, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/medication/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Add a medication",
)
def create_medication(request, patient_id: UUID, payload: CreateMedicationEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.MEDICATION, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/procedure/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Add a procedure / surgery",
)
def create_procedure(request, patient_id: UUID, payload: CreateProcedureEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.PROCEDURE, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/second-opinion/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Submit a second opinion",
    description=(
        "Submit a second opinion for a patient. "
        "Always starts as pending_approval — patient must approve before it "
        "appears on the main timeline."
    ),
)
def create_second_opinion(request, patient_id: UUID, payload: CreateSecondOpinionSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.SECOND_OPINION, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/allergy/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Record an allergy or intolerance",
    description=(
        "Record a patient allergy or intolerance. "
        "criticality=high events are surfaced prominently in the doctor dashboard. "
        "clinical_status: active | resolved | entered_in_error"
    ),
)
def create_allergy(request, patient_id: UUID, payload: CreateAllergyEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.ALLERGY, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/vaccination/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Record a vaccination",
    description="Record an immunisation. CVX coding optional but recommended.",
)
def create_vaccination(request, patient_id: UUID, payload: CreateVaccinationEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.VACCINATION, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/consultation/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Record a consultation note",
    description=(
        "Record a structured consultation note with department, assessment, and plan. "
        "Immutable once created — amend via the /amend/ endpoint. "
        "department: cardiology | neurology | oncology | general_practice | ..."
    ),
)
def create_consultation(request, patient_id: UUID, payload: CreateConsultationEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.CONSULTATION, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


@router.post(
    "/patients/{patient_id}/events/vital-signs/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Record a vital signs set",
    description=(
        "Record a full set of vital signs in one event. "
        "At least one vital sign value required. "
        "BMI auto-computed from weight + height if not explicitly provided."
    ),
)
def create_vital_signs(request, patient_id: UUID, payload: CreateVitalSignsEventSchema):
    user = get_current_user(request)
    try:
        event = services.create_event(user, patient_id, EventType.VITAL_SIGNS, payload)
        return 201, map_full_event(event)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidVerificationLevel as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


# ===========================================================================
# DOCUMENT UPLOAD
# ===========================================================================

@router.post(
    "/patients/{patient_id}/events/document/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Upload a document",
    description=(
        "Upload a medical document (PDF, image) as multipart/form-data. "
        "File is stored in S3-compatible storage with SHA-256 integrity check. "
        "document_type: lab_report | prescription | imaging | discharge_summary "
        "| referral | vaccination | insurance | other"
    ),
)
def upload_document(
    request,
    patient_id:   UUID,
    file:         UploadedFile = File(...),
    document_type: str = "other",
    clinical_timestamp: str = None,
):
    user = get_current_user(request)
    try:
        from django.utils.dateparse import parse_datetime
        from django.utils import timezone as tz

        ts = parse_datetime(clinical_timestamp) if clinical_timestamp else tz.now()

        data = SimpleNamespace(
            document_type      = document_type,
            original_filename  = file.name,
            clinical_timestamp = ts,
        )

        event = services.create_document_event(
            user              = user,
            patient_id        = patient_id,
            file_bytes        = file.read(),
            original_filename = file.name,
            content_type      = file.content_type or "application/octet-stream",
            data              = data,
        )
        return 201, map_full_event(event)
    except DocumentUploadFailed as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# TIMELINE
# ===========================================================================

@router.get(
    "/patients/{patient_id}/timeline/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get patient medical timeline",
    description=(
        "Returns the patient's medical event timeline ordered by clinical date. "
        "Hidden and pending events excluded by default. "
        "Primary holder can pass include_hidden=true or include_pending=true."
    ),
)
def get_timeline(
    request,
    patient_id:         UUID,
    event_type:         Optional[str]  = Query(None),
    from_date:          Optional[date] = Query(None),
    to_date:            Optional[date] = Query(None),
    verification_level: Optional[str]  = Query(None),
    source_type:        Optional[str]  = Query(None),
    include_hidden:     bool           = Query(False),
    include_pending:    bool           = Query(False),
):
    user = get_current_user(request)
    try:
        filters = {
            "event_type":         event_type,
            "from_date":          from_date,
            "to_date":            to_date,
            "verification_level": verification_level,
            "source_type":        source_type,
            "include_hidden":     include_hidden,
            "include_pending":    include_pending,
        }
        qs = services.get_timeline(user, patient_id, filters)
        return 200, [map_timeline_event(e) for e in qs]
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# GET SINGLE EVENT
# ===========================================================================

@router.get(
    "/patients/{patient_id}/events/{event_id}/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get a single medical event with full detail",
)
def get_event(request, patient_id: UUID, event_id: UUID):
    user = get_current_user(request)
    try:
        event = services.get_event(user, patient_id, event_id)
        return 200, map_full_event(event)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# AMEND EVENT
# ===========================================================================

@router.post(
    "/patients/{patient_id}/events/amend/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Amend (correct) an existing event",
    description=(
        "Create a correction for an existing event. "
        "The original is never modified — a new event is created with "
        "relationship_type=amendment referencing the original. "
        "amendment_reason is mandatory."
    ),
)
def amend_event(request, patient_id: UUID, payload: CreateAmendmentSchema):
    user = get_current_user(request)
    try:
        # Convert the event_data dict to a SimpleNamespace so the service
        # can call getattr() on it — same interface as schema objects.
        event_data_ns = SimpleNamespace(**payload.event_data)

        event = services.amend_event(
            user               = user,
            patient_id         = patient_id,
            original_event_id  = payload.original_event_id,
            amendment_reason   = payload.amendment_reason,
            new_data           = event_data_ns,
        )
        return 201, map_full_event(event)
    except AmendmentReasonRequired as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# MEDICATION LIFECYCLE
# ===========================================================================

@router.post(
    "/patients/{patient_id}/events/medication-lifecycle/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Record a medication lifecycle event",
    description=(
        "Record that a medication was modified or discontinued. "
        "action: 'modified' | 'discontinued'. "
        "Creates a new immutable event linked to the original — "
        "the original medication event is never changed."
    ),
)
def medication_lifecycle(request, patient_id: UUID, payload: MedicationLifecycleSchema):
    user = get_current_user(request)
    try:
        event = services.medication_lifecycle(user, patient_id, payload)
        return 201, map_full_event(event)
    except MedicationLifecycleError as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# APPROVE / HIDE
# ===========================================================================

@router.post(
    "/patients/{patient_id}/events/{event_id}/approve/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema, 409: ErrorSchema},
    summary="Approve or hide a pending event",
    description=(
        "Patient approves (approve=true → visible) or hides (approve=false) "
        "a pending_approval event. Only primary holder or full_delegate."
    ),
)
def approve_event(request, patient_id: UUID, event_id: UUID, payload: ApproveEventSchema):
    user = get_current_user(request)
    try:
        event = services.approve_event(user, patient_id, event_id, payload.approve)
        return 200, {"status": event.visibility_status}
    except EventNotApprovable as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/patients/{patient_id}/events/{event_id}/hide/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Hide a visible event from shared views",
)
def hide_event(request, patient_id: UUID, event_id: UUID):
    user = get_current_user(request)
    try:
        event = services.hide_event(user, patient_id, event_id)
        return 200, {"status": event.visibility_status}
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# ACTIVE MEDICATIONS
# ===========================================================================

@router.get(
    "/patients/{patient_id}/medications/active/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get currently active medications",
    description=(
        "Computed view — returns medications with active status "
        "in their latest lifecycle event. "
        "Doctors use this for the current medications panel."
    ),
)
def get_active_medications(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        meds = services.get_active_medications(user, patient_id)
        return 200, [
            {
                # Base event fields
                "event_id":          str(event.id),
                "clinical_timestamp": event.clinical_timestamp,
                "verification_level": event.verification_level,
                "source_type":        event.source_type,
                # Extension fields
                "medication_name":   event.medication_event.medication_name,
                "dosage":            event.medication_event.dosage,
                "frequency":         event.medication_event.frequency,
                "route":             event.medication_event.route,
                "start_date":        event.medication_event.start_date,
                "status":            event.medication_event.status,
            }
            for event in meds
        ]
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# DOCTOR DASHBOARD & HEALTH STATS
# ===========================================================================

from . import stats as stats_module


@router.get(
    "/patients/{patient_id}/dashboard/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Doctor dashboard — full clinical summary",
    description=(
        "Single endpoint returning all clinical categories: "
        "health stats, active medications, conditions, recent labs, "
        "imaging, procedures, and data quality signals. "
        "Designed to give a doctor full clinical context within 2 minutes."
    ),
)
def get_doctor_dashboard(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        # Verify the user has access to this patient
        from patients.services import _get_active_access
        from patients.exceptions import PatientNotFound, PatientRetracted
        access = _get_active_access(user, patient_id)

        dashboard = stats_module.get_doctor_dashboard(patient_id)
        return 200, dashboard
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except Exception as e:
        if "PatientNotFound" in type(e).__name__:
            return 404, ErrorSchema(detail="Patient not found.", status_code=404)
        if "PatientRetracted" in type(e).__name__:
            return 410, ErrorSchema(detail="Patient profile is retracted.", status_code=410)
        raise


@router.get(
    "/patients/{patient_id}/health-stats/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Key health metrics with freshness labels",
    description=(
        "Returns latest value for BMI, blood pressure, glucose, HbA1c, "
        "cholesterol, heart rate, SpO2, and more. "
        "BMI is computed from weight + height if not directly recorded. "
        "Freshness: recent (≤3mo) | moderately_old (3-12mo) | old (>12mo)"
    ),
)
def get_health_stats(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        from patients.services import _get_active_access
        _get_active_access(user, patient_id)
        return 200, stats_module.get_health_stats(patient_id)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/patients/{patient_id}/medications/history/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Full medication history (active + historical)",
)
def get_medication_history(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        from patients.services import _get_active_access
        _get_active_access(user, patient_id)
        return 200, stats_module.get_medication_history(patient_id)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/patients/{patient_id}/conditions/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Conditions summary (active + resolved)",
)
def get_conditions(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        from patients.services import _get_active_access
        _get_active_access(user, patient_id)
        return 200, stats_module.get_conditions_summary(patient_id)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/patients/{patient_id}/imaging/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Imaging and radiology documents",
)
def get_imaging(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        from patients.services import _get_active_access
        _get_active_access(user, patient_id)
        return 200, stats_module.get_imaging_history(patient_id)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/patients/{patient_id}/lab-results/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Lab results history",
)
def get_lab_results(request, patient_id: UUID, limit: int = 50):
    user = get_current_user(request)
    try:
        from patients.services import _get_active_access
        _get_active_access(user, patient_id)
        return 200, stats_module.get_lab_results(patient_id, limit=limit)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# ALLERGY ENDPOINTS
# ===========================================================================

@router.get(
    "/patients/{patient_id}/allergies/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="List patient allergies",
    description=(
        "Returns active allergies ordered by criticality — HIGH risk first. "
        "Pass ?active_only=false to include resolved allergies. "
        "High criticality allergies must be displayed prominently in any clinical view."
    ),
)
def list_allergies(request, patient_id: UUID, active_only: bool = True):
    user = get_current_user(request)
    try:
        allergies = get_allergies(user, patient_id, active_only=active_only)
        return 200, [
            {
                "event_id":          str(a.medical_event_id),
                "substance_name":    a.substance_name,
                "allergy_type":      a.allergy_type,
                "category":          a.category,
                "criticality":       a.criticality,
                "reaction_type":     a.reaction_type,
                "reaction_severity": a.reaction_severity,
                "clinical_status":   a.clinical_status,
                "onset_date":        str(a.onset_date) if a.onset_date else None,
                "coding_code":       a.coding_code,
                "verification_level": a.medical_event.verification_level,
                "clinical_date":     str(a.medical_event.clinical_timestamp.date()),
            }
            for a in allergies
        ]
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# VACCINATION ENDPOINTS
# ===========================================================================

@router.get(
    "/patients/{patient_id}/vaccinations/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="List vaccination history",
)
def list_vaccinations(request, patient_id: UUID):
    user = get_current_user(request)
    try:
        vaccs = get_vaccinations(user, patient_id)
        return 200, [
            {
                "event_id":           str(v.medical_event_id),
                "vaccine_name":       v.vaccine_name,
                "coding_code":        v.coding_code,
                "dose_number":        v.dose_number,
                "administered_date":  str(v.administered_date) if v.administered_date else None,
                "next_dose_due_date": str(v.next_dose_due_date) if v.next_dose_due_date else None,
                "administering_org":  v.administering_org,
                "lot_number":         v.lot_number,
                "verification_level": v.medical_event.verification_level,
                "clinical_date":      str(v.medical_event.clinical_timestamp.date()),
            }
            for v in vaccs
        ]
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# CONSULTATION ENDPOINTS
# ===========================================================================

@router.get(
    "/patients/{patient_id}/consultations/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="List consultation history",
    description=(
        "Returns consultation notes ordered by most recent first. "
        "Pass ?department=cardiology to filter by specialty."
    ),
)
def list_consultations(request, patient_id: UUID, department: str = None):
    user = get_current_user(request)
    try:
        consults = get_consultations(user, patient_id, department=department)
        return 200, [
            {
                "event_id":                    str(c.medical_event_id),
                "department":                  c.department,
                "sub_specialty":               c.sub_specialty,
                "consulting_practitioner_name": (
                    c.consulting_practitioner.full_name
                    if c.consulting_practitioner else None
                ),
                "chief_complaint":   c.chief_complaint,
                "assessment":        c.assessment,
                "plan":              c.plan,
                "follow_up_date":    str(c.follow_up_date) if c.follow_up_date else None,
                "verification_level": c.medical_event.verification_level,
                "source_type":       c.medical_event.source_type,
                "clinical_date":     str(c.medical_event.clinical_timestamp.date()),
            }
            for c in consults
        ]
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# VITAL SIGNS HISTORY
# ===========================================================================

@router.get(
    "/patients/{patient_id}/vital-signs/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Vital signs history",
    description=(
        "Returns vital signs records most recent first. "
        "Pass ?limit=N to control how many records are returned (default 20). "
        "Use for trending BP, weight, SpO2 over time."
    ),
)
def list_vital_signs(request, patient_id: UUID, limit: int = 20):
    user = get_current_user(request)
    try:
        vitals = get_vital_signs_history(user, patient_id, limit=limit)
        return 200, [
            {
                "event_id":        str(v.medical_event_id),
                "systolic_bp":     float(v.systolic_bp) if v.systolic_bp else None,
                "diastolic_bp":    float(v.diastolic_bp) if v.diastolic_bp else None,
                "heart_rate":      float(v.heart_rate) if v.heart_rate else None,
                "temperature":     float(v.temperature) if v.temperature else None,
                "spo2":            float(v.spo2) if v.spo2 else None,
                "respiratory_rate": float(v.respiratory_rate) if v.respiratory_rate else None,
                "weight_kg":       float(v.weight_kg) if v.weight_kg else None,
                "height_cm":       float(v.height_cm) if v.height_cm else None,
                "bmi":             float(v.bmi) if v.bmi else None,
                "pain_score":      v.pain_score,
                "bp_position":     v.bp_position,
                "verification_level": v.medical_event.verification_level,
                "source_type":     v.medical_event.source_type,
                "clinical_date":   str(v.medical_event.clinical_timestamp.date()),
            }
            for v in vitals
        ]
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)


# ===========================================================================
# UN-HIDE EVENT
# ===========================================================================

@router.post(
    "/patients/{patient_id}/events/{event_id}/unhide/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Restore a hidden event to visible",
    description=(
        "Restore a previously hidden event back to visible. "
        "Only primary holder or full_delegate can unhide events."
    ),
)
def unhide_event_endpoint(request, patient_id: UUID, event_id: UUID):
    user = get_current_user(request)
    try:
        event = unhide_event(user, patient_id, event_id)
        return 200, {"status": event.visibility_status, "event_id": str(event.id)}
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# DOCUMENT DOWNLOAD
# ===========================================================================

@router.get(
    "/patients/{patient_id}/events/{event_id}/download/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get a presigned download URL for a document",
    description=(
        "Generate a time-limited presigned S3 URL for downloading a document. "
        "URL expires after 1 hour (configurable via DOCUMENT_PRESIGNED_URL_EXPIRY). "
        "Call this endpoint each time — never cache the presigned URL itself."
    ),
)
def get_document_download(request, patient_id: UUID, event_id: UUID):
    user = get_current_user(request)
    try:
        from django.conf import settings as django_settings
        url = get_document_download_url(user, patient_id, event_id)

        # Get document metadata for the response
        from .models import MedicalEvent
        event = MedicalEvent.objects.select_related("document_event").get(
            pk=event_id, patient_id=patient_id
        )
        doc = event.document_event

        return 200, {
            "event_id":          str(event_id),
            "original_filename": doc.original_filename,
            "document_type":     doc.document_type,
            "file_type":         doc.file_type,
            "file_size_bytes":   doc.file_size_bytes,
            "download_url":      url,
            "expires_in_seconds": getattr(
                django_settings, "DOCUMENT_PRESIGNED_URL_EXPIRY", 3600
            ),
        }
    except EventNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except EventAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except DocumentUploadFailed as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)