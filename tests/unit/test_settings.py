from __future__ import annotations

import pytest

from theater_tickets.settings import BookingMode, Settings


def test_settings_default_to_dry_run_and_twenty_minute_hold_intervals() -> None:
    settings = Settings.from_environ({})

    assert settings.booking_mode is BookingMode.DRY_RUN
    assert settings.poll_interval_seconds == 60
    assert settings.poll_jitter_seconds == 10
    assert settings.worker_interval_seconds == 5
    assert settings.runtime_max_backoff_seconds == 300
    assert settings.runtime_lock_lease_seconds == 30
    assert settings.shutdown_grace_seconds == 30
    assert settings.renewal_interval_seconds == 1200
    assert settings.expected_hold_ttl_seconds == 1200
    assert settings.availability_retry_seconds == 180
    assert settings.theatre_alias == "orel-teatr-svobodnoe-prostranstvo"
    assert settings.hall_profiles_path.as_posix() == "config/halls"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POLL_INTERVAL_SECONDS", "0"),
        ("RENEWAL_INTERVAL_SECONDS", "-1"),
        ("EXPECTED_HOLD_TTL_SECONDS", "invalid"),
        ("WORKER_INTERVAL_SECONDS", "0"),
        ("RUNTIME_LOCK_LEASE_SECONDS", "-1"),
    ],
)
def test_settings_reject_non_positive_intervals(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        Settings.from_environ({name: value})


def test_settings_accept_zero_poll_jitter_but_reject_negative_value() -> None:
    assert Settings.from_environ({"POLL_JITTER_SECONDS": "0"}).poll_jitter_seconds == 0
    with pytest.raises(ValueError, match="POLL_JITTER_SECONDS"):
        Settings.from_environ({"POLL_JITTER_SECONDS": "-1"})


def test_settings_do_not_retain_secret_values() -> None:
    settings = Settings.from_environ(
        {
            "TELEGRAM_BOT_TOKEN": "a-secret-token",
            "ADMIN_TELEGRAM_USER_ID": "12345",
            "DATABASE_URL": "sqlite:///secret.db",
        }
    )

    assert settings.telegram_token_configured is True
    assert settings.administrator_configured is True
    assert settings.database_url_configured is True
    assert "a-secret-token" not in repr(settings)
    assert "secret.db" not in repr(settings)


def test_settings_parse_numeric_telegram_administrator() -> None:
    settings = Settings.from_environ({"ADMIN_TELEGRAM_USER_ID": "12"})

    assert settings.administrator_telegram_user_id == "12"


def test_settings_reject_invalid_telegram_administrator() -> None:
    with pytest.raises(ValueError, match="ADMIN_TELEGRAM_USER_ID"):
        Settings.from_environ({"ADMIN_TELEGRAM_USER_ID": "not-an-id"})


def test_live_mode_does_not_require_a_buyer_profile_file() -> None:
    assert Settings.from_environ({"BOOKING_MODE": "live"}).booking_mode is BookingMode.LIVE


def test_runtime_requires_private_infrastructure_and_live_payment_terminal() -> None:
    complete = {
        "TELEGRAM_BOT_TOKEN": "123456:valid-looking-token",
        "ADMIN_TELEGRAM_USER_ID": "10",
        "DATABASE_URL": "sqlite+aiosqlite:///data/app.sqlite3",
    }
    Settings.from_environ(complete).validate_runtime()

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_environ({}).validate_runtime()
    with pytest.raises(ValueError, match=r"sqlite\+aiosqlite"):
        Settings.from_environ({**complete, "DATABASE_URL": "postgresql://db"}).validate_runtime()
    with pytest.raises(ValueError, match="QUICKTICKETS_PAYMENT_TERMINAL_CHOICE"):
        Settings.from_environ(
            {
                **complete,
                "BOOKING_MODE": "live",
            }
        ).validate_runtime()
    Settings.from_environ(
        {
            **complete,
            "BOOKING_MODE": "live",
            "QUICKTICKETS_PAYMENT_TERMINAL_CHOICE": "bank-sber-2:sbp",
        }
    ).validate_runtime()
