"""Transactional order recording and SQLite-backed notification outbox."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    BuyerModel,
    CandidateModel,
    CheckoutIntentModel,
    OrderModel,
    OutboxMessageModel,
    RenewalCycleModel,
    SessionModel,
)
from theater_tickets.application.checkout import ConfirmedOrder
from theater_tickets.application.outbox import (
    BatchSessionResult,
    OutboxItem,
    OutboxKind,
    RecordedOrder,
    batch_summary_message,
    payment_message,
)

_DELIVERABLE_STATES = ("pending", "retry")


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SqlAlchemyOrderOutboxWriter:
    """Persist a confirmed order and its notification in the same transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_confirmed_order(
        self,
        *,
        intent_id: str,
        order: ConfirmedOrder,
        recorded_at: datetime,
    ) -> RecordedOrder:
        self._validate_time(recorded_at)
        self._validate_time(order.held_at)
        self._validate_time(order.expires_at)
        async with self._session_factory() as database, database.begin():
            intent = await database.get(CheckoutIntentModel, intent_id)
            if intent is None:
                raise LookupError("checkout intent not found")
            cycle = await database.get(RenewalCycleModel, intent.renewal_cycle_id)
            if cycle is None:
                raise LookupError("renewal cycle not found")
            candidate = await database.get(CandidateModel, cycle.candidate_id)
            if candidate is None:
                raise LookupError("candidate not found")
            buyer = await database.get(BuyerModel, candidate.buyer_id)
            if buyer is None:
                raise LookupError("buyer not found")
            if (
                tuple(intent.selected_seat_ids) != order.seat_ids
                or order.total.minor_units < intent.expected_total_minor
                or order.total.minor_units > intent.reserved_total_minor
                or intent.currency != order.total.currency
            ):
                raise ValueError("confirmed order does not match checkout intent")
            if order.expires_at <= order.held_at or not order.payment_url.strip():
                raise ValueError("confirmed order payment handoff is incomplete")

            existing = await database.scalar(
                select(OrderModel).where(OrderModel.checkout_intent_id == intent_id)
            )
            if existing is not None:
                self._verify_same_order(existing, order)
                message = await database.scalar(
                    select(OutboxMessageModel).where(OutboxMessageModel.order_id == existing.id)
                )
                if message is None:
                    raise LookupError("confirmed order has no outbox message")
                return RecordedOrder(existing.id, message.id)

            allocation = await database.scalar(
                select(BudgetAllocationModel).where(
                    BudgetAllocationModel.checkout_intent_id == intent.id
                )
            )
            if allocation is None or not allocation.active:
                raise LookupError("confirmed order has no active budget allocation")
            allocation.reserved_total_minor = order.total.minor_units

            order_id = str(uuid4())
            outbox_id = str(uuid4())
            database.add(
                OrderModel(
                    id=order_id,
                    checkout_intent_id=intent.id,
                    provider_order_id=order.provider_order_id,
                    state="awaiting_payment",
                    actual_seat_ids=list(order.seat_ids),
                    total_minor=order.total.minor_units,
                    currency=order.total.currency,
                    payment_url=order.payment_url,
                    held_at=order.held_at,
                    expires_at=order.expires_at,
                    created_at=recorded_at,
                )
            )
            database.add(
                OutboxMessageModel(
                    id=outbox_id,
                    dedup_key=f"payment:{candidate.id}:{cycle.cycle_no}:{order_id}",
                    kind=OutboxKind.PAYMENT.value,
                    order_id=order_id,
                    discovery_batch_id=None,
                    buyer_id=buyer.id,
                    destination_chat_id=buyer.telegram_chat_id,
                    payload={},
                    state="pending",
                    attempts=0,
                    next_attempt_at=recorded_at,
                    created_at=recorded_at,
                    updated_at=recorded_at,
                )
            )
            intent.state = "confirmed"
            intent.remote_stage = "validated"
            cycle.state = "awaiting_payment"
            candidate.tracking_state = "awaiting_payment"
            return RecordedOrder(order_id, outbox_id)

    async def enqueue_batch_summary(
        self,
        *,
        discovery_batch_id: str,
        buyer_id: str,
        results: tuple[BatchSessionResult, ...],
        recorded_at: datetime,
    ) -> str:
        self._validate_time(recorded_at)
        async with self._session_factory() as database, database.begin():
            buyer = await database.get(BuyerModel, buyer_id)
            if buyer is None:
                raise LookupError("buyer not found")
            dedup_key = f"batch:{discovery_batch_id}:buyer:{buyer_id}:initial_summary"
            existing = await database.scalar(
                select(OutboxMessageModel.id).where(OutboxMessageModel.dedup_key == dedup_key)
            )
            if existing is not None:
                return str(existing)
            outbox_id = str(uuid4())
            database.add(
                OutboxMessageModel(
                    id=outbox_id,
                    dedup_key=dedup_key,
                    kind=OutboxKind.BATCH_SUMMARY.value,
                    order_id=None,
                    discovery_batch_id=discovery_batch_id,
                    buyer_id=buyer.id,
                    destination_chat_id=buyer.telegram_chat_id,
                    payload={
                        "results": [
                            {"session_title": item.session_title, "state": item.state}
                            for item in results
                        ]
                    },
                    state="pending",
                    attempts=0,
                    next_attempt_at=recorded_at,
                    created_at=recorded_at,
                    updated_at=recorded_at,
                )
            )
            return outbox_id

    @staticmethod
    def _verify_same_order(existing: OrderModel, order: ConfirmedOrder) -> None:
        if (
            existing.provider_order_id != order.provider_order_id
            or tuple(existing.actual_seat_ids) != order.seat_ids
            or existing.total_minor != order.total.minor_units
            or existing.currency != order.total.currency
            or existing.payment_url != order.payment_url
        ):
            raise ValueError("checkout intent already has a different confirmed order")

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("order outbox timestamp must be timezone-aware")


