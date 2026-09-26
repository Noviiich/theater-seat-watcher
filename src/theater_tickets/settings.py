"""Safe, dependency-free runtime settings for the initial application shell."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from os import environ
from pathlib import Path


class BookingMode(StrEnum):
    """Modes that govern future ticket-operation adapters."""

    DRY_RUN = "dry_run"
    LIVE = "live"


def _positive_int(value: str | None, *, name: str, default: int) -> int:
    if value is None or value == "":
        return default

    try:
        parsed = int(value)
    except ValueError as exc:
        message = f"{name} must be a positive integer"
        raise ValueError(message) from exc

    if parsed <= 0:
        message = f"{name} must be a positive integer"
        raise ValueError(message)
    return parsed


def _positive_float(value: str | None, *, name: str, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def _non_negative_int(value: str | None, *, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _booking_mode(value: str | None) -> BookingMode:
    if value is None or value == "":
        return BookingMode.DRY_RUN
    try:
        return BookingMode(value.lower())
    except ValueError as exc:
        message = "BOOKING_MODE must be one of: dry_run, live"
        raise ValueError(message) from exc


def _telegram_user_id(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if not normalized.isdecimal():
        raise ValueError("ADMIN_TELEGRAM_USER_ID must be numeric")
    return normalized


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True, slots=True)
class Settings:
    """Settings displayed by the shell without exposing secret values."""

    booking_mode: BookingMode
    poll_interval_seconds: float
    poll_jitter_seconds: int
    worker_interval_seconds: int
    runtime_max_backoff_seconds: int
    runtime_lock_lease_seconds: int
    shutdown_grace_seconds: int
    renewal_interval_seconds: int
    expected_hold_ttl_seconds: int
    availability_retry_seconds: int
    telegram_token_configured: bool
    administrator_configured: bool
    database_url_configured: bool
    telegram_bot_token: str | None = field(default=None, repr=False)
    database_url: str | None = field(default=None, repr=False)
    theatre_alias: str = "orel-teatr-svobodnoe-prostranstvo"
    hall_profiles_path: Path = Path("config/halls")
    administrator_telegram_user_id: str | None = field(default=None, repr=False)
    quicktickets_payment_terminal_choice: str | None = None

    @classmethod
    def from_environ(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Create settings from an environment mapping without loading files."""
        source = environ if env is None else env
        telegram_bot_token = source.get("TELEGRAM_BOT_TOKEN") or None
        database_url = source.get("DATABASE_URL") or None
        administrator_telegram_user_id = _telegram_user_id(source.get("ADMIN_TELEGRAM_USER_ID"))
        payment_terminal_choice = _optional_text(source.get("QUICKTICKETS_PAYMENT_TERMINAL_CHOICE"))
        hall_profiles_path = _path(source.get("HALL_PROFILES_PATH"), default=Path("config/halls"))
        theatre_alias = source.get("THEATRE_ALIAS", "orel-teatr-svobodnoe-prostranstvo").strip()
        if not theatre_alias:
            raise ValueError("THEATRE_ALIAS must not be empty")
        booking_mode = _booking_mode(source.get("BOOKING_MODE"))
        return cls(
            booking_mode=booking_mode,
            poll_interval_seconds=_positive_float(
                source.get("POLL_INTERVAL_SECONDS"),
                name="POLL_INTERVAL_SECONDS",
                default=2.5,
            ),
            poll_jitter_seconds=_non_negative_int(
                source.get("POLL_JITTER_SECONDS"),
                name="POLL_JITTER_SECONDS",
                default=0,
            ),
            worker_interval_seconds=_positive_int(
                source.get("WORKER_INTERVAL_SECONDS"),
                name="WORKER_INTERVAL_SECONDS",
                default=5,
            ),
            runtime_max_backoff_seconds=_positive_int(
                source.get("RUNTIME_MAX_BACKOFF_SECONDS"),
                name="RUNTIME_MAX_BACKOFF_SECONDS",
                default=300,
            ),
            runtime_lock_lease_seconds=_positive_int(
                source.get("RUNTIME_LOCK_LEASE_SECONDS"),
                name="RUNTIME_LOCK_LEASE_SECONDS",
                default=30,
            ),
            shutdown_grace_seconds=_positive_int(
                source.get("SHUTDOWN_GRACE_SECONDS"),
                name="SHUTDOWN_GRACE_SECONDS",
                default=30,
            ),
            renewal_interval_seconds=_positive_int(
                source.get("RENEWAL_INTERVAL_SECONDS"),
                name="RENEWAL_INTERVAL_SECONDS",
                default=1200,
            ),
            expected_hold_ttl_seconds=_positive_int(
                source.get("EXPECTED_HOLD_TTL_SECONDS"),
                name="EXPECTED_HOLD_TTL_SECONDS",
                default=1200,
            ),
            availability_retry_seconds=_positive_int(
                source.get("AVAILABILITY_RETRY_SECONDS"),
                name="AVAILABILITY_RETRY_SECONDS",
                default=180,
            ),
            telegram_token_configured=telegram_bot_token is not None,
            administrator_configured=administrator_telegram_user_id is not None,
            database_url_configured=database_url is not None,
            telegram_bot_token=telegram_bot_token,
            database_url=database_url,
            theatre_alias=theatre_alias,
            hall_profiles_path=hall_profiles_path,
            administrator_telegram_user_id=administrator_telegram_user_id,
            quicktickets_payment_terminal_choice=payment_terminal_choice,
        )

    def validate_runtime(self) -> None:
        """Require concrete infrastructure while keeping all secret values private."""
        if self.telegram_bot_token is None:
            raise ValueError("runtime requires TELEGRAM_BOT_TOKEN")
        if self.administrator_telegram_user_id is None:
            raise ValueError("runtime requires ADMIN_TELEGRAM_USER_ID")
        if self.database_url is None:
            raise ValueError("runtime requires DATABASE_URL")
        if not self.database_url.startswith("sqlite+aiosqlite:///"):
            raise ValueError("runtime DATABASE_URL must use sqlite+aiosqlite with a file path")
        if self.booking_mode is BookingMode.LIVE:
            if self.quicktickets_payment_terminal_choice is None:
                raise ValueError("live runtime requires QUICKTICKETS_PAYMENT_TERMINAL_CHOICE")


def _path(value: str | None, *, default: Path) -> Path:
    if value is None or not value.strip():
        return default
    return Path(value).expanduser()
