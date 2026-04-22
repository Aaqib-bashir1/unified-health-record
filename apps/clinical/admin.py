"""clinical/admin.py — Django admin for TestOrder."""

import logging

from django.contrib import admin
from django.utils.html import mark_safe

from .models import OrderStatus, TestOrder

logger = logging.getLogger(__name__)


class OrderStatusFilter(admin.SimpleListFilter):
    title          = "order status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return OrderStatus.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


@admin.register(TestOrder)
class TestOrderAdmin(admin.ModelAdmin):
    """
    Admin view for TestOrder.
    Status can be updated by staff for manual overrides.
    CPOE fields shown in a collapsed section.
    """

    list_display = (
        "test_name",
        "patient",
        "ordering_practitioner",
        "category",
        "priority_badge",
        "status_badge",
        "ordered_at",
        "due_date",
        "resulting_event",
    )

    list_filter  = (
        OrderStatusFilter,
        "category",
        "priority",
    )

    search_fields = (
        "test_name",
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "ordering_practitioner__full_name",
        "cpoe_order_id",
    )

    ordering      = ("-ordered_at",)
    list_per_page = 100

    readonly_fields = (
        "id",
        "patient",
        "ordering_practitioner",
        "ordering_organisation",
        "created_by",
        "ordered_at",
        "specimen_collected_at",
        "resulted_at",
        "cancelled_at",
        "resulting_event",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Order", {
            "fields": (
                "id",
                "patient",
                "test_name",
                "category",
                ("priority", "status"),
                "due_date",
            ),
        }),
        ("Coding", {
            "fields": (
                "coding_system",
                "coding_code",
                "coding_display",
            ),
            "classes": ("collapse",),
        }),
        ("Clinical", {
            "fields": (
                "clinical_reason",
                "special_instructions",
                "specimen_type",
            ),
        }),
        ("Ordering Context", {
            "fields": (
                "ordering_practitioner",
                "ordering_organisation",
                "created_by",
                "ordered_at",
            ),
        }),
        ("Status Timeline", {
            "fields": (
                "specimen_collected_at",
                "resulted_at",
                "cancelled_at",
                "cancellation_reason",
                "resulting_event",
            ),
            "classes": ("collapse",),
        }),
        ("CPOE", {
            "fields": (
                "cpoe_order_id",
                "order_set_id",
                "billing_code",
                "lab_interface_code",
                "destination_lab",
                "requires_auth",
                "auth_reference",
            ),
            "classes": ("collapse",),
            "description": "Fields for CPOE integration. Nullable until CPOE is enabled.",
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.display(description="Priority")
    def priority_badge(self, obj):
        colours = {
            "routine": "#27ae60",
            "urgent":  "#e67e22",
            "stat":    "#c0392b",
            "asap":    "#e74c3c",
        }
        colour = colours.get(obj.priority, "#333")
        return mark_safe(
            f'<span style="color:{colour};font-weight:bold;">'
            f'{obj.priority.upper()}</span>'
        )

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "draft":               "#95a5a6",
            "active":              "#2980b9",
            "specimen_collected":  "#8e44ad",
            "in_lab":              "#e67e22",
            "resulted":            "#27ae60",
            "cancelled":           "#c0392b",
            "on_hold":             "#7f8c8d",
        }
        colour = colours.get(obj.status, "#333")
        label  = obj.get_status_display()
        return mark_safe(
            f'<span style="color:{colour};font-weight:bold;">{label}</span>'
        )