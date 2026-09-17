from __future__ import annotations

import pytest

from theater_tickets.settings import BookingMode, Settings


def test_settings_default_to_dry_run_and_twenty_minute_hold_intervals() -> None:
    settings = Settings.from_environ({})

    assert settings.booking_mode is BookingMode.DRY_RUN
    assert settings.poll_interval_seconds == 60
    assert settings.renewal_interval_seconds == 1200
    assert settings.expected_hold_ttl_seconds == 1200
    assert settings.availability_retry_seconds == 180


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POLL_INTERVAL_SECONDS", "0"),
        ("RENEWAL_INTERVAL_SECONDS", "-1"),
        ("EXPECTED_HOLD_TTL_SECONDS", "invalid"),
    ],
)
def test_settings_reject_non_positive_intervals(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        Settings.from_environ({name: value})


def test_settings_do_not_retain_secret_values() -> None:
    settings = Settings.from_environ(
        {
            "TELEGRAM_BOT_TOKEN": "a-secret-token",
            "ALLOWED_TELEGRAM_USER_IDS": "12345",
            "DATABASE_URL": "sqlite:///secret.db",
        }
    )

    assert settings.telegram_token_configured is True
    assert settings.allowed_user_ids_configured is True
    assert settings.database_url_configured is True
    assert "a-secret-token" not in repr(settings)
