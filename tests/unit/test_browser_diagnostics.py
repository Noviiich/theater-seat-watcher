from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater_tickets.adapters.quicktickets.browser_diagnostics import (
    json_schema,
    request_field_names,
    sanitize_response,
    validate_session_url,
    write_observation,
)


def test_checkout_observation_accepts_only_public_session_page() -> None:
    assert validate_session_url("https://quicktickets.ru/theatre/s1234?secret=value") == (
        "https://quicktickets.ru/theatre/s1234"
    )
    with pytest.raises(ValueError):
        validate_session_url("https://example.test/theatre/s1234")
    with pytest.raises(ValueError):
        validate_session_url("https://quicktickets.ru/theatre/e1234")
    with pytest.raises(ValueError):
        validate_session_url("https://quicktickets.ru/theatre/something")


def test_checkout_observation_excludes_query_and_non_quicktickets_hosts() -> None:
    assert sanitize_response(
        method="post",
        url="https://api.quicktickets.ru/v1/ordering/confirm?orderCode=secret",
        status=200,
    ) == {
        "method": "POST",
        "url": "https://api.quicktickets.ru/v1/ordering/confirm",
        "status": 200,
    }
    assert sanitize_response(
        method="GET",
        url="https://quicktickets.ru/payment/order/private-order-code",
        status=200,
    ) == {
        "method": "GET",
        "url": "https://quicktickets.ru/payment/order/<redacted>",
        "status": 200,
    }
    assert sanitize_response(method="GET", url="https://payment.test/secret", status=200) is None


def test_checkout_observation_keeps_only_safe_request_field_names_and_json_shape() -> None:
    assert request_field_names(
        content_type="application/x-www-form-urlencoded; charset=UTF-8",
        body="email=secret%40example.test&orderCode=private&%=bad",
    ) == ("email", "orderCode")
    assert request_field_names(
        content_type="application/json",
        body='{"email":"secret@example.test","orderCode":"private"}',
    ) == ("email", "orderCode")
    assert json_schema(
        {"result": "success", "order": {"orderCode": "private"}, "ABC123": "secret"}
    ) == {
        "type": "object",
        "fields": {
            "result": {"type": "string"},
            "order": {
                "type": "object",
                "fields": {"orderCode": {"type": "string"}},
            },
        },
    }


def test_checkout_observation_writes_owner_only_non_overwriting_report(tmp_path: Path) -> None:
    destination = tmp_path / "checkout-observation.json"

    result = write_observation(
        destination=destination,
        entries=[{"method": "POST", "url": "https://quicktickets.ru/path", "status": 200}],
    )

    assert result == destination.resolve()
    assert result.stat().st_mode & 0o777 == 0o600
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "responses": [{"method": "POST", "url": "https://quicktickets.ru/path", "status": 200}]
    }
    with pytest.raises(FileExistsError):
        write_observation(destination=destination, entries=[])
