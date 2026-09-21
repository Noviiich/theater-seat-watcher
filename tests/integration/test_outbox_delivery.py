from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    BuyerModel,
    CandidateModel,
    CatalogueSnapshotModel,
    DiscoveryBatchModel,
    OrderModel,
    OutboxMessageModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.outbox import (
    SqlAlchemyOrderOutboxWriter,
    SqlAlchemyOutboxRepository,
)
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.checkout import ConfirmedOrder
from theater_tickets.application.outbox import (
    BatchSessionResult,
    NotificationRateLimited,
    OutboxItem,
)
from theater_tickets.application.planning import BookingPlanner, PlanningState
from theater_tickets.domain.models import Money, Subscription
from theater_tickets.workers.outbox import OutboxWorker


@dataclass
class FakeTelegram:
    outcomes: list[str]
    fail_edits: bool = False

    def __post_init__(self) -> None:
        self.sent: list[OutboxItem] = []
        self.edited: list[tuple[str, str]] = []

    async def send(self, item: OutboxItem) -> str:
        self.sent.append(item)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "timeout":
            raise TimeoutError("ack lost")
        if outcome == "rate_limit":
            raise NotificationRateLimited(90)
        return str(len(self.sent))

    async def expire(self, *, chat_id: str, message_id: str) -> None:
        self.edited.append((chat_id, message_id))
        if self.fail_edits:
            raise TimeoutError("edit failed")


