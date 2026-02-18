from django.conf import settings
from core.utils.email import send_email


def send_activation_email(user,raw_token: str):
    """
    Send activation email to user.
    """

    # Include both id + email in payload
    # payload = f"{user.id}:{user.email}"
    # token = generate_signed_tokn(payload)

    activation_link = f"{settings.FRONTEND_URL}/activate?token={raw_token}"

    subject = "Activate your UHR account"

    message = f"""
Hello {user.get_full_name()},

Please activate your account:

{activation_link}

This link expires in 1 hour.
"""

    html_message = f"""
    <html>
        <body>
            <h2>Activate Your Account</h2>
            <p>Hello {user.get_full_name()},</p>
            <p>Please click below to activate:</p>
            <p>
                <a href="{activation_link}">
                    Activate Account
                </a>
            </p>
            <p>This link expires in 1 hour.</p>
        </body>
    </html>
    """

    send_email(
        subject=subject,
        message=message,
        recipient_list=[user.email],
        html_message=html_message,
    )

def send_password_reset_email(user, reset_link: str):
    """
    Send password reset email to user.
    """

    subject = "Reset your UHR password"

    message = f"""
Hello {user.get_full_name()},

We received a request to reset your password.

You can reset it using the link below:

{reset_link}

This link expires in 1 hour.

If you did not request this reset, you can safely ignore this email.

— UHR Team
"""

    html_message = f"""
    <html>
        <body>
            <h2>Password Reset Request</h2>
            <p>Hello {user.get_full_name()},</p>
            <p>We received a request to reset your password.</p>
            <p>
                <a href="{reset_link}">
                    Reset Password
                </a>
            </p>
            <p>This link expires in 1 hour.</p>
            <p>If you did not request this reset, you can ignore this email.</p>
        </body>
    </html>
    """

    send_email(
        subject=subject,
        message=message,
        recipient_list=[user.email],
        html_message=html_message,
    )
