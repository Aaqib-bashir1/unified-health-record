"""
lab_reports/api.py
==================
API layer for lab report ingestion — three paths, one unified interface.

Routers:
  patient_router     — patient-facing: upload, manual entry, review, confirm
  org_router         — organisation-facing: push results, integration webhook
  internal_router    — internal: OCR callback (not exposed to end users)

All patient endpoints require JWT auth.
Org endpoints require JWT auth + verified organisation membership.
Internal endpoints require a shared secret header (set via LAB_OCR_SECRET).

Interconnections:
  → medical_events/: upload creates DocumentEvent (source of truth)
                     result_report creates ObservationEvents per confirmed field
  → patients/:       patient access checked on every write
  → organisations/:  org push verifies the org is active and verified
  → clinical/:       pending test orders can be linked to results (future)
"""

import logging
from types import SimpleNamespace
from uuid import UUID

from django.conf import settings
from ninja import File, Router, UploadedFile

from core.auth import JWTBearer, get_current_user
from users.schemas import ErrorSchema

from . import services
from .exceptions import (
    ExtractionFailed,
    IntegrationNotActive,
    IntegrationNotFound,
    LabFieldAlreadyReviewed,
    LabFieldNotFound,
    LabReportAccessDenied,
    LabReportAlreadyResulted,
    LabReportError,
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
from .schemas import (
    BulkConfirmSchema,
    LabFieldResponse,
    LabIntegrationResponse,
    LabReportResponse,
    LabReportSummarySchema,
    ManualLabReportSchema,
    OCRResultSchema,
    ReceiveFromOrgSchema,
    ReviewFieldSchema,
    UploadLabReportSchema,
)

logger   = logging.getLogger(__name__)
jwt_auth = JWTBearer()

patient_router  = Router(tags=["Lab Reports — Patient"])
org_router      = Router(tags=["Lab Reports — Organisation"])
internal_router = Router(tags=["Lab Reports — Internal"])


# ===========================================================================
# RESPONSE BUILDERS
# ===========================================================================

def _build_field(f: LabReportField) -> dict:
    return {
        "id":                      str(f.id),
        "test_name":               f.test_name,
        "loinc_code":              f.loinc_code,
        "loinc_display":           f.loinc_display,
        "extracted_value":         f.extracted_value,
        "extracted_unit":          f.extracted_unit,
        "patient_corrected_value": f.patient_corrected_value,
        "patient_corrected_unit":  f.patient_corrected_unit,
        "effective_value":         f.confirmed_value,   # property
        "effective_unit":          f.confirmed_unit,    # property
        "reference_range":         f.reference_range,
        "is_abnormal":             f.is_abnormal,
        "abnormal_flag":           f.abnormal_flag,
        "field_confidence":        f.field_confidence,
        "status":                  f.status,
        "resulting_event_id":      str(f.resulting_event_id) if f.resulting_event_id else None,
        "display_order":           f.display_order,
    }


def _build_report(report: LabReport) -> dict:
    fields = list(report.fields.all().order_by("display_order", "test_name"))
    panel  = report.panel

    # Split fields into panelled and ungrouped
    ungrouped = [_build_field(f) for f in fields]

    panel_data = None
    if panel:
        panel_data = {
            "id":           str(panel.id),
            "name":         panel.name,
            "display_name": panel.display_name,
            "loinc_code":   panel.loinc_code,
            "fields":       ungrouped,
        }
        ungrouped = []

    org = report.uploading_organisation
    return {
        "id":                  str(report.id),
        "patient_id":          str(report.patient_id),
        "source":              report.source,
        "status":              report.status,
        "lab_name":            report.lab_name,
        "report_date":         str(report.report_date) if report.report_date else None,
        "report_id":           report.report_id,
        "integration_id":      str(report.integration_id) if report.integration_id else None,
        "uploading_org_id":    str(org.id) if org else None,
        "uploading_org_name":  org.name if org else None,
        "ocr_provider":        report.ocr_provider,
        "ocr_confidence":      report.ocr_confidence,
        "ocr_completed_at":    report.ocr_completed_at.isoformat() if report.ocr_completed_at else None,
        "ocr_error_message":   report.ocr_error_message,
        "confirmed_at":        report.confirmed_at.isoformat() if report.confirmed_at else None,
        "resulted_at":         report.resulted_at.isoformat() if report.resulted_at else None,
        "pending_fields":      report.pending_fields,
        "total_fields":        len(fields),
        "abnormal_field_count": sum(1 for f in fields if f.is_abnormal),
        "panel":               panel_data,
        "ungrouped_fields":    ungrouped,
        "notes":               report.notes,
        "created_at":          report.created_at.isoformat(),
    }


def _build_summary(report: LabReport) -> dict:
    fields = list(report.fields.all())
    return {
        "id":            str(report.id),
        "source":        report.source,
        "status":        report.status,
        "lab_name":      report.lab_name,
        "report_date":   str(report.report_date) if report.report_date else None,
        "total_fields":  len(fields),
        "pending_fields": sum(1 for f in fields if f.status == FieldStatus.EXTRACTED),
        "abnormal_count": sum(1 for f in fields if f.is_abnormal),
        "resulted_at":   report.resulted_at.isoformat() if report.resulted_at else None,
        "created_at":    report.created_at.isoformat(),
    }


# ===========================================================================
# PATIENT ROUTER — PATH 1: FILE UPLOAD
# ===========================================================================

@patient_router.post(
    "/patients/{patient_id}/lab-reports/upload/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema},
    summary="Upload a lab report PDF or image",
    description=(
        "Upload a lab report PDF or image as multipart/form-data. "
        "The original file is stored as a DocumentEvent (source of truth). "
        "A LabReport record is created with status=uploaded. "
        "OCR extraction runs asynchronously — the patient will be notified "
        "when fields are ready for review. "
        "Use the /review/ and /confirm/ endpoints after OCR completes."
    ),
)
def upload_lab_report(
    request,
    patient_id:  UUID,
    file:        UploadedFile = File(...),
    lab_name:    str = None,
    report_date: str = None,
    report_id:   str = None,
    notes:       str = None,
):
    user = get_current_user(request)
    try:
        from django.utils.dateparse import parse_date
        data = SimpleNamespace(
            lab_name    = lab_name,
            report_date = parse_date(report_date) if report_date else None,
            report_id   = report_id,
            notes       = notes,
        )
        report = services.upload_lab_report(
            user         = user,
            patient_id   = patient_id,
            file_bytes   = file.read(),
            filename     = file.name,
            content_type = file.content_type or "application/octet-stream",
            data         = data,
        )
        return 201, {
            **_build_summary(report),
            "message": (
                "Lab report uploaded. OCR extraction will run shortly. "
                "You will be notified when fields are ready to review."
            ),
        }
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except Exception as e:
        logger.error("Lab upload failed: %s", e)
        return 400, ErrorSchema(detail=str(e), status_code=400)


# ===========================================================================
# PATIENT ROUTER — PATH 2: MANUAL ENTRY
# ===========================================================================

@patient_router.post(
    "/patients/{patient_id}/lab-reports/manual/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema},
    summary="Enter lab values manually (no file upload)",
    description=(
        "Patient types lab values directly. "
        "Fields are created with status=confirmed immediately. "
        "result_report() is called automatically — "
        "ObservationEvents are created with verification_level=patient_confirmed."
    ),
)
def create_manual_report(
    request,
    patient_id: UUID,
    data:       ManualLabReportSchema,
):
    user = get_current_user(request)
    try:
        fields_data = [
            {
                "test_name":       f.test_name,
                "loinc_code":      f.loinc_code,
                "loinc_display":   f.loinc_display,
                "value":           f.value,
                "unit":            f.unit,
                "reference_range": f.reference_range,
                "is_abnormal":     f.is_abnormal,
                "abnormal_flag":   f.abnormal_flag,
                "display_order":   f.display_order,
            }
            for f in data.fields
        ]
        metadata = {
            "lab_name":    data.lab_name,
            "report_date": data.report_date,
            "report_id":   data.report_id,
            "notes":       data.notes,
        }

        # Reuse receive_from_organisation with manual entry flag
        from .services import _assert_can_write
        from .models import LabReportSource, LabReportStatus, LabReportField, FieldStatus
        from django.utils import timezone as tz

        access  = _assert_can_write(user, patient_id)
        patient = access.patient

        report = LabReport.objects.create(
            patient    = patient,
            created_by = user,
            source     = LabReportSource.MANUAL_ENTRY,
            lab_name   = data.lab_name,
            report_date = data.report_date,
            report_id  = data.report_id,
            status     = LabReportStatus.CONFIRMED,
            notes      = data.notes,
        )

        for i, fd in enumerate(fields_data):
            field = LabReportField.objects.create(
                lab_report      = report,
                test_name       = fd["test_name"],
                loinc_code      = fd.get("loinc_code"),
                loinc_display   = fd.get("loinc_display"),
                extracted_value = fd["value"],
                extracted_unit  = fd.get("unit"),
                reference_range = fd.get("reference_range"),
                is_abnormal     = fd.get("is_abnormal"),
                abnormal_flag   = fd.get("abnormal_flag"),
                status          = FieldStatus.CONFIRMED,
                display_order   = fd.get("display_order", i),
                reviewed_at     = tz.now(),
                reviewed_by     = user,
            )

        # Auto-result — create ObservationEvents immediately
        report = services.result_report(user, report.id)
        return 201, {
            **_build_report(report),
            "message": (
                f"{len(fields_data)} lab value(s) added to your medical timeline."
            ),
        }
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except LabReportError as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


# ===========================================================================
# PATIENT ROUTER — GET / LIST
# ===========================================================================

@patient_router.get(
    "/patients/{patient_id}/lab-reports/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema},
    summary="List patient's lab reports",
)
def list_lab_reports(request, patient_id: UUID, status: str = None):
    user = get_current_user(request)
    try:
        reports = services.list_patient_reports(user, patient_id, status=status)
        return 200, [_build_summary(r) for r in reports]
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@patient_router.get(
    "/patients/{patient_id}/lab-reports/{report_id}/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get a lab report with all fields",
)
def get_lab_report(request, patient_id: UUID, report_id: UUID):
    user = get_current_user(request)
    try:
        report = services.get_report(user, report_id)
        return 200, _build_report(report)
    except LabReportNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# PATIENT ROUTER — FIELD REVIEW
# ===========================================================================

@patient_router.post(
    "/patients/{patient_id}/lab-reports/{report_id}/fields/{field_id}/review/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema, 409: ErrorSchema},
    summary="Review a single extracted field",
    description=(
        "Patient reviews a single OCR-extracted field. "
        "Omit confirmed_value to accept the extracted value. "
        "Pass confirmed_value to correct an OCR error. "
        "Pass reject=true to exclude the field from the timeline entirely."
    ),
)
def review_field(
    request,
    patient_id: UUID,
    report_id:  UUID,
    field_id:   UUID,
    data:       ReviewFieldSchema,
):
    user = get_current_user(request)
    try:
        field = services.review_field(
            user             = user,
            field_id         = field_id,
            confirmed_value  = data.confirmed_value,
            confirmed_unit   = data.confirmed_unit,
            reject           = data.reject,
            rejection_reason = data.rejection_reason,
        )
        return 200, _build_field(field)
    except LabFieldNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except LabFieldAlreadyReviewed as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@patient_router.post(
    "/patients/{patient_id}/lab-reports/{report_id}/confirm-all/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Confirm all extracted fields at once",
    description=(
        "Accept all OCR-extracted values without reviewing field by field. "
        "Useful when the patient trusts the extraction. "
        "Calls result_report() automatically to create ObservationEvents."
    ),
)
def confirm_all_fields(request, patient_id: UUID, report_id: UUID):
    user = get_current_user(request)
    try:
        report = services.confirm_all_fields(user, report_id)
        # Auto-result after confirmation
        report = services.result_report(user, report_id)
        return 200, {
            **_build_report(report),
            "message": (
                "All fields confirmed. Lab values have been added to your medical timeline."
            ),
        }
    except LabReportNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except (LabReportAlreadyResulted, LabReportNotReviewable) as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@patient_router.post(
    "/patients/{patient_id}/lab-reports/{report_id}/result/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Result a report — create ObservationEvents from confirmed fields",
    description=(
        "Creates ObservationEvents in medical_events/ for all confirmed/corrected fields. "
        "Rejected fields are skipped. "
        "Requires all fields to be reviewed first (no EXTRACTED remaining). "
        "Use /confirm-all/ to skip per-field review."
    ),
)
def result_report(request, patient_id: UUID, report_id: UUID):
    user = get_current_user(request)
    try:
        report = services.result_report(user, report_id)
        return 200, {
            **_build_report(report),
            "message": "Lab values have been added to your medical timeline.",
        }
    except LabReportNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except LabReportAlreadyResulted as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except LabReportNotReviewable as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except LabReportAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


# ===========================================================================
# ORG ROUTER — PATH 2: MACHINE / ORGANISATION PUSH
# ===========================================================================

@org_router.post(
    "/organisations/{org_id}/lab-results/push/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Organisation pushes structured lab results for a patient",
    description=(
        "Used by verified organisations to push lab results directly. "
        "Requires verified organisation membership (org admin or practitioner). "
        "Results are auto-confirmed with verification_level=digitally_verified. "
        "ObservationEvents are created immediately — no patient review step. "
        "Patient is notified that new lab results have been received."
    ),
)
def receive_from_organisation(
    request,
    org_id: UUID,
    data:   ReceiveFromOrgSchema,
):
    user = get_current_user(request)
    try:
        from organisations.models import Organisation
        from organisations.exceptions import OrgNotFound, OrgNotVerified

        try:
            org = Organisation.objects.get(pk=org_id, is_active=True)
        except Organisation.DoesNotExist:
            raise OrgNotFound()

        if not org.verified:
            raise OrgNotVerified()

        # Verify the requesting user has org membership
        from practitioners.models import Practitioner, PractitionerRole
        try:
            prac = Practitioner.objects.get(user=user, is_verified=True)
            if not PractitionerRole.objects.filter(
                practitioner=prac, organisation=org, is_active=True
            ).exists():
                return 403, ErrorSchema(
                    detail="You are not a member of this organisation.",
                    status_code=403,
                )
        except Practitioner.DoesNotExist:
            return 403, ErrorSchema(
                detail="Verified practitioner profile required.",
                status_code=403,
            )

        # Resolve patient — by UHR UUID or by MRN
        patient_id = data.patient_id
        if not patient_id and data.patient_mrn:
            from patients.models import Patient
            try:
                patient = Patient.objects.get(
                    mrn=data.patient_mrn,
                    deleted_at__isnull=True,
                )
                patient_id = patient.id
            except Patient.DoesNotExist:
                return 404, ErrorSchema(
                    detail=f"Patient with MRN {data.patient_mrn} not found.",
                    status_code=404,
                )

        if not patient_id:
            return 400, ErrorSchema(
                detail="Either patient_id or patient_mrn is required.",
                status_code=400,
            )

        fields_data = [
            {
                "test_name":       f.test_name,
                "loinc_code":      f.loinc_code,
                "loinc_display":   f.loinc_display,
                "value":           f.value,
                "unit":            f.unit,
                "reference_range": f.reference_range,
                "is_abnormal":     f.is_abnormal,
                "abnormal_flag":   f.abnormal_flag,
                "display_order":   f.display_order,
            }
            for f in data.fields
        ]
        metadata = {
            "lab_name":    data.lab_name,
            "report_date": data.report_date,
            "report_id":   data.report_id,
            "notes":       data.notes,
        }

        report = services.receive_from_organisation(
            organisation    = org,
            patient_id      = patient_id,
            fields_data     = fields_data,
            metadata        = metadata,
            created_by_user = user,
        )
        return 201, {
            **_build_summary(report),
            "message": (
                f"{len(fields_data)} lab result(s) added to patient timeline "
                "with verification_level=digitally_verified."
            ),
        }
    except OrgNotFound as e:
        return 404, ErrorSchema(detail=str(e), status_code=404)
    except OrgNotVerified as e:
        return 403, ErrorSchema(detail=str(e), status_code=403)
    except LabReportError as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


# ===========================================================================
# ORG ROUTER — INTEGRATION WEBHOOK (PATH 3)
# ===========================================================================

@org_router.post(
    "/integrations/{integration_id}/webhook/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Lab integration webhook — receive results from external lab system",
    description=(
        "Called by external lab systems (Apollo, Thyrocare, LIMS) via HL7/FHIR/CSV. "
        "auto_import on the integration controls whether patient review is required. "
        "If auto_import=True → ObservationEvents created immediately (digitally_verified). "
        "If auto_import=False → LabReport goes to pending_review until patient confirms."
    ),
)
def receive_from_integration(
    request,
    integration_id: UUID,
    data:           ReceiveFromOrgSchema,
):
    user = get_current_user(request)
    try:
        patient_id = data.patient_id
        if not patient_id and data.patient_mrn:
            from patients.models import Patient
            try:
                p = Patient.objects.get(mrn=data.patient_mrn, deleted_at__isnull=True)
                patient_id = p.id
            except Patient.DoesNotExist:
                return 404, ErrorSchema(
                    detail=f"Patient with MRN {data.patient_mrn} not found.",
                    status_code=404,
                )

        if not patient_id:
            return 400, ErrorSchema(
                detail="Either patient_id or patient_mrn is required.",
                status_code=400,
            )

        fields_data = [
            {
                "test_name":       f.test_name,
                "loinc_code":      f.loinc_code,
                "loinc_display":   f.loinc_display,
                "value":           f.value,
                "unit":            f.unit,
                "reference_range": f.reference_range,
                "is_abnormal":     f.is_abnormal,
                "abnormal_flag":   f.abnormal_flag,
                "display_order":   f.display_order,
            }
            for f in data.fields
        ]
        metadata = {
            "lab_name":    data.lab_name,
            "report_date": data.report_date,
            "report_id":   data.report_id,
            "notes":       data.notes,
        }

        report = services.receive_from_integration(
            integration_id  = integration_id,
            patient_id      = patient_id,
            fields_data     = fields_data,
            metadata        = metadata,
            created_by_user = user,
        )
        return 201, {
            **_build_summary(report),
            "auto_imported": report.status == LabReportStatus.RESULTED,
        }
    except IntegrationNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except IntegrationNotActive as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)
    except LabReportError as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)


