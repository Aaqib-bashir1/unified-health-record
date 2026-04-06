"""practitioners/exceptions.py — domain exceptions for the practitioners app."""


class PractitionerError(Exception):
    def __init__(self, message: str = "Practitioner error.", code: str = "practitioner_error"):
        self.message = message
        self.code    = code
        super().__init__(message)


class PractitionerNotFound(PractitionerError):
    def __init__(self, message: str = "Practitioner profile not found."):
        super().__init__(message, code="practitioner_not_found")


class PractitionerProfileExists(PractitionerError):
    def __init__(self, message: str = "You already have a practitioner profile."):
        super().__init__(message, code="practitioner_profile_exists")


class MembershipRequestNotFound(PractitionerError):
    def __init__(self, message: str = "Membership request not found."):
        super().__init__(message, code="membership_request_not_found")


class MembershipRequestNotPending(PractitionerError):
    def __init__(self, message: str = "This membership request is no longer pending."):
        super().__init__(message, code="membership_request_not_pending")


class AlreadyMember(PractitionerError):
    def __init__(self, message: str = "This practitioner is already a member of this organisation."):
        super().__init__(message, code="already_member")


class DuplicatePendingMembershipRequest(PractitionerError):
    def __init__(self, message: str = "You already have a pending request for this organisation."):
        super().__init__(message, code="duplicate_pending_membership_request")


class NotOrgAdmin(PractitionerError):
    def __init__(self, message: str = "You must be an org admin to perform this action."):
        super().__init__(message, code="not_org_admin")