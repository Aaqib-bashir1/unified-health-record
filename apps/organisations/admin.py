"""
organisations/admin.py
======================
Django admin for the organisations app.

Design rules:
  - UHR staff can verify organisations via a dedicated action
  - Registration number and type are read-only after creation
  - Branches (child orgs) shown as inline on parent detail
  - Deactivated orgs visible but clearly marked
  - No hard deletes — deactivate only
  - Verify action restricted to is_staff users
"""

import logging

from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, mark_safe

from .models import Organisation, OrganisationType

logger = logging.getLogger(__name__)


# ===========================================================================
# FILTERS
# ===========================================================================

class IsVerifiedFilter(admin.SimpleListFilter):
    title          = "verification status"
    parameter_name = "verified"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Verified only"),
            ("no",  "Unverified only"),
            ("all", "All"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(verified=True)
        if self.value() == "no":
            return queryset.filter(verified=False)
        return queryset

    def choices(self, changelist):
        for lookup, title in self.lookup_choices:
            yield {
                "selected": (
                    self.value() == lookup
                    or (self.value() is None and lookup == "yes")
                ),
                "query_string": changelist.get_query_string(
                    {self.parameter_name: lookup}
                ),
                "display": title,
            }


class IsActiveFilter(admin.SimpleListFilter):
    title          = "active status"
    parameter_name = "active"

    def lookups(self, request, model_admin):
        return (
            ("yes", "Active only"),
            ("no",  "Deactivated only"),
            ("all", "All"),
        )

    def queryset(self, request, queryset):
        if self.value() == "no":
            return queryset.filter(is_active=False)
        if self.value() == "all":
            return queryset
        return queryset.filter(is_active=True)

    def choices(self, changelist):
        for lookup, title in self.lookup_choices:
            yield {
                "selected": (
                    self.value() == lookup
                    or (self.value() is None and lookup == "yes")
                ),
                "query_string": changelist.get_query_string(
                    {self.parameter_name: lookup}
                ),
                "display": title,
            }


# ===========================================================================
# INLINE: Branches on parent Organisation
# ===========================================================================

class BranchInline(admin.TabularInline):
    """
    Shows child organisations (branches) on the parent detail page.
    Read-only — branch relationships are set at branch creation time.
    """
    model          = Organisation
    fk_name        = "parent"
    extra          = 0
    can_delete     = False
    show_change_link = True

    readonly_fields = ("name", "type", "country", "verified", "is_active")
    fields          = ("name", "type", "country", "verified", "is_active")

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ===========================================================================
# ADMIN: Organisation
# ===========================================================================

@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    """
    Admin view for Organisation.

    UHR staff can verify organisations here via the verify action.
    Registration number and type are read-only after creation.
    Deactivated orgs are hidden from the default list view.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "name",
        "type",
        "country",
        "registration_number",
        "parent",
        "verified_badge",
        "status_badge",
        "created_at",
    )

    list_filter  = (
        IsVerifiedFilter,
        IsActiveFilter,
        "type",
        "country",
    )

    search_fields = (
        "name",
        "registration_number",
    )

    ordering      = ("name",)
    list_per_page = 50

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "id",
        "type",                  # immutable after creation
        "registration_number",   # immutable after creation
        "verified",
        "verified_at",
        "verified_by",
        "is_active",
        "deactivated_at",
        "deactivation_reason",
        "created_at",
        "updated_at",
        "branch_count",
        "practitioner_count",
    )

    fieldsets = (
        ("Identity", {
            "fields": (
                "id",
                ("name", "type"),
                "registration_number",
                "parent",
                ("branch_count", "practitioner_count"),
            ),
        }),
        ("Details", {
            "fields": (
                "description",
                "website",
            ),
        }),
        ("Contact", {
            "fields": (
                "email",
                "phone",
                "address",
                "country",
            ),
        }),
        ("Verification", {
            "fields": (
                "verified",
                "verified_at",
                "verified_by",
            ),
            "description": (
                "Verification is managed via the 'Verify selected organisations' action. "
                "Only UHR staff can verify organisations."
            ),
        }),
        ("State", {
            "fields": (
                "is_active",
                "deactivated_at",
                "deactivation_reason",
            ),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    inlines = [BranchInline]

    # ── Actions ───────────────────────────────────────────────────────────────
    actions = ["action_verify_organisations"]

    @admin.action(description="✅ Verify selected organisations (UHR staff only)")
    def action_verify_organisations(self, request, queryset):
        if not request.user.is_staff:
            self.message_user(
                request,
                "Only UHR staff can verify organisations.",
                level="error",
            )
            return

        already_verified = queryset.filter(verified=True).count()
        to_verify        = queryset.filter(verified=False, is_active=True)
        count            = to_verify.count()

        now = timezone.now()
        to_verify.update(
            verified    = True,
            verified_at = now,
            verified_by = request.user,
        )

        msg = f"{count} organisation(s) verified."
        if already_verified:
            msg += f" {already_verified} were already verified and skipped."
        self.message_user(request, msg)

        logger.info(
            "Bulk org verification. count=%s by admin=%s",
            count, request.user.email,
        )

    # ── Permissions ───────────────────────────────────────────────────────────
    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # ── Custom columns ────────────────────────────────────────────────────────
    @admin.display(description="Verified")
    def verified_badge(self, obj):
        if obj.verified:
            return mark_safe('<span style="color:#27ae60;font-weight:bold;">✓ Verified</span>')
        return mark_safe('<span style="color:#e67e22;">⏳ Pending</span>')

    @admin.display(description="Status")
    def status_badge(self, obj):
        if not obj.is_active:
            return mark_safe('<span style="color:#c0392b;font-weight:bold;">⊘ Deactivated</span>')
        return mark_safe('<span style="color:#27ae60;">● Active</span>')

    @admin.display(description="Branches")
    def branch_count(self, obj):
        count = obj.branches.filter(is_active=True).count()
        return f"{count} active branch(es)"

    @admin.display(description="Practitioners")
    def practitioner_count(self, obj):
        try:
            from practitioners.models import PractitionerRole
            active = PractitionerRole.objects.filter(
                organisation=obj, is_active=True
            ).count()
            return f"{active} active"
        except Exception:
            return "—"
        
