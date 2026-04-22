"""
medical_events/stats.py
=======================
Computed health statistics and categorized views for the doctor dashboard.

This module contains read-only computed views derived from MedicalEvent data.
Nothing here creates or modifies events. All outputs are computed fresh from
the event history — no denormalized stat fields on the Patient model.

Categories provided:
  - Health stats panel  (BMI, BP, glucose, cholesterol, HbA1c etc.)
  - Data freshness      (how recent each metric is)
  - Medication history  (all meds, active + historical)
  - Imaging & radiology (document events tagged as imaging)
  - Conditions summary  (active + resolved diagnoses)
  - Procedure history
  - Visit history
  - Second opinions     (approved only for doctors)
  - Doctor dashboard    (single call returning all categories)
"""

import logging
from datetime import date, timedelta
from typing import Optional
from uuid import UUID

from django.db.models import Max, OuterRef, Prefetch, Q, Subquery
from django.utils import timezone

logger = logging.getLogger(__name__)


# ===========================================================================
# LOINC CODES FOR COMMON HEALTH METRICS
# Used to identify specific observations by coding_code
# Falls back to observation_name icontains if no coding_code present
# ===========================================================================

LOINC_CODES = {
    "glucose_fasting":    "1558-6",
    "glucose_random":     "2345-7",
    "hba1c":              "4548-4",
    "cholesterol_total":  "2093-3",
    "cholesterol_ldl":    "2089-1",
    "cholesterol_hdl":    "2085-9",
    "triglycerides":      "2571-8",
    "systolic_bp":        "8480-6",
    "diastolic_bp":       "8462-4",
    "heart_rate":         "8867-4",
    "weight_kg":          "29463-7",
    "height_cm":          "8302-2",
    "bmi":                "39156-5",
    "spo2":               "59408-5",
    "temperature":        "8310-5",
    "creatinine":         "2160-0",
    "hemoglobin":         "718-7",
    "wbc":                "6690-2",
    "platelets":          "777-3",
}

# Human-readable labels for each metric
METRIC_LABELS = {
    "glucose_fasting":   "Fasting Blood Glucose",
    "glucose_random":    "Random Blood Glucose",
    "hba1c":             "HbA1c",
    "cholesterol_total": "Total Cholesterol",
    "cholesterol_ldl":   "LDL Cholesterol",
    "cholesterol_hdl":   "HDL Cholesterol",
    "triglycerides":     "Triglycerides",
    "systolic_bp":       "Systolic Blood Pressure",
    "diastolic_bp":      "Diastolic Blood Pressure",
    "heart_rate":        "Heart Rate",
    "weight_kg":         "Weight",
    "height_cm":         "Height",
    "bmi":               "BMI",
    "spo2":              "Oxygen Saturation (SpO2)",
    "temperature":       "Body Temperature",
    "creatinine":        "Creatinine",
    "hemoglobin":        "Hemoglobin",
    "wbc":               "WBC Count",
    "platelets":         "Platelet Count",
}


def _freshness_label(recorded_date: date) -> str:
    """
    Data freshness label per spec section 8:
      Recent:           ≤ 3 months
      Moderately Old:   3–12 months
      Old:              > 12 months
    """
    today = date.today()
    delta = (today - recorded_date).days

    if delta <= 90:
        return "recent"
    if delta <= 365:
        return "moderately_old"
    return "old"


# ===========================================================================
# HEALTH STATS PANEL
# ===========================================================================

