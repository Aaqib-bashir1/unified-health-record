from django.contrib.auth import get_user_model, authenticate
from django.core.exceptions import ValidationError
from django.contrib.auth.password_validation import validate_password
from django.db import transaction, IntegrityError
from django.utils import timezone
from django.conf import settings

from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.token_blacklist.models import (
    OutstandingToken,
    BlacklistedToken,
)

from core.services.notifications import (
    send_activation_email,
    send_password_reset_email,
)

from .models import UserToken
from .exceptions import AuthenticationError

User = get_user_model()


# =====================================================
# Registration
# =====================================================

def register_user(data):

    user = User(
        email=data.email,
        first_name=data.first_name,
        last_name=data.last_name,
        mobile_number=data.mobile_number or "",
        is_active=False,
        is_verified=False,
    )

    try:
        validate_password(data.password, user)
    except ValidationError as e:
        raise ValidationError({"password": e.messages})

    user.set_password(data.password)

    try:
        with transaction.atomic():
            user.save()
            raw_token = UserToken.generate_secure_token()

            token = UserToken.objects.create(
            user=user,
            token_hash=UserToken.hash_token(raw_token),
            token_type=UserToken.TokenType.ACTIVATION,
            expires_at=UserToken.default_expiry(hours=1),

             )
            transaction.on_commit(
                lambda: send_activation_email(user,raw_token)
            )

    except IntegrityError:
        raise ValidationError({"email": "Email already registered."})

    return user


# =====================================================
# Login
# =====================================================

def login_user(email: str, password: str):

    user = authenticate(username=email, password=password)

    if not user:
        raise AuthenticationError("Invalid email or password.")

    if user.is_deleted:
        raise AuthenticationError("Account disabled.")

    if not user.is_active:
        raise AuthenticationError("Account not activated.")

    if not user.is_verified:
        raise AuthenticationError("Email not verified.")

    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    refresh = RefreshToken.for_user(user)

    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "token_type": "Bearer",
        "user": user,
    }


# =====================================================
# Activation
# =====================================================

@transaction.atomic
def activate_user(raw_token: str):
    token_hash = UserToken.hash_token(raw_token)

    try:
        token_obj = (
            UserToken.objects.select_for_update()
            .select_related("user")
            .get(
                token_hash=token_hash,
                token_type=UserToken.TokenType.ACTIVATION,
                is_used=False,
                is_revoked=False,
            )
        )
    except UserToken.DoesNotExist:
        raise ValidationError({"detail": "Invalid activation token."})

    if token_obj.expires_at <= timezone.now():
        raise ValidationError({"detail": "Activation token expired."})

    user = token_obj.user
    if not user.is_active or not user.is_verified:
        user.is_active = True
        user.is_verified = True
        user.save(update_fields=["is_active", "is_verified"])

    token_obj.mark_used()




# =====================================================
# Resend Activation
# =====================================================
def resend_activation_email(email: str) -> bool:

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return True  # Prevent enumeration

    if user.is_active:
        return True  # Already activated

    with transaction.atomic():

        # Revoke existing unused activation tokens
        UserToken.objects.filter(
            user=user,
            token_type=UserToken.TokenType.ACTIVATION,
            is_used=False,
            is_revoked=False,
            expires_at__gt=timezone.now(),
        ).update(
            is_revoked=True,
            revoked_at=timezone.now()
        )

        # Create new activation token
        raw_token = UserToken.generate_secure_token()

        token = UserToken.objects.create(
            user=user,
            token_hash=UserToken.hash_token(raw_token),
            token_type=UserToken.TokenType.ACTIVATION,
            expires_at=UserToken.default_expiry(hours=1),
        )

    send_activation_email(user, raw_token)

    return True


# =====================================================
# Forgot Password
# =====================================================



def forgot_password(email: str) -> bool:

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return True  # prevent enumeration
    now = timezone.now()
    with transaction.atomic():
        # Revoke all previous active reset tokens
        UserToken.objects.select_for_update().filter(
            user=user,
            token_type=UserToken.TokenType.PASSWORD_RESET,
            is_used=False,
            is_revoked=False,
            expires_at__gt=now,
        ).update(
            is_revoked=True,
            revoked_at=now,
        )

        raw_token = UserToken.generate_secure_token()

        UserToken.objects.create(
            user=user,
            token_hash=UserToken.hash_token(raw_token),
            token_type=UserToken.TokenType.PASSWORD_RESET,
            expires_at=UserToken.default_expiry(hours=1),
        )

        transaction.on_commit(
            lambda: send_password_reset_email(
                user,
                f"{settings.FRONTEND_URL}/reset-password?token={raw_token}",
            )
        )

    return True

# =====================================================
# Reset Password
# =====================================================


def reset_password(raw_token: str, new_password: str):
    token_hash = UserToken.hash_token(raw_token)

    with transaction.atomic():
        try:
            token = UserToken.objects.select_for_update().select_related("user").get(
                token_hash=token_hash,
                token_type=UserToken.TokenType.PASSWORD_RESET,
            )
        except UserToken.DoesNotExist:
            raise ValidationError({"detail": "Invalid reset token."})

        if not token.is_valid():
            raise ValidationError({"detail": "Token expired or already used."})

        user = token.user

        # Validate password properly
        try:
            validate_password(new_password, user)
        except ValidationError as e:
            raise ValidationError({"password": e.messages})

        # Update password
        user.set_password(new_password)
        user.save(update_fields=["password"])

        # Mark token as used
        token.mark_used()

        # Blacklist all refresh tokens
        for outstanding in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=outstanding)

    return True
