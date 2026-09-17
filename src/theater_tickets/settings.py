"""Safe, dependency-free runtime settings for the initial application shell."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from os import environ


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


def _booking_mode(value: str | None) -> BookingMode:
    if value is None or value == "":
        return BookingMode.DRY_RUN
    try:
        return BookingMode(value.lower())
    except ValueError as exc:
        message = "BOOKING_MODE must be one of: dry_run, live"
        raise ValueError(message) from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Settings displayed by the shell without exposing secret values."""

    booking_mode: BookingMode
    poll_interval_seconds: int
    renewal_interval_seconds: int
    expected_hold_ttl_seconds: int
    availability_retry_seconds: int
    telegram_token_configured: bool
    allowed_user_ids_configured: bool
    database_url_configured: bool

    @classmethod
    def from_environ(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Create settings from an environment mapping without loading files."""
        source = environ if env is None else env
        return cls(
            booking_mode=_booking_mode(source.get("BOOKING_MODE")),
            poll_interval_seconds=_positive_int(
                source.get("POLL_INTERVAL_SECONDS"),
                name="POLL_INTERVAL_SECONDS",
                default=60,
            ),
            renewal_interval_seconds=_positive_int(
                source.get("RENEWAL_INTERVAL_SECONDS"),
                name="RENEWAL_INTERVAL_SECONDS",
                default=180,
            ),
            expected_hold_ttl_seconds=_positive_int(
                source.get("EXPECTED_HOLD_TTL_SECONDS"),
                name="EXPECTED_HOLD_TTL_SECONDS",
                default=180,
            ),
            availability_retry_seconds=_positive_int(
                source.get("AVAILABILITY_RETRY_SECONDS"),
                name="AVAILABILITY_RETRY_SECONDS",
                default=180,
            ),
            telegram_token_configured=bool(source.get("TELEGRAM_BOT_TOKEN")),
            allowed_user_ids_configured=bool(source.get("ALLOWED_TELEGRAM_USER_IDS")),
            database_url_configured=bool(source.get("DATABASE_URL")),
        )
