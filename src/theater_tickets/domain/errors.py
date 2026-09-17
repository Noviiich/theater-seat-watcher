"""Domain failures that require no knowledge of adapters."""


class DomainValidationError(ValueError):
    """Raised when a value violates an invariant of the ticket domain."""


class ContractChangedError(RuntimeError):
    """Raised when a provider response cannot be safely interpreted."""


class RequiresUserActionError(RuntimeError):
    """Raised when automatic checkout must stop for a user action."""
