class UserDomainError(Exception):
    """Base exception for user domain errors."""
    pass


class AuthenticationError(UserDomainError):
    pass


class ActivationError(UserDomainError):
    pass


class AccountDeactivatedError(UserDomainError):
    pass