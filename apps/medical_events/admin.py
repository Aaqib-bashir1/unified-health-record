"""
medical_events/admin.py
=======================
Django admin for the medical_events app.

Design rules:
  - MedicalEvent is fully immutable — no add/change/delete anywhere
  - Typed extension shown as inline based on event_type
  - Bulk approve action for pending_approval events (common support task)
  - validator_hash and checksum never shown
  - Colour-coded verification and visibility badges
"""

import logging

from django.contrib import admin
from django.utils.html import format_html, mark_safe

from .models import (
    AllergyEvent,
    ConditionEvent,
    ConsultationEvent,
    DocumentEvent,
    MedicalEvent,
    MedicationEvent,
    ObservationEvent,
    ProcedureEvent,
    SecondOpinionEvent,
    VaccinationEvent,
    VisibilityStatus,
    VisitEvent,
    VitalSignsEvent,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# BASE INLINE — all typed extension inlines inherit this
# ===========================================================================

class BaseExtensionInline(admin.StackedInline):
    extra      = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class VisitEventInline(BaseExtensionInline):
    model           = VisitEvent
    readonly_fields = ("reason", "visit_type", "notes")


class ObservationEventInline(BaseExtensionInline):
    model           = ObservationEvent
    readonly_fields = (
        "observation_name",
        "coding_system", "coding_code", "coding_display",
        "value_type", "value_quantity", "value_unit", "value_string",
        "reference_range",
    )


class ConditionEventInline(BaseExtensionInline):
    model           = ConditionEvent
    readonly_fields = (
        "condition_name",
        "coding_system", "coding_code", "coding_display",
        "clinical_status", "onset_date", "abatement_date", "notes",
    )


class MedicationEventInline(BaseExtensionInline):
    model           = MedicationEvent
    readonly_fields = (
        "medication_name", "dosage", "frequency",
        "route", "start_date", "end_date", "status", "notes",
    )


class ProcedureEventInline(BaseExtensionInline):
    model           = ProcedureEvent
    readonly_fields = (
        "procedure_name",
        "coding_system", "coding_code", "coding_display",
        "performed_date", "notes",
    )


class DocumentEventInline(BaseExtensionInline):
    model           = DocumentEvent
    readonly_fields = (
        "document_type", "original_filename", "file_type",
        "file_size_bytes", "storage_provider",
        "s3_bucket", "s3_key",
        # checksum shown for integrity audit — read-only, no security risk
        "checksum",
        # file_url excluded — generate presigned URL on demand via the API
    )


class SecondOpinionEventInline(BaseExtensionInline):
    model           = SecondOpinionEvent
    readonly_fields = (
        "doctor_name",
        "doctor_registration_number",
        "opinion_text",
        "approved_by_patient",
    )


class AllergyEventInline(BaseExtensionInline):
    model           = AllergyEvent
    readonly_fields = (
        "substance_name", "allergy_type", "category",
        "criticality", "reaction_type", "reaction_severity",
        "clinical_status", "onset_date", "coding_code", "notes",
    )


class VaccinationEventInline(BaseExtensionInline):
    model           = VaccinationEvent
    readonly_fields = (
        "vaccine_name", "coding_code", "dose_number",
        "lot_number", "administered_date", "next_dose_due_date",
        "administering_org", "site", "route", "notes",
    )


class ConsultationEventInline(BaseExtensionInline):
    model           = ConsultationEvent
    readonly_fields = (
        "department", "sub_specialty",
        "consulting_practitioner", "referred_by",
        "chief_complaint", "history_of_present_illness",
        "examination_findings", "investigations_ordered",
        "assessment", "plan",
        "follow_up_date", "follow_up_instructions",
    )


class VitalSignsEventInline(BaseExtensionInline):
    model           = VitalSignsEvent
    readonly_fields = (
        "systolic_bp", "diastolic_bp", "bp_position",
        "heart_rate", "heart_rhythm",
        "temperature", "temp_site",
        "spo2", "on_oxygen", "respiratory_rate",
        "weight_kg", "height_cm", "bmi",
        "pain_score", "notes",
    )


# ===========================================================================
# FILTERS
# ===========================================================================

class PendingApprovalFilter(admin.SimpleListFilter):
    title          = "approval status"
    parameter_name = "pending"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Pending approval only"),
            ("no",  "Not pending"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(visibility_status=VisibilityStatus.PENDING_APPROVAL)
        if self.value() == "no":
            return queryset.exclude(visibility_status=VisibilityStatus.PENDING_APPROVAL)
        return queryset


# ===========================================================================
# ADMIN: MedicalEvent
# ===========================================================================

@admin.register(MedicalEvent)
class MedicalEventAdmin(admin.ModelAdmin):
    """
    Core timeline admin. Fully immutable — read-only at every level.
    Bulk approve action handles the most common support task.
    Typed extension inline dynamically shown based on event_type.
    """

    list_display = (
        "id_short",
        "patient",
        "event_type",
        "clinical_timestamp",
        "source_type",
        "verification_badge",
        "visibility_badge",
        "created_by",
    )

    list_filter = (
        "event_type",
        "source_type",
        "verification_level",
        PendingApprovalFilter,
    )

    search_fields = (
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "external_resource_id",
        "fhir_logical_id",
    )

    ordering      = ("-clinical_timestamp",)
    list_per_page = 50

    readonly_fields = (
        "id",
        "patient",
        "event_type",
        "clinical_timestamp",
        "system_timestamp",
        "source_type",
        "source_practitioner",
        "source_organisation",
        "verification_level",
        "visibility_status",
        "created_by",
        "amends_event",
        "amendment_reason",
        "parent_event",
        "relationship_type",
        "external_system",
        "external_resource_id",
        "fhir_resource_type",
        "fhir_logical_id",
        "created_at",
    )

    fieldsets = (
        ("Core", {
            "fields": (
                "id",
                "patient",
                "event_type",
                ("clinical_timestamp", "system_timestamp"),
            ),
        }),
        ("Provenance", {
            "fields": (
                "source_type",
                "source_practitioner",
                "source_organisation",
                "created_by",
            ),
        }),
        ("Verification & Visibility", {
            "fields": (
                "verification_level",
                "visibility_status",
            ),
        }),
        ("Relationships", {
            "fields": (
                "relationship_type",
                "amends_event",
                "amendment_reason",
                "parent_event",
            ),
        }),
        ("FHIR / External", {
            "fields": (
                "external_system",
                "external_resource_id",
                "fhir_resource_type",
                "fhir_logical_id",
            ),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )

    actions = ["action_bulk_approve", "action_bulk_hide"]

    # ── Actions ───────────────────────────────────────────────────────────────

    @admin.action(description="✅ Approve selected pending events (make visible)")
    def action_bulk_approve(self, request, queryset):
        pending = queryset.filter(visibility_status=VisibilityStatus.PENDING_APPROVAL)
        count   = pending.count()
        pending.update(visibility_status=VisibilityStatus.VISIBLE)

        # Also mark second opinions as approved
        for event in pending.filter(event_type="second_opinion"):
            try:
                event.second_opinion_event.approved_by_patient = True
                event.second_opinion_event.save(update_fields=["approved_by_patient"])
            except Exception:
                pass

        skipped = queryset.count() - count
        msg     = f"{count} event(s) approved."
        if skipped:
            msg += f" {skipped} skipped (not pending approval)."
        self.message_user(request, msg)
        logger.info("Admin bulk approve. count=%s by=%s", count, request.user.email)

    @admin.action(description="🙈 Hide selected events")
    def action_bulk_hide(self, request, queryset):
        visible = queryset.filter(visibility_status=VisibilityStatus.VISIBLE)
        count   = visible.count()
        visible.update(visibility_status=VisibilityStatus.HIDDEN)
        self.message_user(request, f"{count} event(s) hidden.")

    # ── Permissions ───────────────────────────────────────────────────────────

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # ── Dynamic inline based on event_type ───────────────────────────────────

    def get_inline_instances(self, request, obj=None):
        if not obj:
            return []
        mapping = {
            "visit":          VisitEventInline,
            "observation":    ObservationEventInline,
            "condition":      ConditionEventInline,
            "medication":     MedicationEventInline,
            "procedure":      ProcedureEventInline,
            "document":       DocumentEventInline,
            "second_opinion": SecondOpinionEventInline,
            "allergy":        AllergyEventInline,
            "vaccination":    VaccinationEventInline,
            "consultation":   ConsultationEventInline,
            "vital_signs":    VitalSignsEventInline,
        }
        inline_class = mapping.get(obj.event_type)
        if not inline_class:
            return []
        return [inline_class(self.model, self.admin_site)]

    # ── Custom columns ────────────────────────────────────────────────────────

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.id)[:8] + "..."

    @admin.display(description="Verification")
    def verification_badge(self, obj):
        colours = {
            "self_reported":      "#7f8c8d",
            "patient_confirmed":  "#2980b9",
            "provider_verified":  "#27ae60",
            "digitally_verified": "#8e44ad",
        }
        colour = colours.get(obj.verification_level, "#333")
        return format_html(
            '<span style="color:{};font-weight:bold;">{}</span>',
            colour,
            obj.get_verification_level_display(),
        )

    @admin.display(description="Visibility")
    def visibility_badge(self, obj):
        colours = {
            "visible":          "#27ae60",
            "hidden":           "#7f8c8d",
            "pending_approval": "#e67e22",
        }
        colour = colours.get(obj.visibility_status, "#333")
        return format_html(
            '<span style="color:{};font-weight:bold;">{}</span>',
            colour,
            obj.get_visibility_status_display(),
        )


# ===========================================================================
# EXTENSION ADMINS — standalone read-only views
# ===========================================================================

class BaseExtensionAdmin(admin.ModelAdmin):
    """All extension model admins inherit this — fully read-only."""

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


@admin.register(VisitEvent)
class VisitEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "visit_type", "reason")
    search_fields  = ("medical_event__patient__first_name", "medical_event__patient__last_name")


@admin.register(ObservationEvent)
class ObservationEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "observation_name", "value_quantity", "value_unit", "coding_code")
    search_fields  = ("observation_name", "coding_code", "medical_event__patient__last_name")
    list_filter    = ("coding_system",)


@admin.register(ConditionEvent)
class ConditionEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "condition_name", "clinical_status", "coding_code")
    search_fields  = ("condition_name", "coding_code", "medical_event__patient__last_name")
    list_filter    = ("clinical_status",)