# ===========================================================================
# INTERNAL ROUTER — OCR CALLBACK
# ===========================================================================

def _verify_ocr_secret(request) -> bool:
    """Verify the shared OCR secret header."""
    expected = getattr(settings, "LAB_OCR_SECRET", None)
    if not expected:
        return False
    return request.headers.get("X-OCR-Secret") == expected


@internal_router.post(
    "/internal/lab-reports/{report_id}/ocr-result/",
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 404: ErrorSchema},
    summary="[INTERNAL] OCR service posts extraction results",
    description=(
        "Called by the background OCR task (Celery/Django-Q) when extraction completes. "
        "Requires X-OCR-Secret header matching LAB_OCR_SECRET setting. "
        "NOT exposed to end users."
    ),
)
def receive_ocr_result(request, report_id: UUID, data: OCRResultSchema):
    if not _verify_ocr_secret(request):
        return 401, ErrorSchema(detail="Invalid OCR secret.", status_code=401)

    try:
        extracted_fields = [
            {
                "test_name":       f.test_name,
                "loinc_code":      f.loinc_code,
                "loinc_display":   f.loinc_display,
                "value":           f.value,
                "unit":            f.unit,
                "reference_range": f.reference_range,
                "is_abnormal":     f.is_abnormal,
                "abnormal_flag":   f.abnormal_flag,
                "confidence":      f.confidence,
                "display_order":   f.display_order,
            }
            for f in data.fields
        ]

        report = services.process_ocr_result(
            report_id        = report_id,
            extracted_fields = extracted_fields,
            ocr_provider     = data.ocr_provider,
            ocr_confidence   = data.ocr_confidence,
            ocr_raw_output   = data.ocr_raw_output or {},
            error_message    = data.error_message,
        )
        return 200, {
            "report_id":    str(report.id),
            "status":       report.status,
            "field_count":  report.fields.count(),
            "confidence":   report.ocr_confidence,
        }
    except LabReportNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except ExtractionFailed as e:
        return 400, ErrorSchema(detail=e.message, status_code=400)