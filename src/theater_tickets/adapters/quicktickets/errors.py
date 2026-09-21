"""Provider-specific errors normalized for application code."""


class QuickTicketsError(RuntimeError):
    """Base error for a QuickTickets read operation."""


class QuickTicketsAuthError(QuickTicketsError):
    """The public page token was rejected or the session is forbidden."""


class QuickTicketsContractError(QuickTicketsError):
    """The response does not match the public client contract."""


class QuickTicketsRateLimitError(QuickTicketsError):
    """The provider rate limit remained after bounded retries."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("QuickTickets rate limit remained")
        self.retry_after_seconds = max(1, retry_after_seconds)
