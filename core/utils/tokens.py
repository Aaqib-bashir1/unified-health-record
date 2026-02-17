from django.core.signing import TimestampSigner, BadSignature, SignatureExpired

signer = TimestampSigner()


def generate_signed_token(payload: str) -> str:
    """
    Generate signed token with timestamp.
    """
    return signer.sign(payload)


def verify_signed_token(token: str, max_age: int = 3600) -> str:
    """
    Verify signed token and return original payload.
    max_age in seconds (default 1 hour).
    """
    try:
        return signer.unsign(token, max_age=max_age)
    except SignatureExpired:
        raise ValueError("Token has expired.")
    except BadSignature:
        raise ValueError("Invalid token.")
