"""Provider-neutral payment notifications and persistent outbox contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from theater_tickets.application.checkout import ConfirmedOrder

MOSCOW = ZoneInfo("Europe/Moscow")


class OutboxKind(StrEnum):
    PAYMENT = "payment"
    BATCH_SUMMARY = "batch_summary"


@dataclass(frozen=True, slots=True)
class RecordedOrder:
    order_id: str
    outbox_id: str


@dataclass(frozen=True, slots=True)
class BatchSessionResult:
    session_title: str
    state: str

    def __post_init__(self) -> None:
        if not self.session_title.strip() or not self.state.strip():
            raise ValueError("batch session result fields must not be empty")


@dataclass(frozen=True, slots=True, repr=False)
class OutboxItem:
    outbox_id: str
    kind: OutboxKind
    destination_chat_id: str
    destination_user_id: str
    text: str
    attempts: int
    expires_at: datetime | None = None
    payment_url: str | None = None
    stop_callback_data: str | None = None
    previous_message_id: str | None = None


class NotificationRateLimited(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        if retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")
        super().__init__("notification transport rate limited")
        self.retry_after_seconds = retry_after_seconds


class NotificationTransport(Protocol):
    async def send(self, item: OutboxItem) -> str:
        """Send a new message and return its provider message ID."""

    async def expire(self, *, chat_id: str, message_id: str) -> None:
        """Best-effort removal of an old payment button."""


class OutboxRepository(Protocol):
    async def recover_claims(self, *, now: datetime) -> int: ...

    async def claim_due(self, *, now: datetime, limit: int) -> tuple[OutboxItem, ...]: ...

    async def mark_sent(self, *, outbox_id: str, message_id: str, sent_at: datetime) -> None: ...

    async def schedule_retry(
        self,
        *,
        outbox_id: str,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> None: ...

    async def reject(self, *, outbox_id: str, error_code: str, now: datetime) -> None: ...


class OrderOutboxWriter(Protocol):
    async def record_confirmed_order(
        self,
        *,
        intent_id: str,
        order: ConfirmedOrder,
        recorded_at: datetime,
    ) -> RecordedOrder: ...

    async def enqueue_batch_summary(
        self,
        *,
        discovery_batch_id: str,
        buyer_id: str,
        results: tuple[BatchSessionResult, ...],
        recorded_at: datetime,
    ) -> str: ...


def payment_message(
    *,
    order_id: str,
    cycle_no: int,
    title: str,
    starts_at: datetime,
    seat_ids: tuple[str, ...],
    total_minor: int,
    currency: str,
    expires_at: datetime,
) -> str:
    seats = ", ".join(seat_ids)
    return (
        "Билеты удержаны.\n"
        f"{title}\n"
        f"Сеанс: {_moscow_time(starts_at)}\n"
        f"Места: {seats}\n"
        f"Сумма: {_money(total_minor, currency)}\n"
        f"Оплатить до: {_moscow_time(expires_at)}\n"
        f"Заказ: {order_id}; цикл №{cycle_no}.\n\n"
        "Оплата выполняется на стороне продавца. После оплаты остановите повторы."
    )


def batch_summary_message(results: tuple[BatchSessionResult, ...]) -> str:
    if not results:
        return "Проверка новых сеансов завершена: подходящих результатов нет."
    lines = ["Итог обработки новых сеансов:"]
    lines.extend(f"• {item.session_title}: {item.state}" for item in results)
    return "\n".join(lines)


def expired_message() -> str:
    return "Срок этой ссылки на оплату истёк. Используйте сообщение нового цикла."


def _moscow_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification timestamps must be timezone-aware")
    return value.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M МСК")


def _money(minor_units: int, currency: str) -> str:
    major, minor = divmod(minor_units, 100)
    amount = f"{major:,}".replace(",", " ")
    suffix = "₽" if currency == "RUB" else currency
    return f"{amount},{minor:02d} {suffix}"
