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

import logging

from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html, mark_safe

logger = logging.getLogger(__name__)

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
            return mark_safe('<span style="color:#c0392b;font-weight:bold;">⊘ Retracted</span>')
        if obj.is_deceased:
            return mark_safe('<span style="color:#7f8c8d;">† Deceased</span>')
        return mark_safe('<span style="color:#27ae60;">● Active</span>')

    @admin.display(description="Access holders")
    def access_holder_count(self, obj):
        active = obj.user_accesses.filter(is_active=True).count()
        total  = obj.user_accesses.count()
        return f"{active} active / {total} total"

    # ── Merge action and custom URL ───────────────────────────────────────────

    actions = ["action_merge_patient"]

    @admin.action(description="⚠️ Merge into another patient profile")
    def action_merge_patient(self, request, queryset):
        """
        List action: redirect to the merge confirmation page.
        Exactly one patient must be selected — merging is always one-to-one.
        """
        if queryset.count() != 1:
            self.message_user(
                request,
                f"Select exactly one patient profile to merge. "
                f"You selected {queryset.count()}.",
                level=messages.ERROR,
            )
            return
        selected = queryset.first()
        return HttpResponseRedirect(
            reverse("admin:patients_patient_merge", args=[selected.pk])
        )

    def get_urls(self):
        """
        Register the merge confirmation page URL.
        Defined inside the class — no monkey-patching required.
        """
        custom_urls = [
            path(
                "<uuid:patient_id>/merge/",
                self.admin_site.admin_view(PatientMergeView.as_view()),
                name="patients_patient_merge",
            ),
        ]
        return custom_urls + super().get_urls()


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


# ===========================================================================
# MERGE ADMIN — URL, VIEW, FORM, AND ACTION
# ===========================================================================
# Wiring:
#   PatientAdmin.get_urls()     → registers /admin/patients/patient/merge/
#   PatientAdmin.merge_action   → list action that redirects to merge view
#   MergeConfirmView            → handles GET (confirmation page) and POST (execute)
#
# Two-step flow:
#   1. Admin selects ONE source patient from the list
#   2. Clicks "Merge into another patient" action → redirected to confirmation page
#   3. Confirmation page shows source info, provides target selector
#   4. Admin reviews and submits → merge executes atomically
#   5. Success → redirect back to patient list with success message
# ===========================================================================

from django import forms
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path
from django.utils.decorators import method_decorator
from django.views import View

from . import services


class MergePatientForm(forms.Form):
    """
    Form for the merge confirmation page.
    Admin picks the second profile — service auto-determines source/target
    by created_at (older = target/survives, newer = source/retracted).
    """
    other_patient = forms.ModelChoiceField(
        queryset=Patient.objects.filter(deleted_at__isnull=True),
        label="Merge with this patient",
        help_text=(
            "The earlier-created profile will be kept as the canonical record. "
            "The later-created profile will be retracted. "
            "All medical events will be reassigned to the surviving profile."
        ),
        widget=forms.Select(attrs={"style": "width: 400px;"}),
    )

    reason = forms.CharField(
        label="Reason for merge",
        min_length=20,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 4, "style": "width: 400px;"}),
        help_text=(
            "Required. Explain why these profiles are duplicates. "
            "Written permanently into the audit log and retraction reason."
        ),
    )

    def __init__(self, *args, selected_patient=None, **kwargs):
        super().__init__(*args, **kwargs)
        if selected_patient:
            self.fields["other_patient"].queryset = Patient.objects.filter(
                deleted_at__isnull=True,
            ).exclude(pk=selected_patient.pk)


@method_decorator(staff_member_required, name="dispatch")
class PatientMergeView(View):
    """
    Two-step merge confirmation view.

    GET  → show selected patient + form to pick the other patient
    POST → call merge_patients(), which auto-determines source/target by created_at

    Permission: patients.merge_patient OR superuser.
    Django superusers pass has_perm() automatically without explicit grant.
    Template: patients/templates/admin/patients/patient/merge_confirm.html
              Auto-discovered when APP_DIRS=True in settings.
    """
    template_name = "admin/patients/patient/merge_confirm.html"

    def _check_permission(self, request):
        if not request.user.has_perm("patients.merge_patient"):
            messages.error(
                request,
                "You do not have permission to merge patient profiles. "
                "A superuser can grant you the 'patients.merge_patient' permission."
            )
            return False
        return True

    def get(self, request, patient_id):
        if not self._check_permission(request):
            return HttpResponseRedirect("../")

        patient = get_object_or_404(Patient, pk=patient_id, deleted_at__isnull=True)
        form    = MergePatientForm(selected_patient=patient)

        return render(request, self.template_name, {
            "title":          f"Merge Patient: {patient.full_name}",
            "patient":        patient,
            "form":           form,
            "access_records": patient.user_accesses.filter(is_active=True).select_related("user"),
            "opts":           Patient._meta,
            "has_permission": True,
        })

    def post(self, request, patient_id):
        if not self._check_permission(request):
            return HttpResponseRedirect("../")

        patient = get_object_or_404(Patient, pk=patient_id, deleted_at__isnull=True)
        form    = MergePatientForm(request.POST, selected_patient=patient)

        if not form.is_valid():
            return render(request, self.template_name, {
                "title":          f"Merge Patient: {patient.full_name}",
                "patient":        patient,
                "form":           form,
                "access_records": patient.user_accesses.filter(is_active=True).select_related("user"),
                "opts":           Patient._meta,
                "has_permission": True,
            })

        other  = form.cleaned_data["other_patient"]
        reason = form.cleaned_data["reason"]

        try:
            surviving = services.merge_patients(
                admin_user=request.user,
                patient_a_id=patient.pk,
                patient_b_id=other.pk,
                reason=reason,
            )
            messages.success(
                request,
                f"Merge complete. The earlier-created profile '{surviving.full_name}' "
                f"(MRN: {surviving.mrn}) is the surviving record. "
                f"The later-created profile has been retracted."
            )
            return HttpResponseRedirect(
                reverse("admin:patients_patient_change", args=[surviving.pk])
            )

        except PermissionError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Merge failed: {e}")
            logger.exception(
                "Admin merge failed. patient=%s other=%s admin=%s",
                patient_id, other.pk, request.user.email,
            )

        return HttpResponseRedirect("../")