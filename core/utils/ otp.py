import secrets
import string
from datetime import timedelta
from django.utils import timezone
from typing import Tuple


def generate_numeric_otp(length: int = 6) -> str:
    if length < 4 or length > 10:
        raise ValueError("OTP length must be between 4 and 10")

    digits = string.digits
    return "".join(secrets.choice(digits) for _ in range(length))


def generate_otp_with_expiry(
    length: int = 6,
    validity_minutes: int = 10
) -> Tuple[str, timezone.datetime]:

    otp = generate_numeric_otp(length)
    expiry = timezone.now() + timedelta(minutes=validity_minutes)

    return otp, expiry
