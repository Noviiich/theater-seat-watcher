"""Minimal structured runtime logging with defense-in-depth secret redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

_SENSITIVE_KEYS = re.compile(
    r"(authorization|cookie|token|secret|password|payment_url|email|phone|buyer|profile)",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{12,}\b")
_AUTHORIZATION = re.compile(r"\b(?:Basic|Bearer)\s+[A-Za-z0-9+/=._-]+", re.IGNORECASE)
_PAYMENT_URL = re.compile(r"https://quicktickets\.ru/payment/order/[^\s?#]+", re.IGNORECASE)
_QUERY_SECRET = re.compile(r"([?&](?:token|key|authorization|code)=)[^&\s]+", re.IGNORECASE)


class SafeJsonLogger:
    """Emit one JSON object per event without exception text or sensitive fields."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def emit(self, event: str, **fields: object) -> None:
        payload: dict[str, object] = {"event": _sanitize_text(event)}
        for key, value in fields.items():
            payload[key] = "[redacted]" if _SENSITIVE_KEYS.search(key) else _sanitize(value)
        self._logger.info(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )


def _sanitize(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]" if _SENSITIVE_KEYS.search(str(key)) else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list):
        return [_sanitize(item) for item in value]
    return _sanitize_text(type(value).__name__)


def _sanitize_text(value: str) -> str:
    result = _TELEGRAM_TOKEN.sub("[redacted]", value)
    result = _AUTHORIZATION.sub("[redacted]", result)
    result = _PAYMENT_URL.sub("[redacted]", result)
    return _QUERY_SECRET.sub(r"\1[redacted]", result)
