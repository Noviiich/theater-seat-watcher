"""Composition root for the initial shell.

Adapters and background workers are deliberately not constructed here yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from theater_tickets.settings import Settings


@dataclass(frozen=True, slots=True)
class Application:
    """A safe application shell with no I/O side effects."""

    settings: Settings

    def status_lines(self) -> tuple[str, ...]:
        """Return operational status without rendering any configured secret."""
        return (
            "theater_tickets: initialized",
            f"booking_mode={self.settings.booking_mode}",
            f"poll_interval_seconds={self.settings.poll_interval_seconds}",
            f"renewal_interval_seconds={self.settings.renewal_interval_seconds}",
            f"expected_hold_ttl_seconds={self.settings.expected_hold_ttl_seconds}",
            f"availability_retry_seconds={self.settings.availability_retry_seconds}",
            f"telegram_token_configured={self.settings.telegram_token_configured}",
            f"allowed_user_ids_configured={self.settings.allowed_user_ids_configured}",
            f"database_url_configured={self.settings.database_url_configured}",
            "network_operations=disabled",
            "ticket_operations=disabled",
        )


def create_application(settings: Settings | None = None) -> Application:
    """Compose the initial shell without starting adapters or workers."""
    return Application(settings=Settings.from_environ() if settings is None else settings)