def get_health_stats(patient_id: UUID, include_historical: bool = False) -> dict:
    """
    Return the most recent value for each standard health metric.

    For each metric:
      - Looks up by LOINC coding_code first
      - Falls back to observation_name icontains match
      - Returns the most recent visible event only

    Returns:
      {
        "bmi": {
          "value": 24.5,
          "unit": "kg/m2",
          "recorded_date": "2024-06-01",
          "freshness": "recent",
          "verification_level": "provider_verified",
          "source_type": "doctor"
        },
        ...
      }
    """
    from .models import MedicalEvent, ObservationEvent, VisibilityStatus

    stats = {}

    for metric_key, loinc_code in LOINC_CODES.items():
        label = METRIC_LABELS[metric_key]

        # Try LOINC code match first, then name match
        obs = (
            ObservationEvent.objects
            .filter(
                Q(coding_code=loinc_code) |
                Q(observation_name__icontains=label),
                medical_event__patient_id    = patient_id,
                medical_event__visibility_status = VisibilityStatus.VISIBLE,
                medical_event__is_active     = True,
            )
            .select_related("medical_event")
            .order_by("-medical_event__clinical_timestamp")
            .first()
        )

        if not obs:
            stats[metric_key] = None
            continue

        recorded = obs.medical_event.clinical_timestamp.date()
        stats[metric_key] = {
            "label":              label,
            "value":              float(obs.value_quantity) if obs.value_quantity else obs.value_string,
            "unit":               obs.value_unit,
            "recorded_date":      str(recorded),
            "freshness":          _freshness_label(recorded),
            "verification_level": obs.medical_event.verification_level,
            "source_type":        obs.medical_event.source_type,
            "event_id":           str(obs.medical_event_id),
        }

    # Compute BMI from weight + height if not directly recorded
    if not stats.get("bmi") and stats.get("weight_kg") and stats.get("height_cm"):
        try:
            weight = float(stats["weight_kg"]["value"])
            height = float(stats["height_cm"]["value"]) / 100  # cm → m
            bmi    = round(weight / (height ** 2), 1)
            stats["bmi"] = {
                "label":              "BMI (computed)",
                "value":              bmi,
                "unit":               "kg/m²",
                "recorded_date":      stats["weight_kg"]["recorded_date"],
                "freshness":          stats["weight_kg"]["freshness"],
                "verification_level": "computed",
                "source_type":        "system",
                "event_id":           None,
            }
        except (TypeError, ZeroDivisionError):
            pass

    return stats


# ===========================================================================
# MEDICATION HISTORY
# ===========================================================================

def get_medication_history(patient_id: UUID) -> dict:
    """
    Full medication history categorised into active and historical.

    Returns:
      {
        "active":     [ { medication_name, dosage, ... } ],
        "historical": [ { medication_name, dosage, discontinued_date, ... } ]
      }
    """
    from .models import MedicalEvent, MedicationEvent, MedicationStatus, VisibilityStatus

    events = (
        MedicalEvent.objects
        .filter(
            patient_id        = patient_id,
            event_type        = "medication",
            visibility_status = VisibilityStatus.VISIBLE,
            is_active         = True,
        )
        .select_related("medication_event")
        .order_by("-clinical_timestamp")
    )

    active     = []
    historical = []
    seen_roots = set()

    for event in events:
        # Walk to root of lifecycle chain
        root = event
        while root.parent_event_id:
            try:
                root = MedicalEvent.objects.get(pk=root.parent_event_id)
            except MedicalEvent.DoesNotExist:
                break

        if root.id in seen_roots:
            continue
        seen_roots.add(root.id)

        try:
            med = event.medication_event
        except Exception:
            continue

        record = {
            "event_id":        str(event.id),
            "root_event_id":   str(root.id),
            "medication_name": med.medication_name,
            "dosage":          med.dosage,
            "frequency":       med.frequency,
            "route":           med.route,
            "start_date":      str(med.start_date) if med.start_date else None,
            "end_date":        str(med.end_date) if med.end_date else None,
            "status":          med.status,
            "verification_level": event.verification_level,
            "source_type":     event.source_type,
            "clinical_date":   str(event.clinical_timestamp.date()),
        }

        if med.status == MedicationStatus.ACTIVE:
            active.append(record)
        else:
            historical.append(record)

    return {"active": active, "historical": historical}


# ===========================================================================
# IMAGING & RADIOLOGY
# ===========================================================================

