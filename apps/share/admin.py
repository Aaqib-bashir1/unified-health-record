"""
share/admin.py
==============
Django admin for the share app.

Design rules:
  - ShareLink list defaults to active (non-revoked) links
  - ShareLinkSession is read-only — purely an audit view
  - validator_hash is never shown — it is a bcrypt hash of patient DOB/PIN
  - Revoke action available on ShareLink list for UHR staff support
  - No hard deletes on either model
  - Sessions are shown as inline on ShareLink detail
"""

import logging

from django.contrib import admin
from django.utils import timezone
from django.utils.html import mark_safe

from .models import ShareLink, ShareLinkSession

logger = logging.getLogger(__name__)


# ===========================================================================
# FILTERS
# ===========================================================================

class IsRevokedFilter(admin.SimpleListFilter):
    title          = "revocation status"
    parameter_name = "revoked"

    def lookups(self, request, model_admin):
        return (
            ("no",  "Active only"),
            ("yes", "Revoked only"),
            ("all", "All"),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(is_revoked=True)
        if self.value() == "all":
            return queryset
        return queryset.filter(is_revoked=False)

    def choices(self, changelist):
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


class IsExpiredFilter(admin.SimpleListFilter):
    title          = "expiry status"
    parameter_name = "expired"

    def lookups(self, request, model_admin):
        return (
            ("no",  "Not expired"),
            ("yes", "Expired"),
            ("all", "All"),
        )

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == "yes":
            return queryset.filter(expires_at__lt=now)
        if self.value() == "all":
            return queryset
        return queryset.filter(expires_at__gte=now)


# ===========================================================================
# INLINE: ShareLinkSession on ShareLink detail
# ===========================================================================

class ShareLinkSessionInline(admin.TabularInline):
    """
    Shows all sessions created from this share link.
    Read-only — sessions are created by verification, not manually.
    session_token is shown truncated for security — support only needs to
    confirm a session existed, not reproduce the token.
    """
    model      = ShareLinkSession
    extra      = 0
    can_delete = False

    readonly_fields = (
        "session_token_truncated",
        "expires_at",
        "is_revoked",
        "revoked_at",
        "ip_address",
        "created_at",
    )

    fields = (
        "session_token_truncated",
        "expires_at",
        "is_revoked",
        "revoked_at",
        "ip_address",
        "created_at",
    )

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Session token (truncated)")
    def session_token_truncated(self, obj):
        return f"{obj.session_token[:12]}..."


# ===========================================================================
# ADMIN: ShareLink
# ===========================================================================

@admin.register(ShareLink)
class ShareLinkAdmin(admin.ModelAdmin):
    """
    Admin view for ShareLink records.

    validator_hash is never shown — it is a bcrypt hash of patient DOB or PIN.
    Displaying it in admin would be a security information leak even though
    it is hashed — support has no legitimate reason to see it.

    UHR staff can revoke links via the bulk action for abuse/support cases.
    """

    # ── List view ─────────────────────────────────────────────────────────────
    list_display = (
        "token_truncated",
        "patient_name",
        "validator_type",
        "scope",
        "label",
        "expires_at",
        "access_count",
        "first_accessed_at",
        "status_badge",
        "created_at",
    )

    list_filter = (
        IsRevokedFilter,
        IsExpiredFilter,
        "validator_type",
        "scope",
    )

    search_fields = (
        "patient__first_name",
        "patient__last_name",
        "patient__mrn",
        "label",
        # token intentionally excluded — support should use patient lookup
    )

    ordering      = ("-created_at",)
    list_per_page = 50

    # ── Detail view ───────────────────────────────────────────────────────────
    readonly_fields = (
        "id",
        "patient",
        "created_by",
        "token_truncated",
        # validator_hash deliberately omitted — never display bcrypt hashes
        "validator_type",
        "scope",
        "expires_at",
        "is_revoked",
        "revoked_at",
        "first_accessed_at",
        "access_count",
        "label",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        ("Link Identity", {
            "fields": (
                "id",
                "patient",
                "created_by",
                "token_truncated",
                "label",
            ),
        }),
        ("Access Control", {
            "fields": (
                "validator_type",
                # validator_hash intentionally omitted
                "scope",
                "expires_at",
            ),
            "description": (
                "validator_hash is never shown in admin. "
                "It is a bcrypt hash — support has no legitimate need to view it."
            ),
        }),
        ("Usage", {
            "fields": (
                "access_count",
                "first_accessed_at",
            ),
        }),
        ("Revocation", {
            "fields": (
                "is_revoked",
                "revoked_at",
            ),
            "classes": ("collapse",),
        }),
        ("Audit", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    inlines = [ShareLinkSessionInline]
    actions = ["action_revoke_links"]

    # ── Actions ───────────────────────────────────────────────────────────────
    @admin.action(description="⊘ Revoke selected share links (and all their sessions)")
    def action_revoke_links(self, request, queryset):
        now            = timezone.now()
        active         = queryset.filter(is_revoked=False)
        count          = active.count()
        already_revoked = queryset.filter(is_revoked=True).count()

        for link in active:
            link.is_revoked = True
            link.revoked_at = now
            link.save(update_fields=["is_revoked", "revoked_at", "updated_at"])

            # Revoke all active sessions from this link
            ShareLinkSession.objects.filter(
                share_link=link,
                is_revoked=False,
            ).update(is_revoked=True, revoked_at=now)

        msg = f"{count} share link(s) revoked."
        if already_revoked:
            msg += f" {already_revoked} were already revoked and skipped."
        self.message_user(request, msg)

        logger.info(
            "Admin bulk share link revocation. count=%s by admin=%s",
            count, request.user.email,
        )

    # ── Permissions ───────────────────────────────────────────────────────────
    def has_add_permission(self, request):
        # Share links are created by patients via the API — never manually
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    # ── Custom columns ────────────────────────────────────────────────────────
    @admin.display(description="Token")
    def token_truncated(self, obj):
        return f"{obj.token[:12]}..."

    @admin.display(description="Patient", ordering="patient__last_name")
    def patient_name(self, obj):
        return obj.patient.full_name

    @admin.display(description="Status")
    def status_badge(self, obj):
        if obj.is_revoked:
            return mark_safe(
                '<span style="color:#c0392b;font-weight:bold;">⊘ Revoked</span>'
            )
        if obj.expires_at < timezone.now():
            return mark_safe(
                '<span style="color:#7f8c8d;">⏱ Expired</span>'
            )
        return mark_safe(
            '<span style="color:#27ae60;">● Active</span>'
        )


# ===========================================================================
# ADMIN: ShareLinkSession (read-only audit view)
# ===========================================================================

@admin.register(ShareLinkSession)
class ShareLinkSessionAdmin(admin.ModelAdmin):
    """
    Read-only audit view for ShareLinkSession records.
    Used by support to investigate anonymous access to patient timelines.

    session_token shown truncated — full token not needed for support work.
    ip_address and user_agent retained for abuse investigation.
    """

    list_display = (
        "session_token_display",
        "share_link_patient",
        "share_link_label",
        "expires_at",
        "is_revoked",
        "ip_address",
        "created_at",
    )

    list_filter = (
        "is_revoked",
    )

    search_fields = (
        "share_link__patient__first_name",
        "share_link__patient__last_name",
        "share_link__patient__mrn",
        "ip_address",
    )

    ordering      = ("-created_at",)
    list_per_page = 100

    readonly_fields = (
        "id",
        "share_link",
        "session_token_display",
        "expires_at",
        "is_revoked",
        "revoked_at",
        "ip_address",
        "user_agent",
        "created_at",
    )

    fieldsets = (
        ("Session", {
            "fields": (
                "id",
                "share_link",
                "session_token_display",
                "expires_at",
            ),
        }),
        ("State", {
            "fields": (
                "is_revoked",
                "revoked_at",
            ),
        }),
        ("Network", {
            "fields": (
                "ip_address",
                "user_agent",
            ),
            "description": "Stored for abuse investigation. Not used for access control.",
        }),
        ("Audit", {
            "fields": ("created_at",),
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

    @admin.display(description="Session token")
    def session_token_display(self, obj):
        return f"{obj.session_token[:12]}..."

    @admin.display(description="Patient")
    def share_link_patient(self, obj):
        return obj.share_link.patient.full_name

    @admin.display(description="Link label")
    def share_link_label(self, obj):
        return obj.share_link.label or "—"