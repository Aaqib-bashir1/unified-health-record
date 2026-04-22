"""clinical/exceptions.py — domain exceptions for the clinical app."""


class ClinicalError(Exception):
    def __init__(self, message: str = "Clinical error.", code: str = "clinical_error"):
        self.message = message
        self.code    = code
        super().__init__(message)


class OrderNotFound(ClinicalError):
    def __init__(self, message: str = "Test order not found."):
        super().__init__(message, code="order_not_found")


class OrderAccessDenied(ClinicalError):
    def __init__(self, message: str = "You do not have permission to access this order."):
        super().__init__(message, code="order_access_denied")


class InvalidOrderTransition(ClinicalError):
    """Raised when a status transition is not valid."""
    def __init__(self, message: str = "This status transition is not allowed."):
        super().__init__(message, code="invalid_order_transition")


class OrderAlreadyResulted(ClinicalError):
    def __init__(self, message: str = "This order has already resulted."):
        super().__init__(message, code="order_already_resulted")


class OrderAlreadyCancelled(ClinicalError):
    def __init__(self, message: str = "This order has already been cancelled."):
        super().__init__(message, code="order_already_cancelled")


class NoPractitionerProfile(ClinicalError):
    """Raised when the user has no verified practitioner profile."""
    def __init__(self, message: str = "You must have a verified practitioner profile to place orders."):
        super().__init__(message, code="no_practitioner_profile")