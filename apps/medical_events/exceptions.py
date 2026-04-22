"""medical_events/exceptions.py — domain exceptions for the medical_events app."""


class MedicalEventError(Exception):
    def __init__(self, message: str = "Medical event error.", code: str = "medical_event_error"):
        self.message = message
        self.code    = code
        super().__init__(message)


class EventNotFound(MedicalEventError):
    def __init__(self, message: str = "Medical event not found."):
        super().__init__(message, code="event_not_found")


class EventAccessDenied(MedicalEventError):
    def __init__(self, message: str = "You do not have permission to access this event."):
        super().__init__(message, code="event_access_denied")


class EventImmutable(MedicalEventError):
    """Raised when code attempts to modify an existing event directly."""
    def __init__(self, message: str = "Medical events are immutable. Create an amendment instead."):
        super().__init__(message, code="event_immutable")


class AmendmentReasonRequired(MedicalEventError):
    def __init__(self, message: str = "amendment_reason is required when amending an event."):
        super().__init__(message, code="amendment_reason_required")


class InvalidVerificationLevel(MedicalEventError):
    """Raised when non-practitioner attempts to set provider_verified."""
    def __init__(self, message: str = "Only authenticated practitioners can set provider_verified."):
        super().__init__(message, code="invalid_verification_level")


class DocumentChecksumMismatch(MedicalEventError):
    """Raised when a retrieved document's checksum does not match the stored value."""
    def __init__(self, message: str = "Document integrity check failed. File may have been tampered with."):
        super().__init__(message, code="document_checksum_mismatch")


class DocumentUploadFailed(MedicalEventError):
    def __init__(self, message: str = "Document upload to storage failed."):
        super().__init__(message, code="document_upload_failed")


class InvalidEventType(MedicalEventError):
    def __init__(self, message: str = "Invalid event type."):
        super().__init__(message, code="invalid_event_type")


class EventNotApprovable(MedicalEventError):
    """Raised when trying to approve an event that is not pending_approval."""
    def __init__(self, message: str = "This event is not pending approval."):
        super().__init__(message, code="event_not_approvable")


class MedicationLifecycleError(MedicalEventError):
    """Raised when a lifecycle transition is invalid (e.g. discontinuing an already discontinued med)."""
    def __init__(self, message: str = "Invalid medication lifecycle transition."):
        super().__init__(message, code="medication_lifecycle_error")