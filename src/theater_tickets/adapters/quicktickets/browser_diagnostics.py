"""Safe helpers for a manually operated QuickTickets checkout observation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit


def validate_session_url(value: str) -> str:
    """Accept only a public QuickTickets session page, never an arbitrary URL."""
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    session_page = parsed.path.rsplit("/", maxsplit=1)[-1]
    if (
        parsed.scheme != "https"
        or hostname not in {"quicktickets.ru"}
        and not hostname.endswith(".quicktickets.ru")
        or not session_page.startswith("s")
        or not session_page[1:].isdecimal()
    ):
        raise ValueError("нужна HTTPS-ссылка на страницу сеанса QuickTickets вида .../s1234")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def sanitize_response(*, method: str, url: str, status: int) -> dict[str, object] | None:
    """Keep endpoint evidence while excluding query values, headers and bodies."""
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or (
        hostname not in {"quicktickets.ru"} and not hostname.endswith(".quicktickets.ru")
    ):
        return None
    path = _sanitized_path(parsed.path)
    return {
        "method": method.upper(),
        "url": urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
        "status": status,
    }


def request_field_names(*, content_type: str | None, body: str | None) -> tuple[str, ...]:
    """Extract only safe field names from a form or JSON body, never values."""
    if not body or not content_type:
        return ()
    normalized_type = content_type.split(";", maxsplit=1)[0].casefold()
    if normalized_type == "application/x-www-form-urlencoded":
        return tuple(
            sorted(
                name
                for raw_name, _ in parse_qsl(body)
                if (name := _safe_field_name(raw_name)) is not None
            )
        )
    if normalized_type == "application/json":
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return ()
        return _object_field_names(parsed)
    return ()


def json_schema(value: object) -> dict[str, object] | None:
    """Return a value-free, conservative JSON shape suitable for a private report."""
    if isinstance(value, dict):
        fields = {
            name: schema
            for raw_name, raw_value in value.items()
            if (name := _safe_field_name(raw_name)) is not None
            if (schema := json_schema(raw_value)) is not None
        }
        return {"type": "object", "fields": fields}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int | float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    return None


def _object_field_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(sorted({name for key in value if (name := _safe_field_name(key)) is not None}))


def _safe_field_name(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if not value[0].islower() or len(value) > 48:
        return None
    if not all(
        character.isascii() and (character.isalnum() or character == "_") for character in value
    ):
        return None
    return value


def _sanitized_path(path: str) -> str:
    """Remove order identifiers embedded in payment URLs, not just query strings."""
    if path.startswith("/payment/order/"):
        return "/payment/order/<redacted>"
    if path.startswith("/payment/"):
        return "/payment/<redacted>"
    return path


def write_observation(*, destination: Path, entries: Iterable[dict[str, object]]) -> Path:
    """Write a private, non-overwriting endpoint summary with owner-only permissions."""
    target = destination.expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"файл отчёта уже существует: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(mode=0o600, exist_ok=False)
    os.chmod(target, 0o600)
    payload = {"responses": list(entries)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
