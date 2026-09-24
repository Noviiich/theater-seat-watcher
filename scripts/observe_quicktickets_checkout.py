"""Open a browser for a user-operated checkout and save only sanitised endpoints."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Request, Response, async_playwright

from theater_tickets.adapters.quicktickets.browser_diagnostics import (
    json_schema,
    request_field_names,
    sanitize_response,
    validate_session_url,
    write_observation,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session-url", required=True, help="HTTPS URL страницы сеанса QuickTickets"
    )
    parser.add_argument("--report", type=Path, required=True, help="новый путь для отчёта JSON")
    parser.add_argument(
        "--allow-manual-checkout",
        action="store_true",
        help="подтверждает, что действия в открытом браузере выполняете вы",
    )
    parser.add_argument(
        "--capture-schema",
        action="store_true",
        help="сохранить только названия полей POST и форму JSON-ответов без значений",
    )
    args = parser.parse_args()
    if not args.allow_manual_checkout:
        parser.error("добавьте --allow-manual-checkout после ознакомления с предупреждением")
    try:
        args.session_url = validate_session_url(args.session_url)
    except ValueError as error:
        parser.error(str(error))
    return args


async def _observe(*, session_url: str, report: Path, capture_schema: bool) -> Path:
    entries: list[dict[str, object]] = []
    response_tasks: set[asyncio.Task[None]] = set()

    async def record(response: Response) -> None:
        entry = sanitize_response(
            method=response.request.method,
            url=response.url,
            status=response.status,
        )
        if entry is None:
            return
        if capture_schema:
            await _add_request_field_names(entry, response.request)
            await _add_json_schema(entry, response)
        if entry not in entries:
            entries.append(entry)

    def schedule_record(response: Response) -> None:
        task = asyncio.create_task(record(response))
        response_tasks.add(task)
        task.add_done_callback(response_tasks.discard)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context()
        context.on("response", schedule_record)
        page = await context.new_page()
        closed = asyncio.Event()
        page.on("close", closed.set)
        try:
            await page.goto(session_url, wait_until="domcontentloaded")
        except PlaywrightError as error:
            await context.close()
            await browser.close()
            raise RuntimeError("не удалось открыть публичную страницу QuickTickets") from error
        print(
            "Браузер открыт. Вы управляете им сами: скрипт не выбирает места и не "
            "отправляет checkout. Любое нажатие в браузере может создать удержание.\n"
            "Закройте вкладку после остановки на нужном шаге; затем будет записан отчёт."
        )
        await closed.wait()
        if response_tasks:
            await asyncio.gather(*response_tasks)
        await context.close()
        await browser.close()
    return write_observation(destination=report, entries=entries)


async def _add_request_field_names(entry: dict[str, object], request: Request) -> None:
    fields = request_field_names(
        content_type=await request.header_value("content-type"), body=request.post_data
    )
    if fields:
        entry["request_fields"] = list(fields)


async def _add_json_schema(entry: dict[str, object], response: Response) -> None:
    content_type = await response.header_value("content-type") or ""
    if content_type.split(";", maxsplit=1)[0].casefold() != "application/json":
        return
    try:
        schema = json_schema(await response.json())
    except Exception:  # The report stays usable if a provider response is malformed.
        return
    if schema is not None:
        entry["response_schema"] = schema


def main() -> None:
    args = _arguments()
    try:
        report = asyncio.run(
            _observe(
                session_url=args.session_url,
                report=args.report,
                capture_schema=args.capture_schema,
            )
        )
    except (FileExistsError, RuntimeError) as error:
        raise SystemExit(f"Диагностика не выполнена: {error}") from error
    print(f"Сохранён обезличенный отчёт: {report}")


if __name__ == "__main__":
    main()