@admin.register(MedicationEvent)
class MedicationEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "medication_name", "dosage", "frequency", "status")
    search_fields  = ("medication_name", "medical_event__patient__last_name")
    list_filter    = ("status",)


@admin.register(ProcedureEvent)
class ProcedureEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "procedure_name", "performed_date", "coding_code")
    search_fields  = ("procedure_name", "coding_code", "medical_event__patient__last_name")


@admin.register(DocumentEvent)
class DocumentEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "document_type", "original_filename", "file_type", "file_size_bytes")
    search_fields  = ("original_filename", "medical_event__patient__last_name")
    list_filter    = ("document_type", "storage_provider")


@admin.register(SecondOpinionEvent)
class SecondOpinionEventAdmin(BaseExtensionAdmin):
    list_display   = ("medical_event", "doctor_name", "doctor_registration_number", "approved_by_patient")
    search_fields  = ("doctor_name", "medical_event__patient__last_name")
    list_filter    = ("approved_by_patient",)


@admin.register(AllergyEvent)
class AllergyEventAdmin(BaseExtensionAdmin):
    list_display  = ("medical_event", "substance_name", "criticality_badge", "category", "clinical_status")
    search_fields = ("substance_name", "coding_code", "medical_event__patient__last_name")
    list_filter   = ("criticality", "category", "clinical_status", "allergy_type")

    @admin.display(description="Criticality")
    def criticality_badge(self, obj):
        if obj.criticality == "high":
            return mark_safe('<span style="color:#c0392b;font-weight:bold;">🔴 HIGH</span>')
        if obj.criticality == "low":
            return mark_safe('<span style="color:#27ae60;">🟢 low</span>')
        return mark_safe('<span style="color:#7f8c8d;">⚪ unknown</span>')


@admin.register(VaccinationEvent)
class VaccinationEventAdmin(BaseExtensionAdmin):
    list_display  = ("medical_event", "vaccine_name", "dose_number", "administered_date", "next_dose_due_date")
    search_fields = ("vaccine_name", "coding_code", "medical_event__patient__last_name")
    list_filter   = ("coding_system",)


@admin.register(ConsultationEvent)
class ConsultationEventAdmin(BaseExtensionAdmin):
    list_display  = ("medical_event", "department", "sub_specialty", "consulting_practitioner", "chief_complaint_short", "follow_up_date")
    search_fields = ("chief_complaint", "assessment", "medical_event__patient__last_name", "consulting_practitioner__full_name")
    list_filter   = ("department",)

    @admin.display(description="Chief complaint")
    def chief_complaint_short(self, obj):
        return obj.chief_complaint[:60] + ("…" if len(obj.chief_complaint) > 60 else "")


@admin.register(VitalSignsEvent)
class VitalSignsEventAdmin(BaseExtensionAdmin):
    list_display  = ("medical_event", "systolic_bp", "diastolic_bp", "heart_rate", "temperature", "spo2", "bmi")
    search_fields = ("medical_event__patient__first_name", "medical_event__patient__last_name")