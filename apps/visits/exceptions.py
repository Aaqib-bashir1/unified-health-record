"""visits/exceptions.py — domain exceptions for the visits app."""


class VisitError(Exception):
    def __init__(self, message: str = "Visit error.", code: str = "visit_error"):
        self.message = message
        self.code    = code
        super().__init__(message)


class VisitNotFound(VisitError):
    def __init__(self, message: str = "Visit not found."):
        super().__init__(message, code="visit_not_found")


class VisitAlreadyEnded(VisitError):
    def __init__(self, message: str = "This visit has already ended."):
        super().__init__(message, code="visit_already_ended")


class VisitAlreadyActive(VisitError):
    """Raised when patient tries to initiate a duplicate visit at the same org."""
    def __init__(self, message: str = "You already have an active visit at this organisation."):
        super().__init__(message, code="visit_already_active")


class InvalidOrgQRToken(VisitError):
    def __init__(self, message: str = "Invalid or expired organisation QR code."):
        super().__init__(message, code="invalid_org_qr")


class OrganisationNotFound(VisitError):
    def __init__(self, message: str = "Organisation not found or not verified."):
        super().__init__(message, code="organisation_not_found")


class PractitionerNotAtOrg(VisitError):
    """Raised when a practitioner tries to access a patient outside their org."""
    def __init__(self, message: str = "No active visit session found for this patient at your organisation."):
        super().__init__(message, code="practitioner_not_at_org")


class TimelineRequestNotFound(VisitError):
    def __init__(self, message: str = "Timeline request not found."):
        super().__init__(message, code="timeline_request_not_found")


class TimelineRequestNotPending(VisitError):
    def __init__(self, message: str = "This request is no longer pending."):
        super().__init__(message, code="timeline_request_not_pending")


class TimelineRequestAlreadyExists(VisitError):
    def __init__(self, message: str = "A pending timeline request already exists for this visit."):
        super().__init__(message, code="timeline_request_already_exists")


class FullTimelineNotApproved(VisitError):
    def __init__(self, message: str = "Full timeline access has not been approved for this visit."):
        super().__init__(message, code="full_timeline_not_approved")