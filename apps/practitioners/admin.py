"""
practitioners/admin.py
======================
Django admin for the practitioners app.

Design rules:
  - Practitioner verification is managed via the membership approval flow,
    not manually set in admin (is_verified is read-only)
  - PractitionerRole inline on Practitioner shows full affiliation history
  - OrgMembershipRequest has its own admin for support investigation
  - Org admin flag (is_org_admin) can be toggled by UHR staff
  - No hard deletes on any model
  - PractitionerRole is append-only — new roles created, old ones deactivated
"""

import logging

from django.contrib import admin
from django.utils.html import format_html, mark_safe

logger = logging.getLogger(__name__)


# ===========================================================================
# INLINE: PractitionerRole on Practitioner detail
# ===========================================================================

class PractitionerRoleInline(admin.TabularInline):
    """
    Shows all role records (active and historical) for a practitioner.
    UHR staff can toggle is_org_admin here.
    Cannot add or delete — roles are created via the membership approval flow.
    """
    from .models import PractitionerRole
    model      = PractitionerRole
    extra      = 0
    can_delete = False

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("organisation")
            .order_by("-created_at")
        )

    readonly_fields = (
        "organisation",
        "role_title",
        "department",
        "start_date",
        "end_date",
        "is_active",
        "is_primary",
        "approved_by",
        "created_at",
    )

    # is_org_admin is editable — UHR staff can promote/demote org admins
    fields = (
        "organisation",
        "role_title",
        "department",
        "is_active",
        "is_primary",
        "is_org_admin",
        "start_date",
        "end_date",
        "approved_by",
    )

    def has_add_permission(self, request, obj=None):
        # Roles are created via membership approval — not manually
        return False


# ===========================================================================
# FILTERS
# ===========================================================================

class IsVerifiedFilter(admin.SimpleListFilter):
    title          = "verification status"
    parameter_name = "verified"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Verified"),
            ("no",  "Unverified"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(is_verified=True)
        if self.value() == "no":
            return queryset.filter(is_verified=False)
        return queryset


class MembershipStatusFilter(admin.SimpleListFilter):
    title          = "request status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return (
            ("pending",   "Pending"),
            ("approved",  "Approved"),
            ("rejected",  "Rejected"),
            ("cancelled", "Cancelled"),
        )

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


# ===========================================================================
# ADMIN: Practitioner
# ===========================================================================

from .models import Practitioner, PractitionerRole, OrgMembershipRequest


