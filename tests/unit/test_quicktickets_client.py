from __future__ import annotations

import asyncio

import httpx
import pytest

from theater_tickets.adapters.quicktickets.client import QuickTicketsClient, QuickTicketsContext
from theater_tickets.adapters.quicktickets.errors import (
    QuickTicketsAuthError,
    QuickTicketsContractError,
    QuickTicketsRateLimitError,
)


def run(coro):
    return asyncio.run(coro)


def test_client_extracts_context_and_uses_public_api_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "quicktickets.ru":
            return httpx.Response(
                200, text="<script>set_token('transient-token')</script>", request=request
            )
        return httpx.Response(200, json={"response": {"places": []}}, request=request)

    async def scenario() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            async with QuickTicketsClient(
                theatre_alias="theatre", http_client=http_client
            ) as client:
                context = await client.get_context("3159")
                payload = await client.get_json("hall/hall", context=context, elem_id="3159")
        assert payload["response"]["places"] == []

    run(scenario())
    assert seen[1].url.params["scope"] == "qt"
    assert seen[1].headers["Authorization"] == "Basic transient-token"
    assert seen[1].headers["Api-Id"] == "quick-tickets"


@pytest.mark.parametrize("status", [401, 403])
def test_client_does_not_retry_auth_failures(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with QuickTicketsClient(
                theatre_alias="theatre", http_client=http_client
            ) as client:
                await client.get_context("1")

    with pytest.raises(QuickTicketsAuthError):
        run(scenario())
    assert calls == 1


def test_client_retries_429_and_rejects_html_json_response() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, text="<html>", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with QuickTicketsClient(
                theatre_alias="theatre", http_client=http_client, max_retries=1
            ) as client:
                await client.get_json("hall/hall", context=QuickTicketsContext("t", "u"), elem_id=1)

    with pytest.raises(QuickTicketsContractError):
        run(scenario())
    assert calls == 2


def test_client_raises_after_bounded_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            async with QuickTicketsClient(
                theatre_alias="theatre", http_client=http_client, max_retries=1
            ) as client:
                await client.get_context("1")

    with pytest.raises(QuickTicketsRateLimitError):
        run(scenario())
