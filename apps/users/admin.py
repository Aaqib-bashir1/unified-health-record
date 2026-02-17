from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from .models import User,UserToken


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["-created_at"]
    list_display = (
        "email",
        "first_name",
        "last_name",
        "is_active",
        "is_verified",
        "is_staff",
        "created_at",
    )
    list_filter = (
        "is_active",
        "is_verified",
        "is_staff",
        "is_superuser",
        "is_deleted",
    )
    search_fields = ("email", "first_name", "last_name", "mobile_number")

    readonly_fields = ("id", "created_at", "deleted_at", "last_login")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (
            _("Personal Info"),
            {"fields": ("first_name", "last_name", "mobile_number")},
        ),
        (
            _("Account Status"),
            {
                "fields": (
                    "is_active",
                    "is_verified",
                    "is_staff",
                    "is_superuser",
                    "is_deleted",
                    "deleted_at",
                )
            },
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            _("Important Dates"),
            {"fields": ("last_login", "created_at")},
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "password1",
                    "password2",
                    "is_staff",
                    "is_active",
                ),
            },
        ),
    )

    filter_horizontal = ("groups", "user_permissions")

@admin.register(UserToken)
class UserTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "token_type", "expires_at", "created_at")
    list_filter = ("token_type", "expires_at", "created_at")
    search_fields = ("user__email",)
    readonly_fields = ("id", "created_at")  
    