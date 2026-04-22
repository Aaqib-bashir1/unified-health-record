from django.contrib import admin

# Register your models here.
"""
lab_reports/admin.py
====================
Django admin for the lab_reports app.

Design rules:
  - LabReport is append-only — no add/change/delete
  - LabReportField is read-only — patient reviews happen via API
  - Bulk approve action for reports stuck in pending_review
  - Abnormal fields highlighted in red
  - Integration admin for staff to manage org lab connections
  - All OCR raw output preserved and visible to staff for debugging
"""

import logging

from django.contrib import admin
from django.utils.html import format_html, mark_safe

from .models import (
    FieldStatus,
    LabIntegration,
    LabPanel,
    LabReport,
    LabReportField,
    LabReportSource,
    LabReportStatus,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# FILTERS
# ===========================================================================

class LabSourceFilter(admin.SimpleListFilter):
    title          = "ingestion source"
    parameter_name = "source"

    def lookups(self, request, model_admin):
        return LabReportSource.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(source=self.value())
        return queryset


class LabStatusFilter(admin.SimpleListFilter):
    title          = "report status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return LabReportStatus.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


class AbnormalFilter(admin.SimpleListFilter):
    title          = "has abnormal fields"
    parameter_name = "has_abnormal"

    def lookups(self, request, model_admin):
        return (("yes", "Has abnormal fields"), ("no", "All normal"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(fields__is_abnormal=True).distinct()
        if self.value() == "no":
            return queryset.exclude(fields__is_abnormal=True).distinct()
        return queryset


# ===========================================================================
# INLINE: LabReportField on LabReport detail
# ===========================================================================

class LabReportFieldInline(admin.TabularInline):
    model      = LabReportField
    extra      = 0
    can_delete = False

    def get_queryset(self, request):
        return super().get_queryset(request).order_by("display_order", "test_name")

    readonly_fields = (
        "test_name",
        "loinc_code",
        "extracted_value",
        "extracted_unit",
        "patient_corrected_value",
        "effective_value_display",
        "reference_range",
        "is_abnormal",
        "abnormal_flag",
        "field_confidence",
        "status",
        "resulting_event",
    )

    fields = (
        "test_name",
        "extracted_value",
        "extracted_unit",
        "patient_corrected_value",
        "effective_value_display",
        "reference_range",
        "is_abnormal",
        "abnormal_flag",
        "field_confidence",
        "status",
        "resulting_event",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Effective value")
    def effective_value_display(self, obj):
        value = obj.confirmed_value
        unit  = obj.confirmed_unit or ""
        if obj.is_abnormal:
            return mark_safe(
                f'<span style="color:#c0392b;font-weight:bold;">'
                f'{value} {unit} ⚠️</span>'
            )
        return f"{value} {unit}".strip()


# ===========================================================================
# ADMIN: LabReport
# ===========================================================================

@admin.register(LabReport)
class LabReportAdmin(admin.ModelAdmin):
    """
    Admin view for LabReport. Fully read-only.
    Bulk approve action pushes stuck pending_review reports to confirmed.
    """

    list_display = (
        "id_short",
        "patient",
        "source_badge",
        "status_badge",
        "lab_name",
        "report_date",
        "field_count",
        "abnormal_count",
        "ocr_confidence_display",
        "created_at",
    )

    list_filter  = (
        LabStatusFilter,
        LabSourceFilter,
        AbnormalFilter,
    )

    search_fields = (
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "lab_name",
        "report_id",
    )

    ordering      = ("-created_at",)
    list_per_page = 50

    readonly_fields = (
        "id",
        "patient",
        "created_by",
        "source",
        "status",
        "integration",
        "uploading_organisation",
        "lab_name",
        "report_date",
        "report_id",
        "ordered_by",
        "panel",
        "document_event",
        "ocr_provider",
        "ocr_confidence",
        "ocr_completed_at",
        "ocr_error_message",
        "confirmed_at",
        "confirmed_by",
        "resulted_at",
        "notes",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Report", {
            "fields": (
                "id",
                "patient",
                "source",
                "status",
                ("lab_name", "report_date", "report_id"),
                "document_event",
                "panel",
            ),
        }),
        ("Source", {
            "fields": (
                "uploading_organisation",
                "integration",
                "ordered_by",
                "created_by",
            ),
        }),
        ("OCR", {
            "fields": (
                "ocr_provider",
                "ocr_confidence",
                "ocr_completed_at",
                "ocr_error_message",
            ),
            "classes": ("collapse",),
        }),
        ("Lifecycle", {
            "fields": (
                "confirmed_at",
                "confirmed_by",
                "resulted_at",
            ),
            "classes": ("collapse",),
        }),
        ("Notes", {
            "fields": ("notes",),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    inlines = [LabReportFieldInline]
    actions = ["action_approve_pending"]

    # ── Actions ───────────────────────────────────────────────────────────────

    @admin.action(description="✅ Force-confirm all EXTRACTED fields and result report")
    def action_approve_pending(self, request, queryset):
        from django.utils import timezone
        pending = queryset.filter(status=LabReportStatus.PENDING_REVIEW)
        count   = 0
        now     = timezone.now()

        for report in pending:
            report.fields.filter(status=FieldStatus.EXTRACTED).update(
                status      = FieldStatus.CONFIRMED,
                reviewed_at = now,
                reviewed_by = request.user,
            )
            report.status       = LabReportStatus.CONFIRMED
            report.confirmed_at = now
            report.confirmed_by = request.user
            report.save(update_fields=["status", "confirmed_at", "confirmed_by", "updated_at"])

            try:
                from .services import result_report
                result_report(request.user, report.id)
                count += 1
            except Exception as e:
                logger.error("Admin force-result failed for %s: %s", report.id, e)

        skipped = queryset.count() - count
        msg = f"{count} report(s) confirmed and resulted."
        if skipped:
            msg += f" {skipped} skipped (not in pending_review state)."
        self.message_user(request, msg)

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

    # ── Custom columns ────────────────────────────────────────────────────────

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.id)[:8] + "…"

    @admin.display(description="Source")
    def source_badge(self, obj):
        colours = {
            "patient_upload":   "#2980b9",
            "organisation_push": "#27ae60",
            "lab_integration":  "#8e44ad",
            "manual_entry":     "#7f8c8d",
        }
        colour = colours.get(obj.source, "#333")
        return mark_safe(
            f'<span style="color:{colour};font-weight:bold;">'
            f'{obj.get_source_display()}</span>'
        )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "uploaded":       "#95a5a6",
            "extracting":     "#2980b9",
            "extracted":      "#8e44ad",
            "pending_review": "#e67e22",
            "confirmed":      "#2ecc71",
            "resulted":       "#27ae60",
            "received":       "#1abc9c",
            "failed":         "#c0392b",
        }
        colour = colours.get(obj.status, "#333")
        return mark_safe(
            f'<span style="color:{colour};font-weight:bold;">'
            f'{obj.get_status_display()}</span>'
        )

    @admin.display(description="Fields")
    def field_count(self, obj):
        total   = obj.fields.count()
        pending = obj.fields.filter(status=FieldStatus.EXTRACTED).count()
        if pending:
            return mark_safe(
                f'{total} total '
                f'<span style="color:#e67e22;">({pending} pending)</span>'
            )
        return f"{total}"

    @admin.display(description="Abnormal")
    def abnormal_count(self, obj):
        count = obj.fields.filter(is_abnormal=True).count()
        if count:
            return mark_safe(
                f'<span style="color:#c0392b;font-weight:bold;">⚠️ {count}</span>'
            )
        return "—"

    @admin.display(description="OCR confidence")
    def ocr_confidence_display(self, obj):
        if obj.ocr_confidence is None:
            return "—"
        pct = int(obj.ocr_confidence * 100)
        colour = "#27ae60" if pct >= 85 else "#e67e22" if pct >= 60 else "#c0392b"
        return mark_safe(
            f'<span style="color:{colour};">{pct}%</span>'
        )


# ===========================================================================
# ADMIN: LabReportField (standalone audit view)
# ===========================================================================

@admin.register(LabReportField)
class LabReportFieldAdmin(admin.ModelAdmin):
    """Read-only audit view for individual lab fields."""

    list_display = (
        "test_name",
        "patient_name",
        "extracted_value",
        "extracted_unit",
        "is_abnormal",
        "abnormal_flag",
        "status",
        "resulting_event",
        "field_confidence",
    )

    list_filter  = ("status", "is_abnormal")
    search_fields = (
        "test_name",
        "loinc_code",
        "lab_report__patient__last_name",
    )

    ordering      = ("-created_at",)
    list_per_page = 100

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

    @admin.display(description="Patient")
    def patient_name(self, obj):
        return obj.lab_report.patient.full_name


# ===========================================================================
# ADMIN: LabIntegration
# ===========================================================================

@admin.register(LabIntegration)
class LabIntegrationAdmin(admin.ModelAdmin):
    """
    Admin for lab integration connections.
    is_active and auto_import can be toggled by staff.
    Credentials never stored here — config_reference points to secrets manager.
    """

    list_display = (
        "name",
        "organisation",
        "protocol",
        "auto_import",
        "is_active",
        "last_sync_at",
    )

    list_filter  = ("protocol", "is_active", "auto_import")
    search_fields = ("name", "organisation__name")
    ordering      = ("organisation__name", "name")

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "last_sync_at",
    )

    fieldsets = (
        ("Integration", {
            "fields": (
                "id",
                "organisation",
                "name",
                "protocol",
                "endpoint",
            ),
        }),
        ("Behaviour", {
            "fields": (
                "auto_import",
                "is_active",
            ),
            "description": (
                "auto_import=True means results skip patient review — "
                "ObservationEvents created immediately with digitally_verified. "
                "Only enable for trusted, certified lab systems."
            ),
        }),
        ("Security", {
            "fields": ("credentials_encrypted",),
            "description": (
                "Store only a reference key here. "
                "Actual credentials must live in a secrets manager."
            ),
            "classes": ("collapse",),
        }),
        ("FHIR", {
            "fields": ("fhir_system_uri",),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("last_sync_at", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


# ===========================================================================
# ADMIN: LabPanel
# ===========================================================================

@admin.register(LabPanel)
class LabPanelAdmin(admin.ModelAdmin):
    """Reference data for lab panels — staff-managed."""

    list_display  = ("name", "display_name", "loinc_code", "is_active")
    search_fields = ("name", "display_name", "loinc_code")
    list_filter   = ("is_active",)
    ordering      = ("name",)

    def has_delete_permission(self, request, obj=None):
        return False