import logging
from http import HTTPStatus
from ninja import Router
from django.core.exceptions import ValidationError

from .schemas import (
    RegistrationSchema,
    LoginSchema,
    LoginResponseSchema,
    RefreshSchema,
    ActivationSchema,
    ResendActivationSchema,
    TokenResponseSchema,
    UserResponseSchema,
    ErrorSchema,
    ForgotPasswordSchema,
    ResetPasswordSchema,
)

from .services import (
    register_user,
    login_user,
    activate_user,
    resend_activation_email,
    forgot_password,
    reset_password,
)

from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)
router = Router(tags=["Users"])


def error_response(status: HTTPStatus, message: str):
    return status, {
        "detail": message,
        "status_code": int(status),
    }


# =========================
# Register
# =========================

@router.post(
    "/register",
    response={
        HTTPStatus.CREATED: UserResponseSchema,
        HTTPStatus.BAD_REQUEST: ErrorSchema,
        HTTPStatus.INTERNAL_SERVER_ERROR: ErrorSchema,
    },
)
def register(request, body: RegistrationSchema):
    try:
        user = register_user(body)
        return HTTPStatus.CREATED, user

    except ValidationError as e:
        return error_response(HTTPStatus.BAD_REQUEST, str(e))

    except Exception:
        logger.exception("Unexpected registration error")
        return error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Internal server error.",
        )


# =========================
# Login
# =========================

@router.post(
    "/login",
    response={
        HTTPStatus.OK: LoginResponseSchema,
        HTTPStatus.UNAUTHORIZED: ErrorSchema,
        HTTPStatus.BAD_REQUEST: ErrorSchema,
        HTTPStatus.INTERNAL_SERVER_ERROR: ErrorSchema,
    },
)
def login(request, body: LoginSchema):
    try:
        result = login_user(body.email, body.password)
        return HTTPStatus.OK, result

    except AuthenticationError as e:
        return error_response(HTTPStatus.UNAUTHORIZED, str(e))

    except ValidationError as e:
        return error_response(HTTPStatus.BAD_REQUEST, str(e))

    except Exception:
        logger.exception("Unexpected login error")
        return error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Internal server error.",
        )


# =========================
# Refresh
# =========================

@router.post(
    "/refresh",
    response={
        HTTPStatus.OK: TokenResponseSchema,
        HTTPStatus.BAD_REQUEST: ErrorSchema,
    },
)
def refresh_token(request, body: RefreshSchema):
    from rest_framework_simplejwt.tokens import RefreshToken
    from rest_framework_simplejwt.exceptions import TokenError

    try:
        refresh = RefreshToken(body.refresh)
        return HTTPStatus.OK, {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "token_type": "Bearer",
        }

    except TokenError:
        return error_response(
            HTTPStatus.BAD_REQUEST,
            "Invalid or expired refresh token.",
        )


# =========================
# Activate
# =========================

@router.post(
    "/activate",
    response={HTTPStatus.OK: dict, HTTPStatus.BAD_REQUEST: ErrorSchema},
)
def activate(request, body: ActivationSchema):
    try:
        activate_user(body.token)
        return HTTPStatus.OK, {"detail": "Account activated successfully."}

    except ValidationError as e:
        return error_response(HTTPStatus.BAD_REQUEST, str(e))


# =========================
# Resend Activation
# =========================

@router.post("/resend-activation", response={HTTPStatus.OK: dict})
def resend_activation(request, body: ResendActivationSchema):
    resend_activation_email(body.email)
    return HTTPStatus.OK, {
        "detail": "If the account exists, activation email has been sent."
    }


# =========================
# Forgot Password
# =========================

@router.post("/forgot-password", response={HTTPStatus.OK: dict})
def forgot_password_view(request, body: ForgotPasswordSchema):
    forgot_password(body.email)
    return HTTPStatus.OK, {
        "detail": "If the account exists, a password reset link has been sent."
    }


# =========================
# Reset Password
# =========================

@router.post(
    "/reset-password",
    response={HTTPStatus.OK: dict, HTTPStatus.BAD_REQUEST: ErrorSchema},
)
def reset_password_view(request, body: ResetPasswordSchema):
    try:
        reset_password(body.token, body.new_password)
        return HTTPStatus.OK, {"detail": "Password reset successful."}

    except ValidationError as e:
        return error_response(HTTPStatus.BAD_REQUEST, str(e))
