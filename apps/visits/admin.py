"""
visits/admin.py
===============
Django admin for the visits app.

Design rules:
  - PatientVisit list defaults to active visits
  - PatientVisitAccess is read-only — purely an audit view (lazy-created records)
  - UHR staff can end visits early via a bulk action
  - No hard deletes on either model
  - PatientVisitAccess shown as inline on PatientVisit detail
  - Expired visits are clearly distinguished from ended visits
"""

import logging

from django.contrib import admin
from django.utils import timezone
from django.utils.html import mark_safe

from .models import PatientVisit, PatientVisitAccess

logger = logging.getLogger(__name__)


# ===========================================================================
# FILTERS
# ===========================================================================

class VisitStateFilter(admin.SimpleListFilter):
    """
    Filter visits by their runtime state (active, expired, ended).
    is_active=True covers both still-running and already-expired-but-not-closed.
    This filter distinguishes them for clarity.
    """
    title          = "visit state"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return (
            ("active",  "Active (running)"),
            ("expired", "Expired (time passed)"),
            ("ended",   "Ended (explicitly closed)"),
            ("all",     "All"),
        )

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "active":
            return queryset.filter(is_active=True, expires_at__gt=now)
        if self.value() == "expired":
            return queryset.filter(is_active=True, expires_at__lt=now)
        if self.value() == "ended":
            return queryset.filter(is_active=False, ended_at__isnull=False)
        return queryset

    def choices(self, changelist):
        for lookup, title in self.lookup_choices:
            yield {
                "selected": (
                    self.value() == lookup
                    or (self.value() is None and lookup == "active")
                ),
                "query_string": changelist.get_query_string(
                    {self.parameter_name: lookup}
                ),
                "display": title,
            }


# ===========================================================================
# INLINE: PatientVisitAccess on PatientVisit detail
# ===========================================================================