@admin.register(Practitioner)
class PractitionerAdmin(admin.ModelAdmin):
    """
    Admin view for Practitioner profiles.

    is_verified is read-only — set automatically on membership approval.
    UHR staff can view full affiliation history via the role inline.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "full_name",
        "user_email",
        "specialization",
        "license_number",
        "primary_org_display",
        "verified_badge",
        "verification_source",
        "is_active",
        "created_at",
    )

    list_filter = (
        IsVerifiedFilter,
        "verification_source",
        "is_active",
        "specialization",
    )

    search_fields = (
        "full_name",
        "license_number",
        "user__email",
        "specialization",
    )

    ordering      = ("full_name",)
    list_per_page = 50

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "id",
        "user",
        "is_verified",          # set by membership approval flow
        "verified_at",
        "verification_source",
        "created_at",
        "updated_at",
        "primary_org_display",
    )

    fieldsets = (
        ("Identity", {
            "fields": (
                "id",
                "user",
                ("full_name", "gender"),
                "birth_date",
            ),
        }),
        ("Qualification", {
            "fields": (
                "license_number",
                "license_issuing_authority",
                "license_expires_at",
                "specialization",
                "qualification",
            ),
        }),
        ("Verification", {
            "fields": (
                "is_verified",
                "verified_at",
                "verification_source",
                "primary_org_display",
            ),
            "description": (
                "Verification is set automatically when an org admin "
                "approves a membership request. Do not modify manually."
            ),
        }),
        ("State", {
            "fields": ("is_active",),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    inlines = [PractitionerRoleInline]

    # ── Permissions ───────────────────────────────────────────────────────────
    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # ── Custom columns ────────────────────────────────────────────────────────
    @admin.display(description="User", ordering="user__email")
    def user_email(self, obj):
        return obj.user.email if obj.user else "—"

    @admin.display(description="Primary Org")
    def primary_org_display(self, obj):
        org = obj.primary_organisation
        if not org:
            return "No affiliation"
        return org.name

    @admin.display(description="Verified")
    def verified_badge(self, obj):
        if obj.is_verified:
            return mark_safe('<span style="color:#27ae60;font-weight:bold;">✓ Verified</span>')
        return mark_safe('<span style="color:#e67e22;">⏳ Unverified</span>')


# ===========================================================================
# ADMIN: PractitionerRole (standalone audit view)
# ===========================================================================

@admin.register(PractitionerRole)
class PractitionerRoleAdmin(admin.ModelAdmin):
    """
    Standalone admin for PractitionerRole records.
    Used for audit investigation and org admin management.

    UHR staff can toggle is_org_admin here to promote/demote org admins.
    All other fields are read-only — roles are created via membership approval.
    """

    list_display = (
        "practitioner_name",
        "organisation_name",
        "role_title",
        "department",
        "is_active",
        "is_primary",
        "is_org_admin",
        "start_date",
        "approved_by",
    )

    list_filter = (
        "is_active",
        "is_primary",
        "is_org_admin",
    )

    search_fields = (
        "practitioner__full_name",
        "practitioner__user__email",
        "organisation__name",
        "role_title",
    )

    ordering      = ("-created_at",)
    list_per_page = 100

    readonly_fields = (
        "id",
        "practitioner",
        "organisation",
        "role_title",
        "department",
        "start_date",
        "end_date",
        "is_active",
        "is_primary",
        "approved_by",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Role", {
            "fields": (
                "id",
                "practitioner",
                "organisation",
                "role_title",
                "department",
            ),
        }),
        ("Period", {
            "fields": (
                "start_date",
                "end_date",
                "is_active",
            ),
        }),
        ("Flags", {
            "fields": (
                "is_primary",
                "is_org_admin",   # editable — UHR staff can promote/demote
            ),
            "description": (
                "is_org_admin can be toggled here by UHR staff. "
                "All other fields are set by the membership approval flow."
            ),
        }),
        ("Audit", {
            "fields": (
                "approved_by",
                "created_at",
                "updated_at",
            ),
            "classes": ("collapse",),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.display(description="Practitioner", ordering="practitioner__full_name")
    def practitioner_name(self, obj):
        return obj.practitioner.full_name

    @admin.display(description="Organisation", ordering="organisation__name")
    def organisation_name(self, obj):
        return obj.organisation.name


# ===========================================================================
# ADMIN: OrgMembershipRequest (support investigation view)
# ===========================================================================

@admin.register(OrgMembershipRequest)
class OrgMembershipRequestAdmin(admin.ModelAdmin):
    """
    Admin view for OrgMembershipRequest records.
    Used by support for investigation of membership disputes.

    Fully read-only — approvals and rejections go through the API.
    """

    list_display = (
        "practitioner_name",
        "organisation_name",
        "requested_role_title",
        "status_badge",
        "responded_at",
        "responded_by",
        "created_at",
    )

    list_filter = (
        MembershipStatusFilter,
    )

    search_fields = (
        "practitioner__full_name",
        "practitioner__user__email",
        "organisation__name",
    )

    ordering      = ("-created_at",)
    list_per_page = 100

    readonly_fields = (
        "id",
        "practitioner",
        "organisation",
        "requested_role_title",
        "requested_department",
        "message",
        "status",
        "responded_at",
        "responded_by",
        "rejection_reason",
        "resulting_role",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Request", {
            "fields": (
                "id",
                "practitioner",
                "organisation",
                "requested_role_title",
                "requested_department",
                "message",
                "status",
            ),
        }),
        ("Response", {
            "fields": (
                "responded_at",
                "responded_by",
                "rejection_reason",
                "resulting_role",
            ),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

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

    @admin.display(description="Practitioner", ordering="practitioner__full_name")
    def practitioner_name(self, obj):
        return obj.practitioner.full_name

    @admin.display(description="Organisation", ordering="organisation__name")
    def organisation_name(self, obj):
        return obj.organisation.name

    @admin.display(description="Status")
    def status_badge(self, obj):
        colours = {
            "pending":   ("#e67e22", "⏳ Pending"),
            "approved":  ("#27ae60", "✓ Approved"),
            "rejected":  ("#c0392b", "✗ Rejected"),
            "cancelled": ("#7f8c8d", "⊘ Cancelled"),
        }
        colour, label = colours.get(obj.status, ("#333", obj.status))
        return mark_safe(f'<span style="color:{colour};font-weight:bold;">{label}</span>')