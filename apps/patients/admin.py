"""
patients/admin.py
=================
Django admin for the patients app.

Design rules for a healthcare admin:
  - No hard deletes exposed anywhere — models are soft-delete only
  - Retracted profiles visible but excluded from default list view
  - PatientUserAccess shown as inline on Patient — full picture on one page
  - Immutable / system-set fields are always read-only
  - Admin cannot manually assign role=primary — claim system owns that
  - Revocations must go through the service layer, not admin
  - Delete action removed from every model
  - Sensitive fields (email, phone) not in search_fields to avoid PII in logs

Class order matters — filters must be defined before they are referenced
in list_filter on the ModelAdmin classes.
"""

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import AccessRole, Patient, PatientUserAccess


# ===========================================================================
# CUSTOM FILTERS
# Defined before PatientAdmin which references them in list_filter.
# ===========================================================================

class IsRetractedFilter(admin.SimpleListFilter):
    """
    Filter patients by retraction state.

    Default view shows active-only. Admins must explicitly choose
    "Retracted only" or "All records" to see soft-deleted profiles.
    """
    title          = "retraction status"
    parameter_name = "retracted"

    def lookups(self, request, model_admin):
        return (
            ("no",  "Active only"),
            ("yes", "Retracted only"),
            ("all", "All records"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(deleted_at__isnull=False)
        if self.value() == "all":
            return queryset
        return queryset.filter(deleted_at__isnull=True)

    def choices(self, changelist):
        """Make 'Active only' the visual default when no param is set."""
        for lookup, title in self.lookup_choices:
            yield {
                "selected": (
                    self.value() == lookup
                    or (self.value() is None and lookup == "no")
                ),
                "query_string": changelist.get_query_string(
                    {self.parameter_name: lookup}
                ),
                "display": title,
            }


# ===========================================================================
# INLINE: PatientUserAccess on Patient detail page
# ===========================================================================

class PatientUserAccessInline(admin.TabularInline):
    """
    Shows all access records (active and revoked) for a patient.
    Fully read-only — all writes go through the service layer.
    """
    model      = PatientUserAccess
    extra      = 0
    can_delete = False

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("user", "granted_by", "revoked_by")
            .order_by("-granted_at")
        )

    readonly_fields = (
        "user",
        "role",
        "is_active",
        "claim_method",
        "trust_level",
        "granted_by",
        "granted_at",
        "revoked_at",
        "revoked_by",
        "revocation_reason",
        "notes",
    )

    fields = (
        "user",
        "role",
        "is_active",
        "claim_method",
        "trust_level",
        "granted_by",
        "granted_at",
        "revoked_at",
        "revoked_by",
        "revocation_reason",
        "notes",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ===========================================================================
# ADMIN: Patient
# ===========================================================================

@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    """
    Admin view for Patient profiles.

    List view: status badges, age, claim state, retraction filter.
    Detail view: grouped fieldsets, access holder count, inline access records.
    No delete at any level.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "mrn",
        "full_name_display",
        "gender",
        "birth_date",
        "age_display",
        "nationality",
        "is_claimed",
        "is_deceased",
        "status_badge",
        "created_at",
    )

    list_filter = (
        "gender",
        "is_claimed",
        "is_deceased",
        "nationality",
        IsRetractedFilter,
    )

    search_fields = (
        "mrn",
        "first_name",
        "last_name",
        # email/phone excluded: Django logs search queries.
        # Searching by contact fields writes patient PII into server logs.
    )

    ordering      = ("last_name", "first_name")
    list_per_page = 50

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "id",
        "mrn",
        "age_display",
        "access_holder_count",
        "is_claimed",
        "claimed_at",
        "transfer_eligible_at",
        "deleted_at",
        "retraction_reason",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Identity", {
            "fields": (
                "id",
                "mrn",
                "age_display",
                "access_holder_count",
            ),
        }),
        ("Demographics", {
            "fields": (
                ("first_name", "last_name"),
                ("gender", "birth_date"),
                ("phone", "email"),
                "address",
                ("blood_group", "nationality"),
            ),
        }),
        ("Deceased", {
            "fields": (("is_deceased", "deceased_date"),),
            "classes": ("collapse",),
            "description": (
                "If is_deceased=True, deceased_date is required. "
                "Enforced at DB level."
            ),
        }),
        ("Claim State", {
            "fields": (
                "is_claimed",
                "claimed_at",
                "transfer_eligible_at",
            ),
            "description": (
                "Managed by the claim system only. "
                "Do not manually set is_claimed."
            ),
        }),
        ("Retraction", {
            "fields": (
                "deleted_at",
                "retraction_reason",
            ),
            "classes": ("collapse",),
            "description": (
                "Profiles are never physically deleted. "
                "Use the service layer to retract — do not set deleted_at here directly."
            ),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    inlines = [PatientUserAccessInline]

    # ── Permissions ───────────────────────────────────────────────────────────

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # ── Custom columns ────────────────────────────────────────────────────────

    @admin.display(description="Name", ordering="last_name")
    def full_name_display(self, obj):
        return obj.full_name

    @admin.display(description="Age")
    def age_display(self, obj):
        if obj.is_deceased:
            age_at_death = obj.age_at_death
            return f"† {age_at_death} yrs" if age_at_death is not None else "† Deceased"
        age = obj.age
        return f"{age} yrs" if age is not None else "—"

    @admin.display(description="Status")
    def status_badge(self, obj):
        if obj.deleted_at:
            return format_html(
                '<span style="color:#c0392b;font-weight:bold;">⊘ Retracted</span>'
            )
        if obj.is_deceased:
            return format_html('<span style="color:#7f8c8d;">† Deceased</span>')
        return format_html('<span style="color:#27ae60;">● Active</span>')

    @admin.display(description="Access holders")
    def access_holder_count(self, obj):
        active = obj.user_accesses.filter(is_active=True).count()
        total  = obj.user_accesses.count()
        return f"{active} active / {total} total"


# ===========================================================================
# ADMIN: PatientUserAccess (read-only audit view)
# ===========================================================================

@admin.register(PatientUserAccess)
class PatientUserAccessAdmin(admin.ModelAdmin):
    """
    Read-only audit view for PatientUserAccess records.

    Used by support for access dispute investigation and audit review.
    100% read-only — no add, change, or delete at any level.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "patient_link",
        "user_email",
        "role",
        "is_active",
        "claim_method",
        "trust_level",
        "granted_at",
        "revoked_at",
        "revocation_summary",
    )

    list_filter = (
        "role",
        "is_active",
        "claim_method",
        "trust_level",
    )

    search_fields = (
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "user__email",
    )

    ordering      = ("-granted_at",)
    list_per_page = 100

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "id",
        "patient",
        "user",
        "role",
        "is_active",
        "claim_method",
        "trust_level",
        "claim_identity",
        "claim_otp",
        "claim_ticket",
        "granted_by",
        "granted_at",
        "revoked_at",
        "revoked_by",
        "revocation_reason",
        "notes",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Access Record", {
            "fields": (
                "id",
                "patient",
                "user",
                "role",
                "is_active",
            ),
        }),
        ("Claim Provenance", {
            "fields": (
                "claim_method",
                "trust_level",
                "claim_identity",
                "claim_otp",
                "claim_ticket",
            ),
            "description": "How this access was established. Immutable after creation.",
        }),
        ("Grant", {
            "fields": (
                "granted_by",
                "granted_at",
            ),
        }),
        ("Revocation", {
            "fields": (
                "revoked_at",
                "revoked_by",
                "revocation_reason",
            ),
            "classes": ("collapse",),
        }),
        ("Notes & Audit", {
            "fields": (
                "notes",
                "created_at",
                "updated_at",
            ),
            "classes": ("collapse",),
        }),
    )

    # ── Permissions: fully read-only ──────────────────────────────────────────

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

    @admin.display(description="Patient", ordering="patient__last_name")
    def patient_link(self, obj):
        """Clickable link to the Patient detail page."""
        url = reverse("admin:patients_patient_change", args=[obj.patient_id])
        return format_html('<a href="{}">{}</a>', url, obj.patient.full_name)

    @admin.display(description="User", ordering="user__email")
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description="Revocation note")
    def revocation_summary(self, obj):
        if obj.is_active:
            return "—"
        if not obj.revocation_reason:
            return "Revoked (no reason recorded)"
        short  = obj.revocation_reason[:50]
        suffix = "…" if len(obj.revocation_reason) > 50 else ""
        return f"{short}{suffix}"