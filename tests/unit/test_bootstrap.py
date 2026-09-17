from __future__ import annotations

from theater_tickets.bootstrap import create_application
from theater_tickets.settings import Settings


def test_application_status_has_no_network_or_ticket_operations() -> None:
    app = create_application(Settings.from_environ({}))

    assert app.status_lines() == (
        "theater_tickets: initialized",
        "booking_mode=dry_run",
        "poll_interval_seconds=60",
        "renewal_interval_seconds=1200",
        "expected_hold_ttl_seconds=1200",
        "availability_retry_seconds=180",
        "telegram_token_configured=False",
        "allowed_user_ids_configured=False",
        "database_url_configured=False",
        "network_operations=disabled",
        "ticket_operations=disabled",
    )