class PatientVisitAccessInline(admin.TabularInline):
    """
    Shows all lazy-created practitioner access records for this visit.
    These records are created when a practitioner first opens the patient's
    record during an active visit — not at visit creation.

    Fully read-only — lazy access records are system-created.
    """
    model      = PatientVisitAccess
    extra      = 0
    can_delete = False

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("practitioner")
            .order_by("first_accessed_at")
        )

    readonly_fields = (
        "practitioner",
        "first_accessed_at",
        "last_accessed_at",
        "is_active",
        "revoked_at",
        "revocation_reason",
    )

    fields = (
        "practitioner",
        "first_accessed_at",
        "last_accessed_at",
        "is_active",
        "revoked_at",
        "revocation_reason",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ===========================================================================
# ADMIN: PatientVisit
# ===========================================================================

@admin.register(PatientVisit)
class PatientVisitAdmin(admin.ModelAdmin):
    """
    Admin view for PatientVisit records.

    Supports investigation of visit sessions, access disputes,
    and force-ending runaway visits.

    UHR staff can end active visits via the bulk action.
    Ended or expired visits are read-only.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "patient_name",
        "organisation_name",
        "initiated_at",
        "expires_at",
        "ended_at",
        "practitioner_access_count",
        "state_badge",
        "visit_reason",
    )

    list_filter = (
        VisitStateFilter,
    )

    search_fields = (
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "organisation__name",
    )

    ordering      = ("-initiated_at",)
    list_per_page = 50

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "id",
        "patient",
        "organisation",
        "initiated_by",
        "initiated_at",
        "expires_at",
        "ended_at",
        "ended_by",
        "is_active",
        "visit_reason",
        "state_badge",
        "practitioner_access_count",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Visit", {
            "fields": (
                "id",
                "patient",
                "organisation",
                "initiated_by",
                "visit_reason",
                "state_badge",
            ),
        }),
        ("Timing", {
            "fields": (
                "initiated_at",
                "expires_at",
                "ended_at",
                "ended_by",
            ),
        }),
        ("Access Summary", {
            "fields": (
                "practitioner_access_count",
            ),
            "description": (
                "Access records below show each practitioner who accessed "
                "this patient during the visit. Records are created lazily "
                "on first access — not at visit initiation."
            ),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    inlines = [PatientVisitAccessInline]
    actions = ["action_end_visits"]

    # ── Actions ───────────────────────────────────────────────────────────────
    @admin.action(description="⊘ End selected visits (admin force-close)")
    def action_end_visits(self, request, queryset):
        now    = timezone.now()
        active = queryset.filter(is_active=True)
        count  = active.count()

        for visit in active:
            visit.is_active = False
            visit.ended_at  = now
            visit.ended_by  = request.user
            visit.save(update_fields=["is_active", "ended_at", "ended_by", "updated_at"])

            # Deactivate all practitioner access records from this visit
            PatientVisitAccess.objects.filter(
                visit=visit,
                is_active=True,
            ).update(is_active=False, revoked_at=now)

        already_ended = queryset.filter(is_active=False).count()
        msg = f"{count} visit(s) ended."
        if already_ended:
            msg += f" {already_ended} were already ended and skipped."
        self.message_user(request, msg)

        logger.info(
            "Admin force-ended %s visit(s). admin=%s", count, request.user.email
        )

    # ── Permissions ───────────────────────────────────────────────────────────
    def has_add_permission(self, request):
        # Visits are initiated by patients via the API — never manually
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # ── Custom columns ────────────────────────────────────────────────────────
    @admin.display(description="Patient", ordering="patient__last_name")
    def patient_name(self, obj):
        return obj.patient.full_name

    @admin.display(description="Organisation", ordering="organisation__name")
    def organisation_name(self, obj):
        return obj.organisation.name

    @admin.display(description="Practitioners accessed")
    def practitioner_access_count(self, obj):
        total  = obj.practitioner_accesses.count()
        active = obj.practitioner_accesses.filter(is_active=True).count()
        return f"{total} total ({active} active)"

    @admin.display(description="State")
    def state_badge(self, obj):
        now = timezone.now()
        if not obj.is_active and obj.ended_at:
            return mark_safe(
                '<span style="color:#7f8c8d;font-weight:bold;">⊘ Ended</span>'
            )
        if obj.expires_at < now:
            return mark_safe(
                '<span style="color:#e67e22;">⏱ Expired</span>'
            )
        return mark_safe(
            '<span style="color:#27ae60;">● Active</span>'
        )


# ===========================================================================
# ADMIN: PatientVisitAccess (read-only audit view)
# ===========================================================================

@admin.register(PatientVisitAccess)
class PatientVisitAccessAdmin(admin.ModelAdmin):
    """
    Read-only audit view for PatientVisitAccess records.

    These records are lazily created when a practitioner first opens
    a patient's record during an active visit. Used by support to
    investigate who accessed which patient during a visit.
    """

    list_display = (
        "patient_name",
        "practitioner_name",
        "organisation_name",
        "first_accessed_at",
        "last_accessed_at",
        "is_active",
        "revoked_at",
    )

    list_filter = (
        "is_active",
    )

    search_fields = (
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "practitioner__full_name",
        "visit__organisation__name",
    )

    ordering      = ("-first_accessed_at",)
    list_per_page = 100

    readonly_fields = (
        "id",
        "visit",
        "patient",
        "practitioner",
        "first_accessed_at",
        "last_accessed_at",
        "is_active",
        "revoked_at",
        "revoked_by",
        "revocation_reason",
    )

    fieldsets = (
        ("Access Record", {
            "fields": (
                "id",
                "visit",
                "patient",
                "practitioner",
            ),
        }),
        ("Timing", {
            "fields": (
                "first_accessed_at",
                "last_accessed_at",
            ),
        }),
        ("State", {
            "fields": (
                "is_active",
                "revoked_at",
                "revoked_by",
                "revocation_reason",
            ),
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

    @admin.display(description="Patient", ordering="patient__last_name")
    def patient_name(self, obj):
        return obj.patient.full_name

    @admin.display(description="Practitioner", ordering="practitioner__full_name")
    def practitioner_name(self, obj):
        return obj.practitioner.full_name

    @admin.display(description="Organisation")
    def organisation_name(self, obj):
        return obj.visit.organisation.name