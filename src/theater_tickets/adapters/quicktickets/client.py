"""Bounded, read-only HTTP client for the observed public QuickTickets API."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from theater_tickets.adapters.quicktickets.errors import (
    QuickTicketsAuthError,
    QuickTicketsContractError,
    QuickTicketsError,
    QuickTicketsRateLimitError,
)

TOKEN_PATTERN = re.compile(r"set_token[\s\S]{0,1000}?['\"]([A-Za-z0-9+/=._-]+)['\"]")


@dataclass(frozen=True, slots=True)
class QuickTicketsContext:
    """Fresh public page context used for subsequent API reads."""

    token: str
    session_page_url: str


class QuickTicketsClient:
    """Read catalogue and hall data without exposing credentials or write paths."""

    def __init__(
        self,
        *,
        theatre_alias: str,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = "https://api.quicktickets.ru/v1/",
        site_base_url: str = "https://quicktickets.ru",
        hall_origin: str = "https://hall.quicktickets.ru",
        min_request_interval: float = 0.0,
        max_retries: int = 2,
    ) -> None:
        if not theatre_alias.strip():
            raise ValueError("theatre_alias must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.theatre_alias = theatre_alias
        self.api_base_url = api_base_url.rstrip("/") + "/"
        self.site_base_url = site_base_url.rstrip("/")
        self.hall_origin = hall_origin.rstrip("/")
        self.min_request_interval = max(0.0, min_request_interval)
        self.max_retries = max_retries
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._owns_client = http_client is None
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> QuickTicketsClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_context(self, session_id: str | int) -> QuickTicketsContext:
        """Fetch a public session page and extract its transient API token."""
        _, context = await self.get_session_page(session_id)
        return context

    async def get_catalogue_page(self) -> str:
        """Fetch the public theatre catalogue without exposing its URL upstream."""
        response = await self._request(
            "GET",
            f"{self.site_base_url}/{self.theatre_alias}",
            expected_json=False,
        )
        return response.text

    async def get_session_page(self, session_id: str | int) -> tuple[str, QuickTicketsContext]:
        """Fetch one session page and return HTML with its transient read context."""
        page_url = f"{self.site_base_url}/{self.theatre_alias}/s{session_id}"
        response = await self._request("GET", page_url, expected_json=False)
        match = TOKEN_PATTERN.search(response.text)
        if match is None:
            raise QuickTicketsContractError("public session page has no set_token")
        return response.text, QuickTicketsContext(token=match.group(1), session_page_url=page_url)

    async def get_json(
        self,
        path: str,
        *,
        context: QuickTicketsContext,
        elem_type: str = "session",
        elem_id: str | int,
    ) -> dict[str, Any]:
        """Read one observed JSON endpoint using only public client parameters."""
        params = {
            "scope": "qt",
            "panel": "site",
            "user_id": "0",
            "organisation_alias": self.theatre_alias,
            "elem_type": elem_type,
            "elem_id": str(elem_id),
        }
        response = await self._request(
            "GET",
            self.api_base_url + path.lstrip("/"),
            params=params,
            headers={
                "Authorization": f"Basic {context.token}",
                "Api-Id": "quick-tickets",
                "Cache-Control": "no-cache",
                "Origin": self.hall_origin,
                "Referer": self.hall_origin + "/",
            },
            expected_json=True,
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise QuickTicketsContractError("JSON endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise QuickTicketsContractError("JSON endpoint returned a non-object")
        return payload

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        expected_json: bool,
    ) -> httpx.Response:
        """Perform a bounded read retry; this method intentionally accepts GET only."""
        if method != "GET":
            raise QuickTicketsError("QuickTicketsClient is read-only")
        for attempt in range(self.max_retries + 1):
            await self._wait_for_rate_limit()
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries:
                    raise QuickTicketsError("QuickTickets read timed out") from exc
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code in {401, 403}:
                raise QuickTicketsAuthError(f"QuickTickets returned HTTP {response.status_code}")
            if response.status_code == 429:
                retry_after = self._retry_after(response, attempt)
                if attempt >= self.max_retries:
                    raise QuickTicketsRateLimitError(math.ceil(retry_after))
                await asyncio.sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                if attempt >= self.max_retries:
                    raise QuickTicketsError(
                        f"QuickTickets server error HTTP {response.status_code}"
                    )
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise QuickTicketsError(f"QuickTickets returned HTTP {response.status_code}")
            if expected_json and "json" not in response.headers.get("content-type", "").lower():
                raise QuickTicketsContractError("JSON endpoint returned non-JSON content")
            return response
        raise AssertionError("bounded retry loop did not return")

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + self.min_request_interval
        if wait:
            await asyncio.sleep(wait)

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return max(0.0, min(60.0, float(value)))
            except ValueError:
                try:
                    date_value = parsedate_to_datetime(value).timestamp()
                    return max(0.0, min(60.0, date_value - time.time()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(60, 2**attempt))