def test_confirmed_order_and_outbox_are_idempotent_and_delivered(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _setup(tmp_path / "order.sqlite")
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        intent_id = await _plan(factory, now=now, seats=("a1", "a2"), total=4_000)
        order = _order(now=now, cycle_no=1, seats=("a1", "a2"), total=4_000)
        writer = SqlAlchemyOrderOutboxWriter(factory)
        first = await writer.record_confirmed_order(
            intent_id=intent_id, order=order, recorded_at=now
        )
        second = await writer.record_confirmed_order(
            intent_id=intent_id, order=order, recorded_at=now
        )
        assert first == second

        telegram = FakeTelegram(["ok"])
        worker = _worker(factory, telegram)
        assert await worker.run_once(now=now) == 1
        assert len(telegram.sent) == 1
        item = telegram.sent[0]
        assert "Title" in item.text
        assert "a1, a2" in item.text
        assert "40,00 ₽" in item.text
        assert order.payment_url not in item.text
        assert order.payment_url not in repr(item)
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 1
            assert await database.scalar(select(func.count()).select_from(OutboxMessageModel)) == 1
            outbox = await database.get(OutboxMessageModel, first.outbox_id)
            assert outbox is not None
            assert outbox.state == "sent"
            assert outbox.telegram_message_id == "1"
        await engine.dispose()

    asyncio.run(scenario())


def test_order_mismatch_rolls_back_order_and_outbox_together(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _setup(tmp_path / "rollback.sqlite")
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        intent_id = await _plan(factory, now=now, seats=("a1", "a2"), total=4_000)
        with pytest.raises(ValueError, match="does not match checkout intent"):
            await SqlAlchemyOrderOutboxWriter(factory).record_confirmed_order(
                intent_id=intent_id,
                order=_order(now=now, cycle_no=1, seats=("b1", "b2"), total=4_000),
                recorded_at=now,
            )
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 0
            assert await database.scalar(select(func.count()).select_from(OutboxMessageModel)) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_timeout_after_send_retries_after_restart_with_stable_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _setup(tmp_path / "timeout.sqlite")
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        await _create_order(factory, now=now, cycle_no=1, seats=("a1", "a2"), total=4_000)
        telegram = FakeTelegram(["timeout", "ok", "ok"])
        first_worker = _worker(factory, telegram)
        assert await first_worker.run_once(now=now) == 1
        assert await first_worker.run_once(now=now + timedelta(seconds=29)) == 0

        repository = SqlAlchemyOutboxRepository(factory)
        claimed = await repository.claim_due(now=now + timedelta(seconds=30), limit=1)
        assert len(claimed) == 1
        assert await telegram.send(claimed[0]) == "2"

        restarted = _worker(factory, telegram)
        assert await restarted.recover_startup(now=now + timedelta(seconds=31)) == 1
        assert await restarted.run_once(now=now + timedelta(seconds=31)) == 1
        assert len(telegram.sent) == 3
        assert "До окончания удержания:" in telegram.sent[0].text
        assert "До окончания удержания:" not in telegram.sent[1].text
        assert "До окончания удержания:" in telegram.sent[2].text
        assert telegram.sent[0].text != telegram.sent[1].text
        assert "цикл №1" in telegram.sent[2].text
        async with factory() as database:
            outbox = await database.scalar(select(OutboxMessageModel))
            assert outbox is not None
            assert outbox.state == "sent"
            assert outbox.attempts == 3
        await engine.dispose()

    asyncio.run(scenario())


def test_rate_limit_uses_retry_after_and_batch_summary_is_persistent(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _setup(tmp_path / "rate.sqlite")
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        writer = SqlAlchemyOrderOutboxWriter(factory)
        outbox_id = await writer.enqueue_batch_summary(
            discovery_batch_id="batch",
            buyer_id="buyer",
            results=(
                BatchSessionResult("Сеанс A", "ссылка отправлена"),
                BatchSessionResult("Сеанс B", "нет соседних мест"),
            ),
            recorded_at=now,
        )
        assert (
            await writer.enqueue_batch_summary(
                discovery_batch_id="batch",
                buyer_id="buyer",
                results=(BatchSessionResult("ignored", "duplicate"),),
                recorded_at=now,
            )
            == outbox_id
        )
        telegram = FakeTelegram(["rate_limit", "ok"])
        worker = _worker(factory, telegram)
        assert await worker.run_once(now=now) == 1
        assert await worker.run_once(now=now + timedelta(seconds=89)) == 0
        assert await worker.run_once(now=now + timedelta(seconds=90)) == 1
        assert "Сеанс A: ссылка отправлена" in telegram.sent[-1].text
        assert "Сеанс B: нет соседних мест" in telegram.sent[-1].text
        async with factory() as database:
            outbox = await database.get(OutboxMessageModel, outbox_id)
            assert outbox is not None and outbox.state == "sent" and outbox.attempts == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_expired_payment_and_invalid_recipient_are_never_sent(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        expired_factory, expired_engine = await _setup(tmp_path / "expired.sqlite")
        await _create_order(
            expired_factory,
            now=now,
            cycle_no=1,
            seats=("a1", "a2"),
            total=4_000,
            ttl_seconds=10,
        )
        expired_telegram = FakeTelegram([])
        assert (
            await _worker(expired_factory, expired_telegram).run_once(
                now=now + timedelta(seconds=10)
            )
            == 0
        )
        assert expired_telegram.sent == []
        async with expired_factory() as database:
            outbox = await database.scalar(select(OutboxMessageModel))
            assert outbox is not None and outbox.state == "expired"
        await expired_engine.dispose()

        wrong_factory, wrong_engine = await _setup(
            tmp_path / "wrong.sqlite", telegram_chat_id="999"
        )
        await _create_order(wrong_factory, now=now, cycle_no=1, seats=("b1", "b2"), total=4_000)
        wrong_telegram = FakeTelegram([])
        assert await _worker(wrong_factory, wrong_telegram).run_once(now=now) == 1
        assert wrong_telegram.sent == []
        async with wrong_factory() as database:
            outbox = await database.scalar(select(OutboxMessageModel))
            assert outbox is not None
            assert outbox.state == "rejected"
            assert outbox.last_error_code == "invalid_recipient"
        await wrong_engine.dispose()

    asyncio.run(scenario())


def test_three_cycles_send_new_links_and_expire_previous_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _setup(tmp_path / "cycles.sqlite")
        started = datetime(2026, 9, 20, 9, tzinfo=UTC)
        telegram = FakeTelegram(["ok", "ok", "ok"], fail_edits=True)
        worker = _worker(factory, telegram)
        held_times: list[datetime] = []
        for cycle_no, seats, total in (
            (1, ("a1", "a2"), 4_000),
            (2, ("b1", "b2"), 4_200),
            (3, ("c1", "c2"), 4_400),
        ):
            now = started + timedelta(seconds=1200 * (cycle_no - 1))
            if cycle_no > 1:
                await _release(factory, cycle_no=cycle_no - 1, now=now)
            await _create_order(
                factory,
                now=now,
                cycle_no=cycle_no,
                seats=seats,
                total=total,
            )
            held_times.append(now)
            assert await worker.run_once(now=now) == 1

        assert len(telegram.sent) == 3
        assert len({item.payment_url for item in telegram.sent}) == 3
        assert telegram.edited == [("10", "1"), ("10", "2")]
        async with factory() as database:
            orders = (await database.scalars(select(OrderModel).order_by(OrderModel.held_at))).all()
            assert [order.held_at for order in orders] == [
                value.replace(tzinfo=None) for value in held_times
            ]
            assert await database.scalar(select(func.count()).select_from(OutboxMessageModel)) == 3
        await engine.dispose()

    asyncio.run(scenario())


def test_late_old_cycle_is_superseded_when_new_cycle_exists(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _setup(tmp_path / "late.sqlite")
        started = datetime(2026, 9, 20, 9, tzinfo=UTC)
        await _create_order(factory, now=started, cycle_no=1, seats=("a1", "a2"), total=4_000)
        next_time = started + timedelta(seconds=1200)
        await _release(factory, cycle_no=1, now=next_time)
        await _create_order(factory, now=next_time, cycle_no=2, seats=("b1", "b2"), total=4_200)
        telegram = FakeTelegram(["ok"])
        assert await _worker(factory, telegram).run_once(now=next_time) == 1
        assert len(telegram.sent) == 1
        assert "цикл №2" in telegram.sent[0].text
        async with factory() as database:
            states = list(
                await database.scalars(
                    select(OutboxMessageModel.state).order_by(OutboxMessageModel.created_at)
                )
            )
            assert states == ["superseded", "sent"]
        await engine.dispose()

    asyncio.run(scenario())


def _worker(factory: async_sessionmaker[AsyncSession], telegram: FakeTelegram) -> OutboxWorker:
    return OutboxWorker(
        repository=SqlAlchemyOutboxRepository(factory),
        transport=telegram,
        allowed_user_ids=frozenset({"10"}),
    )


async def _create_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    cycle_no: int,
    seats: tuple[str, str],
    total: int,
    ttl_seconds: int = 1200,
) -> None:
    intent_id = await _plan(factory, now=now, seats=seats, total=total)
    async with factory() as database:
        candidate = await database.get(CandidateModel, "candidate")
        assert candidate is not None and candidate.current_cycle_no == cycle_no
    await SqlAlchemyOrderOutboxWriter(factory).record_confirmed_order(
        intent_id=intent_id,
        order=_order(
            now=now,
            cycle_no=cycle_no,
            seats=seats,
            total=total,
            ttl_seconds=ttl_seconds,
        ),
        recorded_at=now,
    )


async def _plan(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    seats: tuple[str, str],
    total: int,
) -> str:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        outcome = await BookingPlanner().plan(
            uow.session,
            candidate_id="candidate",
            subscription=_subscription(),
            reserved_total=Money(total),
            selected_seat_ids=seats,
            now=now,
        )
        assert outcome.state is PlanningState.PLANNED
        assert outcome.checkout_intent_id is not None
        return outcome.checkout_intent_id


async def _release(
    factory: async_sessionmaker[AsyncSession], *, cycle_no: int, now: datetime
) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        assert await BookingPlanner().release_cycle_allocation(
            uow.session,
            candidate_id="candidate",
            cycle_no=cycle_no,
            now=now,
        )


def _order(
    *,
    now: datetime,
    cycle_no: int,
    seats: tuple[str, str],
    total: int,
    ttl_seconds: int = 1200,
) -> ConfirmedOrder:
    return ConfirmedOrder(
        provider_order_id=f"provider-{cycle_no}",
        seat_ids=seats,
        total=Money(total),
        payment_url=f"https://quicktickets.ru/payment/order/secret-{cycle_no}",
        held_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )


def _subscription() -> Subscription:
    return Subscription(
        subscription_id="subscription",
        buyer_id="10",
        theatre_alias="theatre",
        ticket_count=2,
        seat_profile_id="hall",
        max_sessions_per_batch=1,
        max_active_orders=1,
        max_active_total=Money(100_000),
        max_batch_total=Money(100_000),
    )


async def _setup(
    database: Path, *, telegram_chat_id: str = "10"
) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
    now = datetime(2026, 9, 20, 9, tzinfo=UTC)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        uow.session.add_all(
            [
                BuyerModel(
                    id="buyer",
                    telegram_user_id="10",
                    telegram_chat_id=telegram_chat_id,
                    profile_ref=None,
                    created_at=now,
                ),
                SubscriptionModel(
                    id="subscription",
                    buyer_id="buyer",
                    theatre_alias="theatre",
                    ticket_count=2,
                    seat_profile_id="hall",
                    enabled=True,
                    version=1,
                    config={},
                ),
                CatalogueSnapshotModel(
                    id="snapshot",
                    theatre_alias="theatre",
                    fetched_at=now,
                    complete=True,
                    fingerprint="fp",
                ),
                DiscoveryBatchModel(
                    id="batch",
                    snapshot_id="snapshot",
                    discovered_session_ids=["session"],
                    created_at=now,
                ),
                SessionModel(
                    id="session",
                    provider="quicktickets",
                    theatre_alias="theatre",
                    provider_session_id="1",
                    event_id="event",
                    hall_id="hall",
                    title="Title",
                    starts_at=now + timedelta(days=1),
                    first_seen_at=now,
                    last_seen_at=now,
                ),
            ]
        )
        await uow.session.flush()
        uow.session.add(
            CandidateModel(
                id="candidate",
                buyer_id="buyer",
                subscription_id="subscription",
                session_id="session",
                discovery_batch_id="batch",
                booking_mode="live",
                tracking_state="queued",
                current_cycle_no=0,
                watch_until=now + timedelta(days=1),
            )
        )
    return factory, engine
