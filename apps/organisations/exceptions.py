"""organisations/exceptions.py — domain exceptions for the organisations app."""


class OrgError(Exception):
    def __init__(self, message: str = "Organisation error.", code: str = "org_error"):
        self.message = message
        self.code    = code
        super().__init__(message)


class OrgNotFound(OrgError):
    def __init__(self, message: str = "Organisation not found."):
        super().__init__(message, code="org_not_found")


class OrgNotVerified(OrgError):
    def __init__(self, message: str = "This organisation has not been verified yet."):
        super().__init__(message, code="org_not_verified")


class OrgNotActive(OrgError):
    def __init__(self, message: str = "This organisation is not active."):
        super().__init__(message, code="org_not_active")


class OrgAccessDenied(OrgError):
    def __init__(self, message: str = "You do not have permission to perform this action."):
        super().__init__(message, code="org_access_denied")


class OrgAlreadyVerified(OrgError):
    def __init__(self, message: str = "This organisation is already verified."):
        super().__init__(message, code="org_already_verified")