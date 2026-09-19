"""Safe, dependency-free runtime settings for the initial application shell."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
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


def _booking_mode(value: str | None) -> BookingMode:
    if value is None or value == "":
        return BookingMode.DRY_RUN
    try:
        return BookingMode(value.lower())
    except ValueError as exc:
        message = "BOOKING_MODE must be one of: dry_run, live"
        raise ValueError(message) from exc


def _allowed_user_ids(value: str | None) -> frozenset[str]:
    """Parse a comma-separated allowlist without exposing it in status output."""
    if value is None or not value.strip():
        return frozenset()
    values = frozenset(item.strip() for item in value.split(",") if item.strip())
    if not values or any(not item.isdecimal() for item in values):
        raise ValueError("ALLOWED_TELEGRAM_USER_IDS must be comma-separated numeric IDs")
    return values


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
    buyer_profile_path: Path | None = field(default=None, repr=False)
    allowed_telegram_user_ids: frozenset[str] = frozenset()

    @classmethod
    def from_environ(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Create settings from an environment mapping without loading files."""
        source = environ if env is None else env
        allowed_telegram_user_ids = _allowed_user_ids(source.get("ALLOWED_TELEGRAM_USER_IDS"))
        buyer_profile_path = _buyer_profile_path(source.get("BUYER_PROFILE_PATH"))
        booking_mode = _booking_mode(source.get("BOOKING_MODE"))
        if booking_mode is BookingMode.LIVE and buyer_profile_path is None:
            raise ValueError("BOOKING_MODE=live requires BUYER_PROFILE_PATH")
        return cls(
            booking_mode=booking_mode,
            poll_interval_seconds=_positive_int(
                source.get("POLL_INTERVAL_SECONDS"),
                name="POLL_INTERVAL_SECONDS",
                default=60,
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
            telegram_token_configured=bool(source.get("TELEGRAM_BOT_TOKEN")),
            allowed_user_ids_configured=bool(allowed_telegram_user_ids),
            database_url_configured=bool(source.get("DATABASE_URL")),
            allowed_telegram_user_ids=allowed_telegram_user_ids,
            buyer_profile_path=buyer_profile_path,
        )


def _buyer_profile_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser()
