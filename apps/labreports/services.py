"""
lab_reports/services.py
========================
Service layer for lab report ingestion, extraction, and confirmation.

Function index:
  upload_lab_report(user, patient_id, file_bytes, filename, content_type, data)
    → LabReport  (Path 1: patient upload)

  receive_from_organisation(organisation, patient_id, fields_data, metadata)
    → LabReport  (Path 2: org machine push)

  receive_from_integration(integration, patient_id, fields_data, metadata)
    → LabReport  (Path 3: lab API)

  get_report(user, report_id)                    → LabReport
  list_patient_reports(user, patient_id, status) → QuerySet[LabReport]

  review_field(user, field_id, confirmed_value, confirmed_unit, reject, rejection_reason)
    → LabReportField  (patient confirms or rejects one field)

  confirm_all_fields(user, report_id)
    → LabReport  (batch confirm all unreviewed fields at once)

  result_report(user, report_id)
    → LabReport  (create ObservationEvents for all confirmed fields)

  _create_observation_from_field(field, report, verification_level)
    → MedicalEvent (internal — creates the ObservationEvent)
"""

import logging
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from patients.services import _get_active_access

from .exceptions import (
    ExtractionFailed,
    IntegrationNotActive,
    IntegrationNotFound,
    LabFieldAlreadyReviewed,
    LabFieldNotFound,
    LabReportAccessDenied,
    LabReportAlreadyResulted,
    LabReportNotFound,
    LabReportNotReviewable,
)
from .models import (
    FieldStatus,
    LabIntegration,
    LabReport,
    LabReportField,
    LabReportSource,
    LabReportStatus,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# INTERNAL HELPERS
# ===========================================================================

def _assert_can_write(user, patient_id: UUID):
    access = _get_active_access(user, patient_id)
    if not access.can_write:
        raise LabReportAccessDenied(
            f"Your role '{access.role}' does not permit adding lab reports."
        )
    return access


def _assert_can_read(user, patient_id: UUID):
    access = _get_active_access(user, patient_id)
    if not access.can_read:
        raise LabReportAccessDenied("You do not have read access to this patient's reports.")
    return access


def _parse_numeric_value(raw: str):
    """
    Try to parse a numeric value from a raw string.
    Returns (Decimal, str_remainder) or (None, raw) if not parseable.
    Handles: "5.4", "5.4 g/dL", "> 5.4", "< 0.1", "5.4-7.2"
    """
    if not raw:
        return None, raw
    import re
    # Strip comparison operators and take the numeric part
    match = re.search(r"[\d]+(?:\.\d+)?", raw.replace(",", ""))
    if match:
        try:
            return Decimal(match.group()), raw
        except InvalidOperation:
            pass
    return None, raw


def _create_observation_from_field(
    field: LabReportField,
    report: LabReport,
    verification_level: str,
) -> "MedicalEvent":
    """
    Create an ObservationEvent in medical_events/ from a confirmed LabReportField.

    The field's confirmed_value is used (patient correction takes precedence).
    Tries to parse a numeric value — falls back to string value if not parseable.

    Returns the created MedicalEvent.
    """
    from medical_events.models import (
        EventType, MedicalEvent, ObservationEvent,
        RelationshipType, SourceType, VisibilityStatus,
    )

    value_str = field.confirmed_value or ""
    numeric_value, _ = _parse_numeric_value(value_str)

    # Determine source_type from report source
    source_map = {
        LabReportSource.PATIENT_UPLOAD:    SourceType.PATIENT,
        LabReportSource.MANUAL_ENTRY:      SourceType.PATIENT,
        LabReportSource.ORGANISATION_PUSH: SourceType.LAB,
        LabReportSource.LAB_INTEGRATION:   SourceType.LAB,
    }
    source_type = source_map.get(report.source, SourceType.PATIENT)

    # Use report date or now for clinical timestamp
    from django.utils import timezone as tz
    if report.report_date:
        from datetime import datetime, time
        clinical_ts = tz.make_aware(
            datetime.combine(report.report_date, time.min)
        )
    else:
        clinical_ts = tz.now()

    # Visibility: patient uploads visible immediately.
    # Org/lab pushes go pending_approval if no auto_import consent.
    auto_import = (
        report.integration and report.integration.auto_import
    ) or report.source == LabReportSource.ORGANISATION_PUSH

    visibility = (
        VisibilityStatus.VISIBLE
        if auto_import or source_type == SourceType.PATIENT
        else VisibilityStatus.PENDING_APPROVAL
    )

    base = MedicalEvent.objects.create(
        patient              = report.patient,
        event_type           = EventType.OBSERVATION,
        clinical_timestamp   = clinical_ts,
        source_type          = source_type,
        source_practitioner  = report.ordered_by if hasattr(report, 'ordered_by') else None,
        source_organisation  = report.uploading_organisation,
        verification_level   = verification_level,
        visibility_status    = visibility,
        created_by           = report.created_by,
        relationship_type    = RelationshipType.NONE,
    )

    ObservationEvent.objects.create(
        medical_event    = base,
        observation_name = field.test_name,
        coding_system    = "http://loinc.org" if field.loinc_code else None,
        coding_code      = field.loinc_code,
        coding_display   = field.loinc_display,
        value_type       = "quantity" if numeric_value is not None else "string",
        value_quantity   = numeric_value,
        value_unit       = field.confirmed_unit,
        value_string     = value_str if numeric_value is None else None,
        reference_range  = field.reference_range,
    )

    logger.info(
        "ObservationEvent created from lab field. "
        "event_id=%s field_id=%s report_id=%s loinc=%s",
        base.id, field.id, report.id, field.loinc_code,
    )
    return base


# ===========================================================================
# PATH 1: PATIENT UPLOAD
# ===========================================================================

@transaction.atomic
def upload_lab_report(
    user,
    patient_id: UUID,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    data,
) -> LabReport:
    """
    Patient uploads a lab report PDF or image.

    Steps:
      1. Verify patient write access
      2. Create DocumentEvent in medical_events/ (source of truth)
      3. Create LabReport linked to that DocumentEvent
      4. Status = uploaded (OCR will run asynchronously)

    OCR extraction is intentionally decoupled — it runs as a background
    task (Celery / Django-Q) and calls trigger_extraction() when complete.
    This endpoint returns immediately without waiting for OCR.
    """
    access = _assert_can_write(user, patient_id)
    patient = access.patient

    # Step 1: Upload file and create DocumentEvent in medical_events/
    from medical_events.storage import upload_document
    from medical_events.models import (
        EventType, MedicalEvent, DocumentEvent,
        RelationshipType, SourceType, VerificationLevel, VisibilityStatus,
    )
    import uuid as uuid_module

    event_id = uuid_module.uuid4()

    upload_result = upload_document(
        file_bytes        = file_bytes,
        patient_id        = patient_id,
        event_id          = event_id,
        original_filename = filename,
        content_type      = content_type,
    )

    from django.utils import timezone as tz
    doc_event = MedicalEvent(
        id                 = event_id,
        patient            = patient,
        event_type         = EventType.DOCUMENT,
        clinical_timestamp = tz.now(),
        source_type        = SourceType.PATIENT,
        verification_level = VerificationLevel.SELF_REPORTED,
        visibility_status  = VisibilityStatus.VISIBLE,
        created_by         = user,
        relationship_type  = RelationshipType.NONE,
    )
    doc_event.save()

    DocumentEvent.objects.create(
        medical_event     = doc_event,
        file_url          = upload_result["file_url"],
        file_type         = content_type,
        document_type     = "lab_report",
        original_filename = filename,
        file_size_bytes   = len(file_bytes),
        checksum          = upload_result["checksum"],
        storage_provider  = "s3",
        s3_bucket         = upload_result["s3_bucket"],
        s3_key            = upload_result["s3_key"],
    )

    # Step 2: Create LabReport record
    report = LabReport.objects.create(
        patient        = patient,
        created_by     = user,
        source         = LabReportSource.PATIENT_UPLOAD,
        lab_name       = getattr(data, "lab_name", None),
        report_date    = getattr(data, "report_date", None),
        report_id      = getattr(data, "report_id", None),
        document_event = doc_event,
        status         = LabReportStatus.UPLOADED,
        notes          = getattr(data, "notes", None),
    )

    logger.info(
        "Lab report uploaded. report_id=%s patient_id=%s filename=%s",
        report.id, patient_id, filename,
    )
    return report


# ===========================================================================
# PATH 2: ORGANISATION MACHINE PUSH
# ===========================================================================

@transaction.atomic
def receive_from_organisation(
    organisation,
    patient_id: UUID,
    fields_data: list[dict],
    metadata: dict,
    created_by_user,
) -> LabReport:
    """
    Receive lab results pushed directly from an organisation's analyser or EHR.

    fields_data: list of dicts, each:
      {
        "test_name":        str,
        "loinc_code":       str | None,
        "loinc_display":    str | None,
        "value":            str,
        "unit":             str | None,
        "reference_range":  str | None,
        "is_abnormal":      bool | None,
        "abnormal_flag":    str | None,
      }

    Since the data comes from a verified machine at a verified org,
    fields are auto-resulted (no patient review required).
    verification_level = digitally_verified
    """
    from patients.models import Patient

    try:
        patient = Patient.objects.get(pk=patient_id, deleted_at__isnull=True)
    except Patient.DoesNotExist:
        from patients.exceptions import PatientNotFound
        raise PatientNotFound()

    report = LabReport.objects.create(
        patient                = patient,
        created_by             = created_by_user,
        source                 = LabReportSource.ORGANISATION_PUSH,
        uploading_organisation = organisation,
        lab_name               = metadata.get("lab_name"),
        report_date            = metadata.get("report_date"),
        report_id              = metadata.get("report_id"),
        status                 = LabReportStatus.RECEIVED,
        notes                  = metadata.get("notes"),
    )

    _create_fields_and_result(
        report             = report,
        fields_data        = fields_data,
        verification_level = "digitally_verified",
        auto_result        = True,
    )

    report.status     = LabReportStatus.RESULTED
    report.resulted_at = timezone.now()
    report.save(update_fields=["status", "resulted_at", "updated_at"])

    logger.info(
        "Lab report received from org. report_id=%s org=%s patient=%s fields=%s",
        report.id, organisation.id, patient_id, len(fields_data),
    )
    return report


# ===========================================================================
# PATH 3: LAB INTEGRATION API
# ===========================================================================

@transaction.atomic
def receive_from_integration(
    integration_id: UUID,
    patient_id: UUID,
    fields_data: list[dict],
    metadata: dict,
    created_by_user,
) -> LabReport:
    """
    Receive lab results from an external lab integration (Apollo, Thyrocare etc.).

    auto_import on the integration determines whether patient review is required.
    """
    try:
        integration = LabIntegration.objects.select_related("organisation").get(
            pk=integration_id
        )
    except LabIntegration.DoesNotExist:
        raise IntegrationNotFound()

    if not integration.is_active:
        raise IntegrationNotActive()

    from patients.models import Patient
    try:
        patient = Patient.objects.get(pk=patient_id, deleted_at__isnull=True)
    except Patient.DoesNotExist:
        from patients.exceptions import PatientNotFound
        raise PatientNotFound()

    auto_result        = integration.auto_import
    verification_level = "digitally_verified" if auto_result else "self_reported"
    status             = LabReportStatus.RESULTED if auto_result else LabReportStatus.PENDING_REVIEW

    report = LabReport.objects.create(
        patient                = patient,
        created_by             = created_by_user,
        source                 = LabReportSource.LAB_INTEGRATION,
        integration            = integration,
        uploading_organisation = integration.organisation,
        lab_name               = metadata.get("lab_name", integration.name),
        report_date            = metadata.get("report_date"),
        report_id              = metadata.get("report_id"),
        status                 = status,
        notes                  = metadata.get("notes"),
    )

    _create_fields_and_result(
        report             = report,
        fields_data        = fields_data,
        verification_level = verification_level,
        auto_result        = auto_result,
    )

    if auto_result:
        report.resulted_at = timezone.now()
        report.save(update_fields=["resulted_at", "updated_at"])

    # Update last sync timestamp on the integration
    integration.last_sync_at = timezone.now()
    integration.save(update_fields=["last_sync_at"])

    logger.info(
        "Lab report received from integration. report_id=%s integration=%s patient=%s auto=%s",
        report.id, integration_id, patient_id, auto_result,
    )
    return report


def _create_fields_and_result(
    report: LabReport,
    fields_data: list[dict],
    verification_level: str,
    auto_result: bool,
) -> None:
    """Create LabReportField records and optionally auto-create ObservationEvents."""
    for i, fd in enumerate(fields_data):
        field = LabReportField.objects.create(
            lab_report      = report,
            test_name       = fd.get("test_name", "Unknown"),
            loinc_code      = fd.get("loinc_code"),
            loinc_display   = fd.get("loinc_display"),
            extracted_value = str(fd.get("value", "")),
            extracted_unit  = fd.get("unit"),
            reference_range = fd.get("reference_range"),
            is_abnormal     = fd.get("is_abnormal"),
            abnormal_flag   = fd.get("abnormal_flag"),
            status          = FieldStatus.RESULTED if auto_result else FieldStatus.EXTRACTED,
            display_order   = i,
        )

        if auto_result:
            obs_event = _create_observation_from_field(field, report, verification_level)
            field.resulting_event = obs_event
            field.save(update_fields=["resulting_event", "status"])


# ===========================================================================
# GET / LIST
# ===========================================================================

def get_report(user, report_id: UUID) -> LabReport:
    access = _get_active_access(user, None) if False else None  # placeholder
    try:
        report = LabReport.objects.select_related(
            "patient", "integration", "uploading_organisation", "panel"
        ).prefetch_related("fields").get(pk=report_id)
    except LabReport.DoesNotExist:
        raise LabReportNotFound()

    # Verify the requesting user has access to this patient
    _assert_can_read(user, report.patient_id)
    return report


def list_patient_reports(user, patient_id: UUID, status: str = None):
    _assert_can_read(user, patient_id)

    qs = (
        LabReport.objects
        .filter(patient_id=patient_id)
        .select_related("integration", "uploading_organisation", "panel")
        .order_by("-created_at")
    )

    if status:
        qs = qs.filter(status=status)

    return qs


# ===========================================================================
# FIELD REVIEW — patient confirms or rejects individual fields
# ===========================================================================

@transaction.atomic
def review_field(
    user,
    field_id: UUID,
    confirmed_value: str = None,
    confirmed_unit:  str = None,
    reject:          bool = False,
    rejection_reason: str = None,
) -> LabReportField:
    """
    Patient reviews a single extracted field.

    If confirmed_value matches extracted_value → status = CONFIRMED
    If confirmed_value differs from extracted_value → status = CORRECTED
    If reject=True → status = REJECTED (field will not become an ObservationEvent)
    """
    try:
        field = LabReportField.objects.select_for_update().select_related(
            "lab_report__patient"
        ).get(pk=field_id)
    except LabReportField.DoesNotExist:
        raise LabFieldNotFound()

    _assert_can_write(user, field.lab_report.patient_id)

    if field.status not in (FieldStatus.EXTRACTED,):
        raise LabFieldAlreadyReviewed(
            f"This field has already been {field.status}."
        )

    now = timezone.now()

    if reject:
        field.status          = FieldStatus.REJECTED
        field.reviewed_at     = now
        field.reviewed_by     = user
        field.rejection_reason = rejection_reason
    else:
        # Determine if the patient corrected the value
        effective_value = confirmed_value or field.extracted_value
        effective_unit  = confirmed_unit  or field.extracted_unit

        if effective_value != field.extracted_value or effective_unit != field.extracted_unit:
            field.patient_corrected_value = effective_value
            field.patient_corrected_unit  = effective_unit
            field.status                  = FieldStatus.CORRECTED
        else:
            field.status = FieldStatus.CONFIRMED

        field.reviewed_at = now
        field.reviewed_by = user

    field.save(update_fields=[
        "status", "reviewed_at", "reviewed_by",
        "patient_corrected_value", "patient_corrected_unit",
        "rejection_reason", "updated_at",
    ])

    return field


# ===========================================================================
# CONFIRM ALL — batch confirm all unreviewed fields
# ===========================================================================

@transaction.atomic
def confirm_all_fields(user, report_id: UUID) -> LabReport:
    """
    Batch confirm all unreviewed (EXTRACTED) fields at their extracted values.
    Useful when the patient trusts the extraction and doesn't want to review one-by-one.
    """
    try:
        report = LabReport.objects.select_for_update().get(pk=report_id)
    except LabReport.DoesNotExist:
        raise LabReportNotFound()

    _assert_can_write(user, report.patient_id)

    now = timezone.now()
    unreviewed = report.fields.filter(status=FieldStatus.EXTRACTED)
    unreviewed.update(
        status      = FieldStatus.CONFIRMED,
        reviewed_at = now,
        reviewed_by = user,
    )

    report.status       = LabReportStatus.CONFIRMED
    report.confirmed_at = now
    report.confirmed_by = user
    report.save(update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"])

    return report


# ===========================================================================
# RESULT REPORT — create ObservationEvents for all confirmed fields
# ===========================================================================

@transaction.atomic
def result_report(user, report_id: UUID) -> LabReport:
    """
    Create ObservationEvents in medical_events/ for all confirmed/corrected fields.

    Can be called:
      - Automatically after confirm_all_fields()
      - Manually by the patient after reviewing individual fields
      - Any time all fields have been reviewed (no EXTRACTED remaining)

    Rejected fields are skipped — they become no ObservationEvent.
    Already-resulted fields (RESULTED) are skipped to prevent duplicates.
    """
    try:
        report = LabReport.objects.select_for_update().select_related(
            "patient", "integration", "uploading_organisation"
        ).get(pk=report_id)
    except LabReport.DoesNotExist:
        raise LabReportNotFound()

    _assert_can_write(user, report.patient_id)

    if report.status == LabReportStatus.RESULTED:
        raise LabReportAlreadyResulted()

    if report.pending_fields > 0:
        raise LabReportNotReviewable(
            f"{report.pending_fields} field(s) are still pending review. "
            "Review all fields before resulting the report."
        )

    # Determine verification level based on source
    verification_map = {
        LabReportSource.PATIENT_UPLOAD:    "patient_confirmed",
        LabReportSource.MANUAL_ENTRY:      "patient_confirmed",
        LabReportSource.ORGANISATION_PUSH: "digitally_verified",
        LabReportSource.LAB_INTEGRATION:   "digitally_verified",
    }
    verification_level = verification_map.get(report.source, "patient_confirmed")

    # Create ObservationEvent for each confirmed/corrected field
    to_result = report.fields.filter(
        status__in=[FieldStatus.CONFIRMED, FieldStatus.CORRECTED]
    ).select_for_update()

    for field in to_result:
        obs_event = _create_observation_from_field(field, report, verification_level)
        field.resulting_event = obs_event
        field.status          = FieldStatus.RESULTED
        field.save(update_fields=["resulting_event", "status", "updated_at"])

    now               = timezone.now()
    report.status     = LabReportStatus.RESULTED
    report.resulted_at = now
    report.save(update_fields=["status", "resulted_at", "updated_at"])

    logger.info(
        "Lab report resulted. report_id=%s patient_id=%s fields_resulted=%s",
        report_id, report.patient_id, to_result.count(),
    )
    return report


# ===========================================================================
# OCR TRIGGER (called by background task when OCR completes)
# ===========================================================================

@transaction.atomic
def process_ocr_result(
    report_id: UUID,
    extracted_fields: list[dict],
    ocr_provider: str,
    ocr_confidence: float,
    ocr_raw_output: dict,
    error_message: str = None,
) -> LabReport:
    """
    Called by the background OCR task when extraction completes.

    Creates LabReportField records for each extracted value.
    Transitions report status to extracted or failed.
    Patient is then notified to review.

    This function is called by Celery/Django-Q, not directly by the API.
    """
    try:
        report = LabReport.objects.select_for_update().get(pk=report_id)
    except LabReport.DoesNotExist:
        raise LabReportNotFound()

    now = timezone.now()

    if error_message:
        report.status            = LabReportStatus.FAILED
        report.ocr_error_message = error_message
        report.save(update_fields=["status", "ocr_error_message", "updated_at"])
        logger.error("OCR failed for report %s: %s", report_id, error_message)
        return report

    # Store OCR metadata
    report.ocr_provider      = ocr_provider
    report.ocr_confidence    = ocr_confidence
    report.ocr_raw_output    = ocr_raw_output
    report.ocr_completed_at  = now

    # Create field records for each extracted value
    for i, field_data in enumerate(extracted_fields):
        LabReportField.objects.create(
            lab_report       = report,
            test_name        = field_data.get("test_name", "Unknown"),
            loinc_code       = field_data.get("loinc_code"),
            loinc_display    = field_data.get("loinc_display"),
            extracted_value  = str(field_data.get("value", "")),
            extracted_unit   = field_data.get("unit"),
            reference_range  = field_data.get("reference_range"),
            is_abnormal      = field_data.get("is_abnormal"),
            abnormal_flag    = field_data.get("abnormal_flag"),
            field_confidence = field_data.get("confidence"),
            status           = FieldStatus.EXTRACTED,
            display_order    = i,
        )

    report.status = LabReportStatus.PENDING_REVIEW
    report.save(update_fields=[
        "ocr_provider", "ocr_confidence", "ocr_raw_output",
        "ocr_completed_at", "status", "updated_at",
    ])

    logger.info(
        "OCR complete. report_id=%s fields=%s confidence=%.2f",
        report_id, len(extracted_fields), ocr_confidence or 0,
    )
    return report