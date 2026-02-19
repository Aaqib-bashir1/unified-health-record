"""
patients/exceptions.py
======================
Domain exceptions for the patients app.

Pattern mirrors users/exceptions.py (AuthenticationError).
Service layer raises these. API layer catches and maps to HTTP responses.
Never let Django or Python built-in exceptions leak through to the API layer.
"""


class PatientError(Exception):
    """
    Base exception for all patients app errors.
    Carry a human-readable message and an optional field name
    so the API layer can produce consistent ErrorSchema responses.
    """
    def __init__(self, message: str, field: str = None, code: str = None):
        self.message = message
        self.field   = field      # Which field caused the error (for form-level feedback)
        self.code    = code       # Machine-readable error code for clients
        super().__init__(message)


class PatientNotFound(PatientError):
    """Patient profile does not exist or the requesting user has no access to it."""
    def __init__(self, message: str = "Patient profile not found."):
        super().__init__(message, code="patient_not_found")


class AccessDenied(PatientError):
    """
    The requesting user does not have sufficient role to perform this action.
    Distinct from PatientNotFound — used when the profile exists but the
    user's role is insufficient (e.g. viewer trying to write).
    """
    def __init__(self, message: str = "You do not have permission to perform this action."):
        super().__init__(message, code="access_denied")


class ProfileAlreadyClaimed(PatientError):
    """
    The profile has already been claimed by a primary holder.
    Used when a second user attempts to initiate a primary claim.
    """
    def __init__(self, message: str = "This profile has already been claimed by its patient."):
        super().__init__(message, code="profile_already_claimed")


class OrphanProtectionError(PatientError):
    """
    A revocation or transfer was blocked because it would leave the
    patient profile without any active delegate or primary holder.
    """
    def __init__(self, message: str = "Cannot revoke access: profile must always have at least one active holder."):
        super().__init__(message, code="orphan_protection")


class DuplicateAccessError(PatientError):
    """
    The target user already has active access to this patient profile.
    """
    def __init__(self, message: str = "This user already has active access to the profile."):
        super().__init__(message, code="duplicate_access")


class PatientRetracted(PatientError):
    """
    The patient profile has been soft-deleted and cannot be accessed or modified.
    """
    def __init__(self, message: str = "This patient profile has been retracted."):
        super().__init__(message, code="patient_retracted")