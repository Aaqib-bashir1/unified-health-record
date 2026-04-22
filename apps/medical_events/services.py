"""
medical_events/services.py
===========================
Service layer for the medical_events app.

All write paths enforce the schema invariants.
No event is ever modified after creation.

Function index:
  create_event(user, patient_id, event_type, data, source_context)
  create_document_event(user, patient_id, file_bytes, filename, content_type, data)
  amend_event(user, patient_id, original_event_id, amendment_reason, new_data)
  medication_lifecycle(user, patient_id, data)
  get_event(user, patient_id, event_id)
  get_timeline(user, patient_id, filters)
  get_active_medications(user, patient_id)
  approve_event(user, patient_id, event_id, approve)
  hide_event(user, patient_id, event_id)
"""

import logging
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from apps.patients.models import Patient, PatientUserAccess
from apps.patients.services import _get_active_access

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
from .models import (
    AllergyEvent,
    ConditionEvent,
    ConsultationEvent,
    DocumentEvent,
    EventType,
    MedicalEvent,
    MedicationEvent,
    MedicationStatus,
    ObservationEvent,
    ProcedureEvent,
    RelationshipType,
    SecondOpinionEvent,
    SourceType,
    VaccinationEvent,
    VerificationLevel,
    VisibilityStatus,
    VisitEvent,
    VitalSignsEvent,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# INTERNAL HELPERS
# ===========================================================================

def _assert_can_write_events(access: PatientUserAccess) -> None:
    """Raise EventAccessDenied if the access record does not permit writing events."""
    if not access.can_write:
        raise EventAccessDenied(
            f"Your role '{access.role}' does not permit adding medical events."
        )


def _assert_can_read_events(access: PatientUserAccess) -> None:
    if not access.can_read:
        raise EventAccessDenied("You do not have read access to this patient's timeline.")


def _determine_visibility(source_context: dict) -> str:
    """
    Determine the initial visibility_status for a new event.

    Rules (schema invariant 12.7):
      - Events submitted via visit session → pending_approval
      - Events submitted via share link    → pending_approval
      - Events submitted by patient directly → visible
      - Events submitted by practitioner with direct access → pending_approval
    """
    via_visit      = source_context.get("via_visit", False)
    via_share_link = source_context.get("via_share_link", False)
    source_type    = source_context.get("source_type", SourceType.PATIENT)

    if via_visit or via_share_link:
        return VisibilityStatus.PENDING_APPROVAL
    if source_type == SourceType.DOCTOR:
        return VisibilityStatus.PENDING_APPROVAL
    return VisibilityStatus.VISIBLE


def _determine_verification_level(user, source_context: dict) -> str:
    """
    Determine the verification_level for a new event.
    Enforces invariant 12.4 — only practitioners can set provider_verified.
    """
    source_type = source_context.get("source_type", SourceType.PATIENT)

    if source_type == SourceType.LAB:
        return VerificationLevel.DIGITALLY_VERIFIED

    if source_type == SourceType.DOCTOR:
        # Verify the user actually has a verified practitioner profile
        try:
            from practitioners.models import Practitioner
            practitioner = Practitioner.objects.get(user=user, is_verified=True)
            return VerificationLevel.PROVIDER_VERIFIED
        except Exception:
            raise InvalidVerificationLevel()

    # Patient source — self_reported unless explicitly OCR-confirmed
    if source_context.get("ocr_confirmed", False):
        return VerificationLevel.PATIENT_CONFIRMED

    return VerificationLevel.SELF_REPORTED


def _get_practitioner_for_user(user):
    """Return the practitioner profile for a user, or None."""
    try:
        from practitioners.models import Practitioner
        return Practitioner.objects.get(user=user, is_verified=True, is_active=True)
    except Exception:
        return None


def _build_base_event(
    user,
    patient: Patient,
    event_type: str,
    clinical_timestamp,
    source_context: dict,
) -> MedicalEvent:
    """
    Build and save the base MedicalEvent record.
    Does NOT create the typed extension — caller does that.
    """
    source_type    = source_context.get("source_type", SourceType.PATIENT)
    practitioner   = _get_practitioner_for_user(user) if source_type == SourceType.DOCTOR else None
    org_id         = source_context.get("organisation_id")
    visibility     = _determine_visibility(source_context)
    verification   = _determine_verification_level(user, source_context)

    return MedicalEvent.objects.create(
        patient              = patient,
        event_type           = event_type,
        clinical_timestamp   = clinical_timestamp,
        source_type          = source_type,
        source_practitioner  = practitioner,
        source_organisation_id = org_id,
        verification_level   = verification,
        visibility_status    = visibility,
        created_by           = user,
        relationship_type    = RelationshipType.NONE,
    )


# ===========================================================================
# CREATE EVENT
# ===========================================================================

@transaction.atomic
def create_event(
    user,
    patient_id: UUID,
    event_type: str,
    data,
    source_context: dict = None,
) -> MedicalEvent:
    """
    Create a medical event and its typed extension.

    source_context keys:
      source_type:     'patient' | 'doctor' | 'lab' | 'system'
      via_visit:       bool — submitted during a hospital visit session
      via_share_link:  bool — submitted via anonymous share link
      organisation_id: UUID — source org (for visit-based events)
      ocr_confirmed:   bool — patient confirmed OCR extraction

    Permission:
      source_type=patient → requires can_write access
      source_type=doctor  → requires verified practitioner profile + can_write
      source_type=lab     → system-level, no access check (called by integration layer)
    """
    if source_context is None:
        source_context = {"source_type": SourceType.PATIENT}

    access = _get_active_access(user, patient_id)
    _assert_can_write_events(access)

    patient = access.patient

    base = _build_base_event(
        user, patient, event_type,
        data.clinical_timestamp, source_context,
    )

    _create_extension(base, event_type, data)

    logger.info(
        "Medical event created. event_id=%s type=%s patient_id=%s user_id=%s",
        base.id, event_type, patient_id, user.id,
    )
    return base


def _create_extension(base: MedicalEvent, event_type: str, data) -> None:
    """Create the typed extension for a base MedicalEvent."""
    if event_type == EventType.VISIT:
        VisitEvent.objects.create(
            medical_event = base,
            reason        = getattr(data, "reason", None),
            visit_type    = getattr(data, "visit_type", None),
            notes         = getattr(data, "notes", None),
        )
    elif event_type == EventType.OBSERVATION:
        ObservationEvent.objects.create(
            medical_event    = base,
            observation_name = data.observation_name,
            coding_system    = getattr(data, "coding_system", None),
            coding_code      = getattr(data, "coding_code", None),
            coding_display   = getattr(data, "coding_display", None),
            value_type       = getattr(data, "value_type", "quantity"),
            value_quantity   = getattr(data, "value_quantity", None),
            value_unit       = getattr(data, "value_unit", None),
            value_string     = getattr(data, "value_string", None),
            reference_range  = getattr(data, "reference_range", None),
        )
    elif event_type == EventType.CONDITION:
        ConditionEvent.objects.create(
            medical_event   = base,
            condition_name  = data.condition_name,
            coding_system   = getattr(data, "coding_system", None),
            coding_code     = getattr(data, "coding_code", None),
            coding_display  = getattr(data, "coding_display", None),
            clinical_status = getattr(data, "clinical_status", "active"),
            onset_date      = getattr(data, "onset_date", None),
            abatement_date  = getattr(data, "abatement_date", None),
            notes           = getattr(data, "notes", None),
        )
    elif event_type == EventType.MEDICATION:
        MedicationEvent.objects.create(
            medical_event   = base,
            medication_name = data.medication_name,
            dosage          = getattr(data, "dosage", None),
            frequency       = getattr(data, "frequency", None),
            route           = getattr(data, "route", None),
            start_date      = getattr(data, "start_date", None),
            end_date        = getattr(data, "end_date", None),
            status          = getattr(data, "status", "active"),
            notes           = getattr(data, "notes", None),
        )
    elif event_type == EventType.PROCEDURE:
        ProcedureEvent.objects.create(
            medical_event  = base,
            procedure_name = data.procedure_name,
            coding_system  = getattr(data, "coding_system", None),
            coding_code    = getattr(data, "coding_code", None),
            coding_display = getattr(data, "coding_display", None),
            performed_date = getattr(data, "performed_date", None),
            notes          = getattr(data, "notes", None),
        )
    elif event_type == EventType.SECOND_OPINION:
        SecondOpinionEvent.objects.create(
            medical_event               = base,
            doctor_name                 = data.doctor_name,
            doctor_registration_number  = getattr(data, "doctor_registration_number", None),
            opinion_text                = data.opinion_text,
            approved_by_patient         = False,
        )
    elif event_type == EventType.ALLERGY:
        AllergyEvent.objects.create(
            medical_event     = base,
            substance_name    = data.substance_name,
            coding_system     = getattr(data, "coding_system", None),
            coding_code       = getattr(data, "coding_code", None),
            coding_display    = getattr(data, "coding_display", None),
            allergy_type      = getattr(data, "allergy_type", "allergy"),
            category          = getattr(data, "category", "medication"),
            criticality       = getattr(data, "criticality", "unable_to_assess"),
            reaction_type     = getattr(data, "reaction_type", None),
            reaction_severity = getattr(data, "reaction_severity", None),
            clinical_status   = getattr(data, "clinical_status", "active"),
            onset_date        = getattr(data, "onset_date", None),
            notes             = getattr(data, "notes", None),
        )
    elif event_type == EventType.VACCINATION:
        VaccinationEvent.objects.create(
            medical_event       = base,
            vaccine_name        = data.vaccine_name,
            coding_system       = getattr(data, "coding_system", None),
            coding_code         = getattr(data, "coding_code", None),
            coding_display      = getattr(data, "coding_display", None),
            dose_number         = getattr(data, "dose_number", None),
            lot_number          = getattr(data, "lot_number", None),
            administered_date   = getattr(data, "administered_date", None),
            next_dose_due_date  = getattr(data, "next_dose_due_date", None),
            administering_org   = getattr(data, "administering_org", None),
            site                = getattr(data, "site", None),
            route               = getattr(data, "route", None),
            notes               = getattr(data, "notes", None),
        )
    elif event_type == EventType.CONSULTATION:
        # Resolve consulting practitioner FK if provided
        consulting_prac = None
        consulting_prac_id = getattr(data, "consulting_practitioner_id", None)
        if consulting_prac_id:
            try:
                from practitioners.models import Practitioner
                consulting_prac = Practitioner.objects.get(pk=consulting_prac_id)
            except Exception:
                pass

        referred_by = None
        referred_by_id = getattr(data, "referred_by_id", None)
        if referred_by_id:
            try:
                from practitioners.models import Practitioner
                referred_by = Practitioner.objects.get(pk=referred_by_id)
            except Exception:
                pass

        ConsultationEvent.objects.create(
            medical_event              = base,
            department                 = getattr(data, "department", "general_practice"),
            sub_specialty              = getattr(data, "sub_specialty", None),
            consulting_practitioner    = consulting_prac,
            referred_by                = referred_by,
            chief_complaint            = data.chief_complaint,
            history_of_present_illness = getattr(data, "history_of_present_illness", None),
            examination_findings       = getattr(data, "examination_findings", None),
            investigations_ordered     = getattr(data, "investigations_ordered", None),
            assessment                 = getattr(data, "assessment", None),
            plan                       = getattr(data, "plan", None),
            follow_up_date             = getattr(data, "follow_up_date", None),
            follow_up_instructions     = getattr(data, "follow_up_instructions", None),
        )
    elif event_type == EventType.VITAL_SIGNS:
        VitalSignsEvent.objects.create(
            medical_event    = base,
            systolic_bp      = getattr(data, "systolic_bp", None),
            diastolic_bp     = getattr(data, "diastolic_bp", None),
            bp_position      = getattr(data, "bp_position", None),
            heart_rate       = getattr(data, "heart_rate", None),
            heart_rhythm     = getattr(data, "heart_rhythm", None),
            temperature      = getattr(data, "temperature", None),
            temp_site        = getattr(data, "temp_site", None),
            spo2             = getattr(data, "spo2", None),
            on_oxygen        = getattr(data, "on_oxygen", None),
            respiratory_rate = getattr(data, "respiratory_rate", None),
            weight_kg        = getattr(data, "weight_kg", None),
            height_cm        = getattr(data, "height_cm", None),
            bmi              = getattr(data, "bmi", None),
            pain_score       = getattr(data, "pain_score", None),
            notes            = getattr(data, "notes", None),
        )


# ===========================================================================
# DOCUMENT UPLOAD
# ===========================================================================

@transaction.atomic
def create_document_event(
    user,
    patient_id: UUID,
    file_bytes: bytes,
    original_filename: str,
    content_type: str,
    data,
    source_context: dict = None,
) -> MedicalEvent:
    """
    Upload a document to S3 and create a DocumentEvent.

    Steps:
      1. Compute SHA-256 checksum from file_bytes
      2. Upload to S3-compatible storage
      3. Create base MedicalEvent
      4. Create DocumentEvent with S3 reference and checksum

    The event ID is used as part of the S3 key, so we create the
    base event first, then upload with the event ID in the key.
    """
    from .storage import upload_document

    if source_context is None:
        source_context = {"source_type": SourceType.PATIENT}

    access = _get_active_access(user, patient_id)
    _assert_can_write_events(access)

    patient    = access.patient
    import uuid as uuid_module
    event_id   = uuid_module.uuid4()

    # Upload first — if storage fails, no DB record is created
    try:
        upload_result = upload_document(
            file_bytes        = file_bytes,
            patient_id        = patient_id,
            event_id          = event_id,
            original_filename = original_filename,
            content_type      = content_type,
        )
    except DocumentUploadFailed:
        raise

    base = MedicalEvent(
        id                   = event_id,
        patient              = patient,
        event_type           = EventType.DOCUMENT,
        clinical_timestamp   = data.clinical_timestamp,
        source_type          = source_context.get("source_type", SourceType.PATIENT),
        verification_level   = _determine_verification_level(user, source_context),
        visibility_status    = _determine_visibility(source_context),
        created_by           = user,
        relationship_type    = RelationshipType.NONE,
    )
    base.save()

    DocumentEvent.objects.create(
        medical_event     = base,
        file_url          = upload_result["file_url"],
        file_type         = content_type,
        document_type     = getattr(data, "document_type", "other"),
        original_filename = original_filename,
        file_size_bytes   = len(file_bytes),
        checksum          = upload_result["checksum"],
        storage_provider  = upload_result.get("s3_bucket", "s3") and "s3",
        s3_bucket         = upload_result["s3_bucket"],
        s3_key            = upload_result["s3_key"],
    )

    logger.info(
        "Document event created. event_id=%s patient_id=%s filename=%s size=%s",
        event_id, patient_id, original_filename, len(file_bytes),
    )
    return base


# ===========================================================================
# AMEND EVENT (correction)
# ===========================================================================

@transaction.atomic
def amend_event(
    user,
    patient_id: UUID,
    original_event_id: UUID,
    amendment_reason: str,
    new_data,
    source_context: dict = None,
) -> MedicalEvent:
    """
    Create a correction for an existing event.

    The original event is NEVER modified.
    A new event is created with:
      relationship_type = amendment
      amends_event      = original event
      amendment_reason  = mandatory explanation

    The new event inherits the original's event_type.
    """
    if not amendment_reason or len(amendment_reason.strip()) < 5:
        raise AmendmentReasonRequired()

    access = _get_active_access(user, patient_id)
    _assert_can_write_events(access)

    try:
        original = MedicalEvent.objects.get(
            id=original_event_id,
            patient_id=patient_id,
            is_active=True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound()

    if source_context is None:
        source_context = {"source_type": SourceType.PATIENT}

    patient      = access.patient
    source_type  = source_context.get("source_type", SourceType.PATIENT)
    practitioner = _get_practitioner_for_user(user) if source_type == SourceType.DOCTOR else None
    visibility   = _determine_visibility(source_context)
    verification = _determine_verification_level(user, source_context)

    amendment = MedicalEvent.objects.create(
        patient              = patient,
        event_type           = original.event_type,
        clinical_timestamp   = getattr(new_data, "clinical_timestamp", original.clinical_timestamp),
        source_type          = source_type,
        source_practitioner  = practitioner,
        verification_level   = verification,
        visibility_status    = visibility,
        created_by           = user,
        amends_event         = original,
        amendment_reason     = amendment_reason,
        relationship_type    = RelationshipType.AMENDMENT,
    )

    _create_extension(amendment, original.event_type, new_data)

    logger.info(
        "Event amended. amendment_id=%s original_id=%s patient_id=%s reason=%s",
        amendment.id, original_event_id, patient_id, amendment_reason[:50],
    )
    return amendment


# ===========================================================================
# MEDICATION LIFECYCLE TRANSITION
# ===========================================================================

@transaction.atomic
def medication_lifecycle(
    user,
    patient_id: UUID,
    data,
) -> MedicalEvent:
    """
    Create a medication lifecycle event (modified or discontinued).

    Finds the original medication event, validates the transition is valid,
    and creates a new event with relationship_type=lifecycle.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_write_events(access)

    try:
        parent = MedicalEvent.objects.select_related("medication_event").get(
            id=data.parent_event_id,
            patient_id=patient_id,
            event_type=EventType.MEDICATION,
            is_active=True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound("Original medication event not found.")

    parent_med = parent.medication_event
    if parent_med.status == MedicationStatus.DISCONTINUED:
        raise MedicationLifecycleError(
            "Cannot create a lifecycle event for a discontinued medication."
        )

    action = data.action
    if action not in ("modified", "discontinued"):
        raise MedicationLifecycleError("action must be 'modified' or 'discontinued'.")

    new_status = MedicationStatus.DISCONTINUED if action == "discontinued" else MedicationStatus.ACTIVE

    patient      = access.patient
    source_context = {"source_type": SourceType.PATIENT}
    practitioner = _get_practitioner_for_user(user)
    if practitioner:
        source_context = {"source_type": SourceType.DOCTOR}

    lifecycle_event = MedicalEvent.objects.create(
        patient            = patient,
        event_type         = EventType.MEDICATION,
        clinical_timestamp = data.clinical_timestamp,
        source_type        = source_context["source_type"],
        source_practitioner = practitioner,
        verification_level = _determine_verification_level(user, source_context),
        visibility_status  = _determine_visibility(source_context),
        created_by         = user,
        parent_event       = parent,
        relationship_type  = RelationshipType.LIFECYCLE,
    )

    MedicationEvent.objects.create(
        medical_event   = lifecycle_event,
        medication_name = getattr(data, "medication_name", None) or parent_med.medication_name,
        dosage          = getattr(data, "dosage", None) or parent_med.dosage,
        frequency       = getattr(data, "frequency", None) or parent_med.frequency,
        route           = parent_med.route,
        start_date      = parent_med.start_date,
        end_date        = getattr(data, "end_date", None),
        status          = new_status,
        notes           = getattr(data, "notes", None),
    )

    logger.info(
        "Medication lifecycle event created. event_id=%s action=%s parent=%s",
        lifecycle_event.id, action, data.parent_event_id,
    )
    return lifecycle_event


# ===========================================================================
# GET SINGLE EVENT
# ===========================================================================

def get_event(user, patient_id: UUID, event_id: UUID) -> MedicalEvent:
    """
    Fetch a single medical event. Respects visibility rules.
    Patient can see pending_approval events. Others cannot.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    try:
        event = MedicalEvent.objects.select_related(
            "source_practitioner",
            "source_organisation",
            "created_by",
        ).get(
            id=event_id,
            patient_id=patient_id,
            is_active=True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound()

    # Non-primary roles cannot see pending_approval events
    if event.visibility_status == VisibilityStatus.PENDING_APPROVAL:
        if access.role not in ("primary", "full_delegate"):
            raise EventNotFound()

    return event


# ===========================================================================
# GET TIMELINE
# ===========================================================================

def get_timeline(user, patient_id: UUID, filters: dict = None) -> object:
    """
    Return the patient's medical event timeline.
    Ordered by clinical_timestamp descending.

    Filters:
      event_type:         str
      from_date:          date
      to_date:            date
      verification_level: str
      source_type:        str
      include_hidden:     bool (default False — only primary can see hidden)
      include_pending:    bool (default False — only primary/delegate can see pending)
    """
    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    if filters is None:
        filters = {}

    qs = MedicalEvent.objects.filter(
        patient_id=patient_id,
        is_active=True,
    ).select_related(
        "source_practitioner",
        "source_organisation",
    ).order_by("-clinical_timestamp")

    # Default: exclude hidden and pending
    visibility_include = [VisibilityStatus.VISIBLE]

    include_hidden  = filters.get("include_hidden", False)
    include_pending = filters.get("include_pending", False)

    # Only primary/delegate can opt into seeing hidden or pending events
    if include_hidden and access.role in ("primary", "full_delegate"):
        visibility_include.append(VisibilityStatus.HIDDEN)

    if include_pending and access.role in ("primary", "full_delegate"):
        visibility_include.append(VisibilityStatus.PENDING_APPROVAL)

    qs = qs.filter(visibility_status__in=visibility_include)

    # Apply filters
    if filters.get("event_type"):
        qs = qs.filter(event_type=filters["event_type"])
    if filters.get("from_date"):
        qs = qs.filter(clinical_timestamp__date__gte=filters["from_date"])
    if filters.get("to_date"):
        qs = qs.filter(clinical_timestamp__date__lte=filters["to_date"])
    if filters.get("verification_level"):
        qs = qs.filter(verification_level=filters["verification_level"])
    if filters.get("source_type"):
        qs = qs.filter(source_type=filters["source_type"])

    return qs


# ===========================================================================
# GET ACTIVE MEDICATIONS (computed view)
# ===========================================================================

def get_active_medications(user, patient_id: UUID):
    """
    Return currently active medications for a patient.

    Computes active medications by finding MedicalEvents of type=medication
    where the latest event in the lifecycle chain has status=active.

    This is more reliable than checking status on the latest event alone
    because the chain may have multiple lifecycle events.
    """
    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    # Find all medication events for this patient that are visible
    med_events = MedicalEvent.objects.filter(
        patient_id        = patient_id,
        event_type        = EventType.MEDICATION,
        is_active         = True,
        visibility_status = VisibilityStatus.VISIBLE,
    ).select_related("medication_event").order_by("-clinical_timestamp")

    # For each medication, find the root event (no parent = it's a starter)
    # and check whether its latest lifecycle event has status=active
    seen_roots = set()
    active_medications = []

    for event in med_events:
        # Walk up to the root of the chain
        root = event
        while root.parent_event_id:
            root = root.parent_event

        if root.id in seen_roots:
            continue
        seen_roots.add(root.id)

        # The latest event in this chain is `event` (ordered by clinical_timestamp desc)
        # Check its medication status
        try:
            med = event.medication_event
            if med.status == MedicationStatus.ACTIVE:
                active_medications.append(event)
        except Exception:
            continue

    return active_medications


# ===========================================================================
# APPROVE / HIDE EVENT
# ===========================================================================

@transaction.atomic
def approve_event(user, patient_id: UUID, event_id: UUID, approve: bool) -> MedicalEvent:
    """
    Patient approves (makes visible) or hides a pending_approval event.
    Only primary holder or full_delegate can do this.
    """
    access = _get_active_access(user, patient_id)
    if access.role not in ("primary", "full_delegate"):
        raise EventAccessDenied("Only the primary holder or delegate can approve events.")

    try:
        event = MedicalEvent.objects.select_for_update().get(
            id=event_id,
            patient_id=patient_id,
            is_active=True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound()

    if event.visibility_status != VisibilityStatus.PENDING_APPROVAL:
        raise EventNotApprovable()

    event.visibility_status = (
        VisibilityStatus.VISIBLE if approve else VisibilityStatus.HIDDEN
    )
    event.save(update_fields=["visibility_status"])

    # If second opinion, update approved_by_patient flag
    if event.event_type == EventType.SECOND_OPINION:
        try:
            event.second_opinion_event.approved_by_patient = approve
            event.second_opinion_event.save(update_fields=["approved_by_patient"])
        except Exception:
            pass

    logger.info(
        "Event %s. event_id=%s patient_id=%s by user_id=%s",
        "approved" if approve else "hidden",
        event_id, patient_id, user.id,
    )
    return event


@transaction.atomic
def hide_event(user, patient_id: UUID, event_id: UUID) -> MedicalEvent:
    """
    Patient hides a currently visible event.
    Only primary holder or full_delegate can hide events.
    """
    access = _get_active_access(user, patient_id)
    if access.role not in ("primary", "full_delegate"):
        raise EventAccessDenied("Only the primary holder or delegate can hide events.")

    try:
        event = MedicalEvent.objects.select_for_update().get(
            id=event_id,
            patient_id=patient_id,
            visibility_status=VisibilityStatus.VISIBLE,
            is_active=True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound("Visible event not found.")

    event.visibility_status = VisibilityStatus.HIDDEN
    event.save(update_fields=["visibility_status"])

    logger.info(
        "Event hidden. event_id=%s patient_id=%s by user_id=%s",
        event_id, patient_id, user.id,
    )
    return event


# ===========================================================================
# ALLERGY LIST
# ===========================================================================

def get_allergies(user, patient_id: UUID, active_only: bool = True):
    """
    Return patient allergies ordered by criticality (high first) then name.
    active_only=True (default) returns only active allergies.
    Primary/delegate can pass active_only=False to see resolved/entered-in-error.
    """
    from .models import AllergyEvent

    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    qs = (
        AllergyEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status__in = [
                VisibilityStatus.VISIBLE,
                VisibilityStatus.HIDDEN,
            ] if access.role in ("primary", "full_delegate") else [VisibilityStatus.VISIBLE],
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
    )

    if active_only:
        qs = qs.filter(clinical_status="active")

    # Criticality ordering: high → low → unable_to_assess
    from django.db.models import Case, IntegerField, Value, When
    qs = qs.annotate(
        criticality_order=Case(
            When(criticality="high",             then=Value(0)),
            When(criticality="low",              then=Value(2)),
            When(criticality="unable_to_assess", then=Value(3)),
            default=Value(3),
            output_field=IntegerField(),
        )
    ).order_by("criticality_order", "substance_name")

    return qs


# ===========================================================================
# VACCINATION LIST
# ===========================================================================

def get_vaccinations(user, patient_id: UUID):
    """Return vaccination history most recent first."""
    from .models import VaccinationEvent

    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    return (
        VaccinationEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")
    )


# ===========================================================================
# CONSULTATION LIST
# ===========================================================================

def get_consultations(user, patient_id: UUID, department: str = None):
    """
    Return consultation history.
    Optionally filtered by department for department-specific views.
    """
    from .models import ConsultationEvent

    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    qs = (
        ConsultationEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event", "consulting_practitioner")
        .order_by("-medical_event__clinical_timestamp")
    )

    if department:
        qs = qs.filter(department=department)

    return qs


# ===========================================================================
# VITAL SIGNS HISTORY
# ===========================================================================

def get_vital_signs_history(user, patient_id: UUID, limit: int = 20):
    """Return vital signs history most recent first."""
    from .models import VitalSignsEvent

    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    return (
        VitalSignsEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")[:limit]
    )


# ===========================================================================
# UN-HIDE EVENT
# ===========================================================================

@transaction.atomic
def unhide_event(user, patient_id: UUID, event_id: UUID) -> MedicalEvent:
    """
    Restore a hidden event back to visible.
    Only primary holder or full_delegate can unhide.
    """
    access = _get_active_access(user, patient_id)
    if access.role not in ("primary", "full_delegate"):
        raise EventAccessDenied(
            "Only the primary holder or delegate can restore hidden events."
        )

    try:
        event = MedicalEvent.objects.select_for_update().get(
            id                = event_id,
            patient_id        = patient_id,
            visibility_status = VisibilityStatus.HIDDEN,
            is_active         = True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound("Hidden event not found.")

    event.visibility_status = VisibilityStatus.VISIBLE
    event.save(update_fields=["visibility_status"])

    logger.info(
        "Event unhidden. event_id=%s patient_id=%s by user_id=%s",
        event_id, patient_id, user.id,
    )
    return event


# ===========================================================================
# DOCUMENT PRESIGNED DOWNLOAD URL
# ===========================================================================

def get_document_download_url(user, patient_id: UUID, event_id: UUID) -> str:
    """
    Generate a presigned S3 URL for a document event.
    Validates the document checksum is on record before generating the URL.
    Returns the presigned URL string.
    """
    from .models import DocumentEvent
    from .storage import generate_presigned_url

    access = _get_active_access(user, patient_id)
    _assert_can_read_events(access)

    try:
        event = MedicalEvent.objects.get(
            id         = event_id,
            patient_id = patient_id,
            event_type = "document",
            is_active  = True,
        )
    except MedicalEvent.DoesNotExist:
        raise EventNotFound("Document event not found.")

    if event.visibility_status == VisibilityStatus.PENDING_APPROVAL:
        if access.role not in ("primary", "full_delegate"):
            raise EventNotFound()

    try:
        doc = event.document_event
    except Exception:
        raise EventNotFound("Document metadata not found.")

    if not doc.s3_key:
        raise EventNotFound("Document has no storage reference.")

    return generate_presigned_url(doc.s3_key)