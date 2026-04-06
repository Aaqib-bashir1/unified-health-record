"""share/exceptions.py — domain exceptions for the share app."""


class ShareError(Exception):
    def __init__(self, message: str = "Share error.", code: str = "share_error"):
        self.message   = message
        self.code      = code
        super().__init__(message)


class ShareLinkNotFound(ShareError):
    def __init__(self, message: str = "Share link not found or expired."):
        super().__init__(message, code="share_link_not_found")


class ShareLinkRevoked(ShareError):
    def __init__(self, message: str = "This share link has been revoked."):
        super().__init__(message, code="share_link_revoked")


class ShareLinkExpired(ShareError):
    def __init__(self, message: str = "This share link has expired."):
        super().__init__(message, code="share_link_expired")


class InvalidValidator(ShareError):
    """Raised when the DOB or PIN challenge fails."""
    def __init__(self, message: str = "Incorrect date of birth or PIN."):
        super().__init__(message, code="invalid_validator")


class SessionNotFound(ShareError):
    def __init__(self, message: str = "Session not found, expired, or revoked."):
        super().__init__(message, code="session_not_found")


class ShareLinkAccessDenied(ShareError):
    """Raised when the caller lacks permission to manage a share link."""
    def __init__(self, message: str = "You do not have permission to manage this share link."):
        super().__init__(message, code="share_access_denied")