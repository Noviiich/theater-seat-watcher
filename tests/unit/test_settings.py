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


def test_settings_parse_numeric_telegram_allowlist() -> None:
    settings = Settings.from_environ({"ALLOWED_TELEGRAM_USER_IDS": "12, 34,12"})

    assert settings.allowed_telegram_user_ids == frozenset({"12", "34"})


def test_settings_reject_invalid_telegram_allowlist() -> None:
    with pytest.raises(ValueError, match="ALLOWED_TELEGRAM_USER_IDS"):
        Settings.from_environ({"ALLOWED_TELEGRAM_USER_IDS": "12,not-an-id"})


def test_live_mode_requires_private_buyer_profile_path() -> None:
    with pytest.raises(ValueError, match="BUYER_PROFILE_PATH"):
        Settings.from_environ({"BOOKING_MODE": "live"})

    settings = Settings.from_environ(
        {"BOOKING_MODE": "live", "BUYER_PROFILE_PATH": "/private/buyer.json"}
    )

    assert settings.buyer_profile_path is not None
    assert "buyer.json" not in repr(settings)