def get_imaging_history(patient_id: UUID) -> list:
    """
    All imaging-type documents: X-ray, MRI, CT, ultrasound etc.
    Filters DocumentEvent by document_type=imaging.
    """
    from .models import MedicalEvent, DocumentEvent, VisibilityStatus

    docs = (
        DocumentEvent.objects
        .filter(
            document_type                    = "imaging",
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")
    )

    return [
        {
            "event_id":          str(d.medical_event_id),
            "document_type":     d.document_type,
            "original_filename": d.original_filename,
            "file_type":         d.file_type,
            "clinical_date":     str(d.medical_event.clinical_timestamp.date()),
            "verification_level": d.medical_event.verification_level,
            "source_type":       d.medical_event.source_type,
            "s3_key":            d.s3_key,
        }
        for d in docs
    ]


# ===========================================================================
# CONDITIONS SUMMARY
# ===========================================================================

def get_conditions_summary(patient_id: UUID) -> dict:
    """
    All conditions categorised by clinical status.

    Returns:
      {
        "active":     [ { condition_name, coding_code, onset_date, ... } ],
        "resolved":   [ ... ],
        "other":      [ ... ]  (remission, inactive, etc.)
      }
    """
    from .models import MedicalEvent, ConditionEvent, VisibilityStatus

    conditions = (
        ConditionEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")
    )

    active   = []
    resolved = []
    other    = []

    for cond in conditions:
        record = {
            "event_id":          str(cond.medical_event_id),
            "condition_name":    cond.condition_name,
            "coding_system":     cond.coding_system,
            "coding_code":       cond.coding_code,
            "coding_display":    cond.coding_display,
            "clinical_status":   cond.clinical_status,
            "onset_date":        str(cond.onset_date) if cond.onset_date else None,
            "abatement_date":    str(cond.abatement_date) if cond.abatement_date else None,
            "verification_level": cond.medical_event.verification_level,
            "source_type":       cond.medical_event.source_type,
            "clinical_date":     str(cond.medical_event.clinical_timestamp.date()),
        }

        if cond.clinical_status == "active":
            active.append(record)
        elif cond.clinical_status == "resolved":
            resolved.append(record)
        else:
            other.append(record)

    return {"active": active, "resolved": resolved, "other": other}


# ===========================================================================
# LAB RESULTS HISTORY
# ===========================================================================

def get_lab_results(patient_id: UUID, limit: int = 50) -> list:
    """
    All observation events (lab results, vitals) most recent first.
    """
    from .models import MedicalEvent, ObservationEvent, VisibilityStatus

    obs = (
        ObservationEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")[:limit]
    )

    return [
        {
            "event_id":          str(o.medical_event_id),
            "observation_name":  o.observation_name,
            "coding_system":     o.coding_system,
            "coding_code":       o.coding_code,
            "value_quantity":    float(o.value_quantity) if o.value_quantity else None,
            "value_unit":        o.value_unit,
            "value_string":      o.value_string,
            "reference_range":   o.reference_range,
            "clinical_date":     str(o.medical_event.clinical_timestamp.date()),
            "verification_level": o.medical_event.verification_level,
            "source_type":       o.medical_event.source_type,
        }
        for o in obs
    ]


# ===========================================================================
# PROCEDURE HISTORY
# ===========================================================================

def get_procedure_history(patient_id: UUID) -> list:
    from .models import MedicalEvent, ProcedureEvent, VisibilityStatus

    procs = (
        ProcedureEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")
    )

    return [
        {
            "event_id":       str(p.medical_event_id),
            "procedure_name": p.procedure_name,
            "coding_code":    p.coding_code,
            "coding_display": p.coding_display,
            "performed_date": str(p.performed_date) if p.performed_date else None,
            "notes":          p.notes,
            "verification_level": p.medical_event.verification_level,
            "source_type":    p.medical_event.source_type,
        }
        for p in procs
    ]


# ===========================================================================
# DOCTOR DASHBOARD — single endpoint returns all categories
# ===========================================================================

def get_doctor_dashboard(patient_id: UUID) -> dict:
    """
    Single aggregated call for the doctor dashboard.

    Returns all clinical categories in one response:
      - health_stats:        key vitals and metrics with freshness
      - active_medications:  current medication list
      - active_conditions:   current diagnoses
      - recent_labs:         last 10 lab results
      - imaging:             imaging documents
      - procedures:          surgical/procedure history
      - pending_events:      events awaiting patient approval
                             (doctors need to know their submissions are staged)

    This is designed to give a doctor full clinical context within 2 minutes
    of opening the patient's record — per spec section 16 success criterion.
    """
    from .models import MedicalEvent, VisibilityStatus

    health_stats = get_health_stats(patient_id)
    medications  = get_medication_history(patient_id)
    conditions   = get_conditions_summary(patient_id)
    recent_labs  = get_lab_results(patient_id, limit=10)
    imaging      = get_imaging_history(patient_id)
    procedures   = get_procedure_history(patient_id)

    # Count pending events so doctor knows their contributions are staged
    pending_count = MedicalEvent.objects.filter(
        patient_id        = patient_id,
        visibility_status = VisibilityStatus.PENDING_APPROVAL,
        is_active         = True,
    ).count()

    # Data completeness signal — which key metrics are missing or stale
    missing_metrics = [
        k for k, v in health_stats.items()
        if v is None
    ]
    stale_metrics = [
        k for k, v in health_stats.items()
        if v and v.get("freshness") == "old"
    ]

    return {
        "health_stats":        health_stats,
        "active_medications":  medications["active"],
        "medication_history":  medications["historical"],
        "active_conditions":   conditions["active"],
        "resolved_conditions": conditions["resolved"],
        "recent_labs":         recent_labs,
        "imaging":             imaging,
        "procedures":          procedures,
        "pending_events_count": pending_count,
        "data_quality": {
            "missing_metrics": missing_metrics,
            "stale_metrics":   stale_metrics,
            "note": (
                "missing_metrics = no data recorded. "
                "stale_metrics = last recorded > 12 months ago."
            ),
        },
    }


# ===========================================================================
# ALLERGY SUMMARY (critical — always shown prominently)
# ===========================================================================

def get_allergy_summary(patient_id: UUID) -> dict:
    """
    All active allergies categorised by criticality.
    High-criticality allergies (anaphylaxis risk) are separated
    for prominent display in the doctor dashboard.

    Returns:
      {
        "high_criticality": [ { substance_name, reaction_type, category } ],
        "standard":         [ ... ],
        "total_active":     int
      }
    """
    from .models import AllergyEvent, MedicalEvent, VisibilityStatus, AllergyStatus, AllergyCriticality

    allergies = (
        AllergyEvent.objects
        .filter(
            clinical_status                  = AllergyStatus.ACTIVE,
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("criticality", "substance_name")
    )

    high    = []
    standard = []

    for a in allergies:
        record = {
            "event_id":        str(a.medical_event_id),
            "substance_name":  a.substance_name,
            "allergy_type":    a.allergy_type,
            "category":        a.category,
            "criticality":     a.criticality,
            "reaction_type":   a.reaction_type,
            "reaction_severity": a.reaction_severity,
            "onset_date":      str(a.onset_date) if a.onset_date else None,
            "coding_code":     a.coding_code,
            "verification_level": a.medical_event.verification_level,
        }
        if a.criticality == AllergyCriticality.HIGH:
            high.append(record)
        else:
            standard.append(record)

    return {
        "high_criticality": high,
        "standard":         standard,
        "total_active":     len(high) + len(standard),
    }


# ===========================================================================
# VACCINATION SUMMARY
# ===========================================================================

def get_vaccination_summary(patient_id: UUID) -> list:
    """All vaccinations recorded, most recent first."""
    from .models import VaccinationEvent, VisibilityStatus

    vaccs = (
        VaccinationEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")
    )

    return [
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
        }
        for v in vaccs
    ]


# ===========================================================================
# RECENT VITALS
# ===========================================================================

def get_recent_vitals(patient_id: UUID, limit: int = 5) -> list:
    """
    Most recent vital signs sets.
    Returns structured sets with all components per recording.
    """
    from .models import VitalSignsEvent, VisibilityStatus

    vitals = (
        VitalSignsEvent.objects
        .filter(
            medical_event__patient_id        = patient_id,
            medical_event__visibility_status = VisibilityStatus.VISIBLE,
            medical_event__is_active         = True,
        )
        .select_related("medical_event")
        .order_by("-medical_event__clinical_timestamp")[:limit]
    )

    return [
        {
            "event_id":        str(v.medical_event_id),
            "recorded_at":     str(v.medical_event.clinical_timestamp.date()),
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
            "verification_level": v.medical_event.verification_level,
            "source_type":     v.medical_event.source_type,
        }
        for v in vitals
    ]


# ===========================================================================
# CONSULTATIONS BY DEPARTMENT
# ===========================================================================

def get_consultations(patient_id: UUID, department: str = None) -> list:
    """
    Consultation history, optionally filtered by department.
    """
    from .models import ConsultationEvent, VisibilityStatus

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

    return [
        {
            "event_id":              str(c.medical_event_id),
            "department":            c.department,
            "sub_specialty":         c.sub_specialty,
            "consulting_practitioner": (
                c.consulting_practitioner.full_name
                if c.consulting_practitioner else None
            ),
            "chief_complaint":       c.chief_complaint,
            "assessment":            c.assessment,
            "plan":                  c.plan,
            "follow_up_date":        str(c.follow_up_date) if c.follow_up_date else None,
            "clinical_date":         str(c.medical_event.clinical_timestamp.date()),
            "verification_level":    c.medical_event.verification_level,
        }
        for c in qs
    ]


# ===========================================================================
# PENDING TEST ORDERS
# ===========================================================================

def get_pending_orders(patient_id: UUID) -> list:
    """
    Pending test orders for a patient (from clinical app).
    Used in doctor dashboard to show what's been ordered but not yet resulted.
    """
    try:
        from clinical.models import TestOrder, OrderStatus
        orders = (
            TestOrder.objects
            .filter(
                patient_id = patient_id,
                status__in = [
                    OrderStatus.ACTIVE,
                    OrderStatus.SPECIMEN_COLLECTED,
                    OrderStatus.IN_LAB,
                ],
            )
            .select_related("ordering_practitioner")
            .order_by("priority", "-ordered_at")
        )
        return [
            {
                "order_id":           str(o.id),
                "test_name":          o.test_name,
                "category":           o.category,
                "priority":           o.priority,
                "status":             o.status,
                "ordered_by":         o.ordering_practitioner.full_name,
                "ordered_at":         str(o.ordered_at.date()),
                "due_date":           str(o.due_date) if o.due_date else None,
            }
            for o in orders
        ]
    except Exception:
        return []


# ===========================================================================
# UPDATED DOCTOR DASHBOARD — includes all new categories
# ===========================================================================

def get_doctor_dashboard(patient_id: UUID) -> dict:
    """
    Full doctor dashboard — all clinical categories in one call.

    Sections:
      allergies            ← HIGH CRITICALITY shown first
      recent_vitals        ← latest vital signs sets
      health_stats         ← BMI, BP, glucose, HbA1c etc.
      active_medications
      active_conditions
      pending_orders       ← tests ordered but not yet resulted
      recent_labs          ← last 10 lab results
      consultations        ← recent consultations (all departments)
      imaging
      procedures
      vaccinations
      pending_events_count ← events awaiting patient approval
      data_quality         ← missing/stale metrics signal
    """
    from .models import MedicalEvent, VisibilityStatus

    allergies    = get_allergy_summary(patient_id)
    vitals       = get_recent_vitals(patient_id, limit=3)
    health_stats = get_health_stats(patient_id)
    medications  = get_medication_history(patient_id)
    conditions   = get_conditions_summary(patient_id)
    pending_orders = get_pending_orders(patient_id)
    recent_labs  = get_lab_results(patient_id, limit=10)
    consultations = get_consultations(patient_id)
    imaging      = get_imaging_history(patient_id)
    procedures   = get_procedure_history(patient_id)
    vaccinations = get_vaccination_summary(patient_id)

    pending_count = MedicalEvent.objects.filter(
        patient_id        = patient_id,
        visibility_status = VisibilityStatus.PENDING_APPROVAL,
        is_active         = True,
    ).count()

    missing_metrics = [k for k, v in health_stats.items() if v is None]
    stale_metrics   = [k for k, v in health_stats.items() if v and v.get("freshness") == "old"]

    return {
        # ── Critical first ────────────────────────────────────────────────
        "allergies":             allergies,
        "recent_vitals":         vitals,
        # ── Clinical snapshot ─────────────────────────────────────────────
        "health_stats":          health_stats,
        "active_medications":    medications["active"],
        "active_conditions":     conditions["active"],
        "pending_orders":        pending_orders,
        # ── History ───────────────────────────────────────────────────────
        "recent_labs":           recent_labs,
        "consultations":         consultations,
        "imaging":               imaging,
        "procedures":            procedures,
        "medication_history":    medications["historical"],
        "resolved_conditions":   conditions["resolved"],
        "vaccinations":          vaccinations,
        # ── Metadata ──────────────────────────────────────────────────────
        "pending_events_count":  pending_count,
        "data_quality": {
            "missing_metrics": missing_metrics,
            "stale_metrics":   stale_metrics,
        },
    }