class SqlAlchemyOutboxRepository:
    """Atomically claim due messages and suppress stale payment handoffs."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def recover_claims(self, *, now: datetime) -> int:
        self._validate_time(now)
        async with self._session_factory() as database, database.begin():
            result = cast(
                CursorResult[Any],
                await database.execute(
                    update(OutboxMessageModel)
                    .where(OutboxMessageModel.state == "sending")
                    .values(
                        state="retry",
                        next_attempt_at=now,
                        claimed_at=None,
                        updated_at=now,
                        last_error_code="interrupted_delivery",
                    )
                ),
            )
            return result.rowcount

    async def claim_due(self, *, now: datetime, limit: int) -> tuple[OutboxItem, ...]:
        self._validate_time(now)
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._session_factory() as database, database.begin():
            due_ids = (
                select(OutboxMessageModel.id)
                .where(
                    OutboxMessageModel.state.in_(_DELIVERABLE_STATES),
                    OutboxMessageModel.next_attempt_at <= now,
                )
                .order_by(OutboxMessageModel.next_attempt_at, OutboxMessageModel.created_at)
                .limit(limit)
            )
            claimed_ids = tuple(
                (
                    await database.execute(
                        update(OutboxMessageModel)
                        .where(
                            OutboxMessageModel.id.in_(due_ids),
                            OutboxMessageModel.state.in_(_DELIVERABLE_STATES),
                        )
                        .values(
                            state="sending",
                            attempts=OutboxMessageModel.attempts + 1,
                            claimed_at=now,
                            updated_at=now,
                        )
                        .returning(OutboxMessageModel.id)
                    )
                ).scalars()
            )
            if not claimed_ids:
                return ()
            messages = (
                await database.scalars(
                    select(OutboxMessageModel)
                    .where(OutboxMessageModel.id.in_(claimed_ids))
                    .order_by(OutboxMessageModel.created_at, OutboxMessageModel.id)
                )
            ).all()
            items: list[OutboxItem] = []
            for message in messages:
                item = await self._build_item(database, message=message, now=now)
                if item is not None:
                    items.append(item)
            return tuple(items)

    async def mark_sent(self, *, outbox_id: str, message_id: str, sent_at: datetime) -> None:
        self._validate_time(sent_at)
        if not message_id.strip():
            raise ValueError("telegram message_id must not be empty")
        await self._finish(
            outbox_id,
            state="sent",
            now=sent_at,
            telegram_message_id=message_id,
            last_error_code=None,
        )

    async def schedule_retry(
        self,
        *,
        outbox_id: str,
        now: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> None:
        self._validate_time(now)
        self._validate_time(next_attempt_at)
        async with self._session_factory() as database, database.begin():
            await database.execute(
                update(OutboxMessageModel)
                .where(
                    OutboxMessageModel.id == outbox_id,
                    OutboxMessageModel.state == "sending",
                )
                .values(
                    state="retry",
                    next_attempt_at=next_attempt_at,
                    claimed_at=None,
                    updated_at=now,
                    last_error_code=error_code,
                )
            )

    async def reject(self, *, outbox_id: str, error_code: str, now: datetime) -> None:
        self._validate_time(now)
        await self._finish(
            outbox_id,
            state="rejected",
            now=now,
            last_error_code=error_code,
        )

    async def _build_item(
        self,
        database: AsyncSession,
        *,
        message: OutboxMessageModel,
        now: datetime,
    ) -> OutboxItem | None:
        buyer = await database.get(BuyerModel, message.buyer_id)
        if buyer is None:
            self._fail_model(message, now=now, error_code="buyer_missing")
            return None
        if message.kind == OutboxKind.BATCH_SUMMARY.value:
            results = self._batch_results(message.payload)
            if results is None:
                self._fail_model(message, now=now, error_code="payload_invalid")
                return None
            return OutboxItem(
                outbox_id=message.id,
                kind=OutboxKind.BATCH_SUMMARY,
                destination_chat_id=message.destination_chat_id,
                destination_user_id=buyer.telegram_user_id,
                text=batch_summary_message(results),
                attempts=message.attempts,
            )
        if message.kind != OutboxKind.PAYMENT.value or message.order_id is None:
            self._fail_model(message, now=now, error_code="payload_invalid")
            return None
        return await self._payment_item(database, message=message, buyer=buyer, now=now)

    async def _payment_item(
        self,
        database: AsyncSession,
        *,
        message: OutboxMessageModel,
        buyer: BuyerModel,
        now: datetime,
    ) -> OutboxItem | None:
        row = (
            await database.execute(
                select(OrderModel, RenewalCycleModel, CandidateModel, SessionModel)
                .join(
                    CheckoutIntentModel,
                    CheckoutIntentModel.id == OrderModel.checkout_intent_id,
                )
                .join(
                    RenewalCycleModel,
                    RenewalCycleModel.id == CheckoutIntentModel.renewal_cycle_id,
                )
                .join(CandidateModel, CandidateModel.id == RenewalCycleModel.candidate_id)
                .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                .where(OrderModel.id == message.order_id)
            )
        ).one_or_none()
        if row is None:
            self._fail_model(message, now=now, error_code="order_missing")
            return None
        order, cycle, candidate, session = row
        expires_at = _aware(order.expires_at)
        starts_at = _aware(session.starts_at)
        if expires_at is None or starts_at is None or order.payment_url is None:
            self._fail_model(message, now=now, error_code="order_incomplete")
            return None
        if cycle.cycle_no != candidate.current_cycle_no:
            message.state = "superseded"
            message.claimed_at = None
            message.updated_at = now
            message.last_error_code = "newer_cycle"
            return None
        if expires_at <= now:
            message.state = "expired"
            message.claimed_at = None
            message.updated_at = now
            message.last_error_code = "payment_expired"
            return None
        previous_message_id = await database.scalar(
            select(OutboxMessageModel.telegram_message_id)
            .join(OrderModel, OrderModel.id == OutboxMessageModel.order_id)
            .join(
                CheckoutIntentModel,
                CheckoutIntentModel.id == OrderModel.checkout_intent_id,
            )
            .join(
                RenewalCycleModel,
                RenewalCycleModel.id == CheckoutIntentModel.renewal_cycle_id,
            )
            .where(
                OutboxMessageModel.kind == OutboxKind.PAYMENT.value,
                OutboxMessageModel.state == "sent",
                OutboxMessageModel.telegram_message_id.is_not(None),
                RenewalCycleModel.candidate_id == candidate.id,
                RenewalCycleModel.cycle_no < cycle.cycle_no,
            )
            .order_by(RenewalCycleModel.cycle_no.desc())
            .limit(1)
        )
        return OutboxItem(
            outbox_id=message.id,
            kind=OutboxKind.PAYMENT,
            destination_chat_id=message.destination_chat_id,
            destination_user_id=buyer.telegram_user_id,
            text=payment_message(
                order_id=order.id,
                cycle_no=cycle.cycle_no,
                title=session.title,
                starts_at=starts_at,
                seat_ids=tuple(order.actual_seat_ids),
                total_minor=order.total_minor,
                currency=order.currency,
                expires_at=expires_at,
            ),
            attempts=message.attempts,
            expires_at=expires_at,
            payment_url=order.payment_url,
            stop_callback_data=f"stop:{candidate.id}",
            previous_message_id=(
                str(previous_message_id) if previous_message_id is not None else None
            ),
        )

    async def _finish(
        self,
        outbox_id: str,
        *,
        state: str,
        now: datetime,
        telegram_message_id: str | None = None,
        last_error_code: str | None,
    ) -> None:
        async with self._session_factory() as database, database.begin():
            await database.execute(
                update(OutboxMessageModel)
                .where(
                    OutboxMessageModel.id == outbox_id,
                    OutboxMessageModel.state == "sending",
                )
                .values(
                    state=state,
                    claimed_at=None,
                    sent_at=now if state == "sent" else None,
                    telegram_message_id=telegram_message_id,
                    updated_at=now,
                    last_error_code=last_error_code,
                )
            )

    @staticmethod
    def _batch_results(payload: dict[str, object]) -> tuple[BatchSessionResult, ...] | None:
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return None
        results: list[BatchSessionResult] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                return None
            title = raw.get("session_title")
            state = raw.get("state")
            if not isinstance(title, str) or not isinstance(state, str):
                return None
            results.append(BatchSessionResult(title, state))
        return tuple(results)

    @staticmethod
    def _fail_model(message: OutboxMessageModel, *, now: datetime, error_code: str) -> None:
        message.state = "rejected"
        message.claimed_at = None
        message.updated_at = now
        message.last_error_code = error_code

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("outbox timestamp must be timezone-aware")
