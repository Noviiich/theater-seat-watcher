"""Isolated Playwright checkout using the contract confirmed by a private HAR."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from theater_tickets.adapters.quicktickets.checkout import (
    BrowserContactFormObservation,
    BrowserHoldObservation,
)
from theater_tickets.application.checkout import (
    CheckoutErrorCode,
    CheckoutRequest,
    CheckoutState,
    ProviderCheckoutObservation,
)
from theater_tickets.domain.models import Money

_PAYMENT_PATH = re.compile(r"^/payment/order/(?P<order_id>[^/]+)/?$")
_POST_JSON_SCRIPT = """
async ({path, fields}) => {
  const body = new URLSearchParams();
  for (const [name, value] of fields) body.append(name, value);
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'X-Requested-With': 'XMLHttpRequest',
    },
    body: body.toString(),
  });
  return {status: response.status, text: await response.text()};
}
"""
_SUBMIT_FORM_SCRIPT = """
({path, fields}) => {
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = path;
  for (const [name, value] of fields) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }
  document.body.appendChild(form);
  form.submit();
}
"""


class PlaywrightQuickTicketsBrowserDriver:
    """Perform one checkout in a fresh cookie context without automatic retries."""

    def __init__(
        self,
        *,
        theatre_alias: str,
        payment_terminal_choice: str,
        now: Callable[[], datetime] | None = None,
        headless: bool = True,
        timeout_ms: int = 30_000,
    ) -> None:
        self._theatre_alias = theatre_alias
        self._payment_terminal_choice = payment_terminal_choice
        self._now = now or (lambda: datetime.now(UTC))
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._request: CheckoutRequest | None = None
        self._codes: tuple[str, ...] = ()
        self._held_at: datetime | None = None

    async def initialize_hold(self, request: CheckoutRequest) -> BrowserHoldObservation:
        """Open the public session page and create exactly one provider hold."""
        self._request = request
        try:
            page = await self._start(request)
            fields = build_init_fields(request, theatre_alias=self._theatre_alias)
            payload = await self._post_json(page, "/ordering/initAnytickets", fields)
            codes, selected_count = parse_init_response(payload)
        except PlaywrightTimeoutError as exc:
            raise TimeoutError("QuickTickets hold timed out") from exc
        except (LookupError, ValueError, json.JSONDecodeError):
            return BrowserHoldObservation(
                False, (), CheckoutErrorCode.CONTRACT_CHANGED, uncertain=True
            )
        if selected_count != len(request.seat_ids) or len(codes) != len(request.seat_ids):
            return BrowserHoldObservation(
                False, (), CheckoutErrorCode.PARTIAL_RESULT, uncertain=True
            )
        self._codes = codes
        self._held_at = self._now()
        return BrowserHoldObservation(True, request.seat_ids)

    async def open_contact_form(self) -> BrowserContactFormObservation:
        """Navigate through the confirmed second form and inspect its safe metadata."""
        request, page = self._state()
        fields = [
            ("organisationAlias", self._theatre_alias),
            ("elemType", "session"),
            ("elemId", request.session_key.session_id),
            ("anyticketsCodes", ",".join(self._codes)),
            ("selectAnyplacesCount", str(len(self._codes))),
        ]
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=self._timeout_ms
            ):
                await page.evaluate(
                    _SUBMIT_FORM_SCRIPT, {"path": "/ordering/anytickets", "fields": fields}
                )
        except PlaywrightTimeoutError as exc:
            raise TimeoutError("QuickTickets contact form timed out") from exc

        form = page.locator('form[action="/ordering/confirm"]').first
        if await form.count() != 1:
            return BrowserContactFormObservation(frozenset(), False, auth_expired=True)
        input_names = frozenset(
            name
            for name in await form.locator(
                "input[name], select[name], textarea[name]"
            ).evaluate_all("elements => elements.map(element => element.name)")
            if isinstance(name, str)
        )
        captcha_present = (
            await page.locator(
                'iframe[src*="captcha" i], [class*="captcha" i], [id*="captcha" i]'
            ).count()
            > 0
        )
        submitters = form.locator(
            'button[type="submit"]:visible:not([disabled]), '
            'input[type="submit"]:visible:not([disabled])'
        )
        return BrowserContactFormObservation(
            input_names,
            await submitters.count() == 1,
            captcha_present=captcha_present,
        )

    async def submit_contact_form(self, request: CheckoutRequest) -> ProviderCheckoutObservation:
        """Fill the observed form, validate the quote and click its one visible submitter."""
        stored_request, page = self._state()
        if stored_request != request or self._held_at is None:
            return _ambiguous(CheckoutErrorCode.CONTRACT_CHANGED)
        form = page.locator('form[action="/ordering/confirm"]').first
        await form.locator('input[name="email"]').fill(request.buyer.email)
        full_name = " ".join(
            (request.buyer.lastname, request.buyer.firstname, request.buyer.middlename)
        )
        await form.locator('input[name="lastname"]').fill(full_name)
        await form.locator('input[name="phone"]').fill(request.buyer.phone)
        await form.locator('input[name="personalDataConsent"]').check(force=True)

        radios = form.locator('input[name="paymentTerminalChoice"]')
        selected_index = await radios.evaluate_all(
            "(elements, expected) => elements.findIndex(element => element.value === expected)",
            self._payment_terminal_choice,
        )
        if not isinstance(selected_index, int) or selected_index < 0:
            return _requires_action(CheckoutErrorCode.CONTRACT_CHANGED)
        await radios.nth(selected_index).check(force=True)

        try:
            amount, commission, total = await self._calculate_total(page, form)
            if amount != request.expected_total or total != amount + commission:
                return _requires_action(CheckoutErrorCode.AMOUNT_MISMATCH)
            if total > request.reserved_total:
                return _requires_action(CheckoutErrorCode.AMOUNT_MISMATCH)
            submitters = form.locator(
                'button[type="submit"]:visible:not([disabled]), '
                'input[type="submit"]:visible:not([disabled])'
            )
            if await submitters.count() != 1:
                return _requires_action(CheckoutErrorCode.CONTRACT_CHANGED)
            async with page.expect_response(
                lambda response: urlsplit(response.url).path == "/ordering/confirm",
                timeout=self._timeout_ms,
            ) as response_info:
                await submitters.first.click()
            response = await response_info.value
            payload = _json_object(await response.text())
            payment_url = parse_confirm_response(payload)
        except PlaywrightTimeoutError as exc:
            raise TimeoutError("QuickTickets confirm timed out") from exc
        except (LookupError, ValueError, json.JSONDecodeError):
            return _ambiguous(CheckoutErrorCode.CONTRACT_CHANGED)

        payment_path = _PAYMENT_PATH.fullmatch(urlsplit(payment_url).path)
        if payment_path is None:
            return _ambiguous(CheckoutErrorCode.INVALID_PAYMENT_URL)
        held_at = self._held_at
        return ProviderCheckoutObservation(
            CheckoutState.CONFIRMED,
            provider_order_id=payment_path.group("order_id"),
            seat_ids=request.seat_ids,
            total=total,
            payment_url=payment_url,
            held_at=held_at,
            expires_at=held_at + timedelta(seconds=request.expected_hold_ttl_seconds),
            payment_url_transferable=True,
        )

    async def aclose(self) -> None:
        """Close all browser resources without retaining cookies on disk."""
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def _start(self, request: CheckoutRequest) -> Page:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout_ms)
        session_url = (
            f"https://quicktickets.ru/{self._theatre_alias}/s{request.session_key.session_id}"
        )
        await self._page.goto(session_url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        return self._page

    async def _calculate_total(self, page: Page, form: Any) -> tuple[Money, Money, Money]:
        fields = []
        for name in (
            "anyticketsCodes",
            "certificatesCodes",
            "exemptionId",
            "organisationAlias",
            "paymentTerminalSystem",
            "paymentTerminalType",
            "phone",
            "promocodeCode",
            "sessionId",
            "useLoyaltyCardBalance",
        ):
            locator = form.locator(f'[name="{name}"]').first
            value = await locator.input_value() if await locator.count() else ""
            fields.append((name, value))
        payload = await self._post_json(page, "/ordering/calcAnytickets", fields)
        return parse_calculation_quote(payload)

    async def _post_json(
        self, page: Page, path: str, fields: list[tuple[str, str]]
    ) -> dict[str, object]:
        raw = await page.evaluate(_POST_JSON_SCRIPT, {"path": path, "fields": fields})
        if (
            not isinstance(raw, dict)
            or raw.get("status") != 200
            or not isinstance(raw.get("text"), str)
        ):
            raise ValueError("unexpected QuickTickets response")
        return _json_object(raw["text"])

    def _state(self) -> tuple[CheckoutRequest, Page]:
        if self._request is None or self._page is None or not self._codes:
            raise RuntimeError("browser checkout state is incomplete")
        return self._request, self._page


def parse_init_response(payload: dict[str, object]) -> tuple[tuple[str, ...], int]:
    """Parse only the confirmed success fields from initAnytickets."""
    if payload.get("result") != "success" or not isinstance(payload.get("data"), dict):
        raise ValueError("QuickTickets hold was not accepted")
    data = payload["data"]
    assert isinstance(data, dict)
    raw_codes = data.get("anyticketsCodes")
    selected_count = data.get("selectAnyplacesCount")
    if (
        not isinstance(raw_codes, list)
        or not raw_codes
        or not all(isinstance(value, str) and value for value in raw_codes)
        or not isinstance(selected_count, int)
        or isinstance(selected_count, bool)
    ):
        raise ValueError("QuickTickets hold response is incomplete")
    return tuple(raw_codes), selected_count


def build_init_fields(request: CheckoutRequest, *, theatre_alias: str) -> list[tuple[str, str]]:
    """Match the observed jQuery form encoding for selected hall places."""
    fields = [
        ("organisationAlias", theatre_alias),
        ("elemType", "session"),
        ("elemId", request.session_key.session_id),
        ("collectiveSell", "0"),
        ("sessionAnyplaces[count]", str(len(request.seat_ids))),
        ("sessionAnyplaces[amount]", _rubles(request.expected_total)),
    ]
    fields.extend(("sessionAnyplaces[hallplaces][]", seat_id) for seat_id in request.seat_ids)
    return fields


def parse_calculation_total(payload: dict[str, object]) -> Money:
    """Read the provider total exactly, including its calculated commission."""
    data = payload.get("data")
    if payload.get("result") != "success" or not isinstance(data, dict):
        raise ValueError("QuickTickets calculation failed")
    total = data.get("total")
    if isinstance(total, bool) or not isinstance(total, (Decimal, int, str)):
        raise ValueError("QuickTickets calculation has no exact total")
    return Money.from_rubles(total)


def parse_calculation_quote(payload: dict[str, object]) -> tuple[Money, Money, Money]:
    """Require exact subtotal, commission and final total before the final POST."""
    data = payload.get("data")
    if payload.get("result") != "success" or not isinstance(data, dict):
        raise ValueError("QuickTickets calculation failed")
    values: list[Money] = []
    for field in ("amount", "commission", "total"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
            raise ValueError(f"QuickTickets calculation has no exact {field}")
        values.append(Money.from_rubles(value))
    return values[0], values[1], values[2]


def parse_confirm_response(payload: dict[str, object]) -> str:
    """Accept only the confirmed payment handoff shape from the observed checkout."""
    data = payload.get("data")
    if payload.get("result") != "success" or not isinstance(data, dict):
        raise ValueError("QuickTickets confirm failed")
    url = data.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("QuickTickets confirm has no payment URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "quicktickets.ru"
        or parsed.username is not None
        or parsed.password is not None
        or _PAYMENT_PATH.fullmatch(parsed.path) is None
        or parsed.fragment
    ):
        raise ValueError("QuickTickets returned an invalid payment URL")
    return url


def _json_object(value: str) -> dict[str, object]:
    parsed = json.loads(value, parse_float=Decimal)
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise ValueError("QuickTickets response must be an object")
    return parsed


def _rubles(value: Money) -> str:
    return format(Decimal(value.minor_units) / Decimal(100), "f")


def _ambiguous(code: CheckoutErrorCode) -> ProviderCheckoutObservation:
    return ProviderCheckoutObservation(CheckoutState.AMBIGUOUS, error_code=code)


def _requires_action(code: CheckoutErrorCode) -> ProviderCheckoutObservation:
    return ProviderCheckoutObservation(CheckoutState.REQUIRES_USER_ACTION, error_code=code)
