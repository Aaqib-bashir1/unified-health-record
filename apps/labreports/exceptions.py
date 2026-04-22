"""lab_reports/exceptions.py — domain exceptions for the lab_reports app."""


class LabReportError(Exception):
    def __init__(self, message: str = "Lab report error.", code: str = "lab_report_error"):
        self.message = message
        self.code    = code
        super().__init__(message)


class LabReportNotFound(LabReportError):
    def __init__(self, message: str = "Lab report not found."):
        super().__init__(message, code="lab_report_not_found")


class LabReportAccessDenied(LabReportError):
    def __init__(self, message: str = "You do not have access to this lab report."):
        super().__init__(message, code="lab_report_access_denied")


class LabFieldNotFound(LabReportError):
    def __init__(self, message: str = "Lab report field not found."):
        super().__init__(message, code="lab_field_not_found")


class LabFieldAlreadyReviewed(LabReportError):
    def __init__(self, message: str = "This field has already been reviewed."):
        super().__init__(message, code="lab_field_already_reviewed")


class LabReportNotReviewable(LabReportError):
    """Raised when trying to review a report that is not in a reviewable state."""
    def __init__(self, message: str = "This lab report is not in a reviewable state."):
        super().__init__(message, code="lab_report_not_reviewable")


class LabReportAlreadyResulted(LabReportError):
    def __init__(self, message: str = "This lab report has already been resulted."):
        super().__init__(message, code="lab_report_already_resulted")


class ExtractionFailed(LabReportError):
    def __init__(self, message: str = "Failed to extract values from the uploaded report."):
        super().__init__(message, code="extraction_failed")


class IntegrationNotFound(LabReportError):
    def __init__(self, message: str = "Lab integration not found."):
        super().__init__(message, code="integration_not_found")


class IntegrationNotActive(LabReportError):
    def __init__(self, message: str = "This lab integration is not active."):
        super().__init__(message, code="integration_not_active")