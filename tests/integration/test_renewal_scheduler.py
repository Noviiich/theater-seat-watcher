from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    BuyerModel,
    CandidateModel,
    CatalogueSnapshotModel,
    CheckoutIntentModel,
    DiscoveryBatchModel,
    OrderModel,
    RenewalCycleModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.renewals import SqlAlchemyRenewalRepository
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.planning import BookingPlanner, PlanningState
from theater_tickets.application.renewals import (
    RenewalProcessResult,
    RenewalProcessState,
    RenewalTask,
)
from theater_tickets.domain.models import Money, RenewalPolicy, Subscription
from theater_tickets.workers.renewals import RenewalWorker


@dataclass
class FakeClock:
    now: datetime


class FakeBookingProcessor:
    """Fake provider boundary that records validated orders without external I/O."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        clock: FakeClock,
        subscription: Subscription,
        offers: list[tuple[tuple[str, ...], int] | RenewalProcessState],
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._subscription = subscription
        self._offers = offers
        self.calls: list[str] = []

    async def process(self, task: RenewalTask) -> RenewalProcessResult:
        self.calls.append(task.candidate_id)
        offer = self._offers.pop(0)
        if isinstance(offer, RenewalProcessState):
            return RenewalProcessResult(offer)
        seats, total_minor = offer
        planner = BookingPlanner()
        async with SqlAlchemyUnitOfWork(self._factory) as uow:
            assert uow.session is not None
            outcome = await planner.plan(
                uow.session,
                candidate_id=task.candidate_id,
                subscription=self._subscription,
                reserved_total=Money(total_minor),
                selected_seat_ids=seats,
                now=self._clock.now,
            )
            if outcome.state is PlanningState.WAITING_BUDGET:
                return RenewalProcessResult(RenewalProcessState.WAITING_BUDGET)
            if outcome.state is PlanningState.SKIPPED_LIMIT:
                return RenewalProcessResult(
                    RenewalProcessState.STOPPED,
                    stop_reason="max_sessions_per_batch",
                )
            if outcome.state is PlanningState.ALREADY_ACTIVE:
                return RenewalProcessResult(RenewalProcessState.ALREADY_ACTIVE)
            assert outcome.state is PlanningState.PLANNED
            assert outcome.checkout_intent_id is not None
            assert outcome.cycle_no is not None
            intent = await uow.session.get(CheckoutIntentModel, outcome.checkout_intent_id)
            candidate = await uow.session.get(CandidateModel, task.candidate_id)
            assert intent is not None and candidate is not None
            intent.state = "confirmed"
            intent.remote_stage = "validated"
            cycle = await uow.session.get(RenewalCycleModel, intent.renewal_cycle_id)
            assert cycle is not None
            cycle.state = "awaiting_payment"
            candidate.tracking_state = "awaiting_payment"
            uow.session.add(
                OrderModel(
                    id=str(uuid4()),
                    checkout_intent_id=intent.id,
                    provider_order_id=f"fake-{task.candidate_id}-{outcome.cycle_no}",
                    state="awaiting_payment",
                    total_minor=total_minor,
                    held_at=self._clock.now,
                    expires_at=self._clock.now + timedelta(seconds=1200),
                    created_at=self._clock.now,
                )
            )
        return RenewalProcessResult(
            RenewalProcessState.HELD,
            cycle_no=outcome.cycle_no,
            held_at=self._clock.now,
        )


def test_three_cycles_use_exact_deadline_and_refresh_seats_and_price(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(
            tmp_path / "cycles.sqlite", candidate_count=1, max_cycles=3
        )
        started = datetime(2026, 9, 20, 9, tzinfo=UTC)
        clock = FakeClock(started)
        subscription = _subscription(max_cycles=3)
        processor = FakeBookingProcessor(
            factory,
            clock,
            subscription,
            [(("a1", "a2"), 4_000), (("b1", "b2"), 4_600), (("c1", "c2"), 3_800)],
        )
        worker = RenewalWorker(
            repository=SqlAlchemyRenewalRepository(factory),
            processor=processor,
        )

        assert await worker.run_once(now=clock.now) == 1
        clock.now = started + timedelta(seconds=1199)
        assert await worker.run_once(now=clock.now) == 0
        clock.now = started + timedelta(seconds=1200)
        assert await worker.run_once(now=clock.now) == 1
        clock.now = started + timedelta(seconds=2400)
        assert await worker.run_once(now=clock.now) == 1

        async with factory() as database:
            intents = (
                await database.scalars(select(CheckoutIntentModel).order_by(CheckoutIntentModel.id))
            ).all()
            assert {tuple(intent.selected_seat_ids) for intent in intents} == {
                ("a1", "a2"),
                ("b1", "b2"),
                ("c1", "c2"),
            }
            assert {intent.reserved_total_minor for intent in intents} == {3_800, 4_000, 4_600}
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 3
            assert (
                await database.scalar(
                    select(func.count(BudgetAllocationModel.id)).where(
                        BudgetAllocationModel.active.is_(True)
                    )
                )
                == 1
            )
            candidate = await database.get(CandidateModel, "candidate-1")
            assert candidate is not None
            assert candidate.current_cycle_no == 3
            assert candidate.tracking_state == "stopped"
            assert candidate.stop_reason == "max_cycles_per_session"
        await dispose()

    asyncio.run(scenario())


def test_due_cycle_runs_at_1200_and_late_restart_collapses_missed_ticks(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(tmp_path / "restart.sqlite", candidate_count=1)
        started = datetime(2026, 9, 20, 9, tzinfo=UTC)
        clock = FakeClock(started)
        processor = FakeBookingProcessor(
            factory,
            clock,
            _subscription(),
            [(("a1", "a2"), 4_000), (("b1", "b2"), 4_200), (("c1", "c2"), 4_400)],
        )
        worker = RenewalWorker(repository=SqlAlchemyRenewalRepository(factory), processor=processor)
        assert await worker.run_once(now=clock.now) == 1

        clock.now = started + timedelta(seconds=1201)
        assert await worker.run_once(now=clock.now) == 1
        clock.now = started + timedelta(seconds=5000)
        assert await worker.run_once(now=clock.now) == 1
        assert len(processor.calls) == 3
        async with factory() as database:
            candidate = await database.get(CandidateModel, "candidate-1")
            assert candidate is not None
            assert candidate.current_cycle_no == 3
            assert candidate.next_run_at == _naive(clock.now + timedelta(seconds=1200))
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 3
        await dispose()

    asyncio.run(scenario())


def test_no_group_and_budget_limit_retry_after_180_seconds(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(tmp_path / "wait.sqlite", candidate_count=3)
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        clock = FakeClock(now)
        processor = FakeBookingProcessor(
            factory,
            clock,
            _subscription(max_active_orders=1),
            [
                RenewalProcessState.WAITING_AVAILABILITY,
                (("b1", "b2"), 4_000),
                (("c1", "c2"), 4_000),
            ],
        )
        worker = RenewalWorker(repository=SqlAlchemyRenewalRepository(factory), processor=processor)
        assert await worker.run_once(now=now) == 3
        async with factory() as database:
            first = await database.get(CandidateModel, "candidate-1")
            second = await database.get(CandidateModel, "candidate-2")
            third = await database.get(CandidateModel, "candidate-3")
            assert first is not None and second is not None and third is not None
            assert first.tracking_state == "waiting_availability"
            assert first.next_run_at == _naive(now + timedelta(seconds=180))
            assert second.tracking_state == "renewal_waiting"
            assert second.current_cycle_no == 1
            assert third.tracking_state == "waiting_budget"
            assert third.next_run_at == _naive(now + timedelta(seconds=180))
            assert third.current_cycle_no == 0
        await dispose()

    asyncio.run(scenario())


def test_two_sessions_and_two_schedulers_remain_independent(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(tmp_path / "concurrent.sqlite", candidate_count=2)
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        clock = FakeClock(now)
        processor = FakeBookingProcessor(
            factory,
            clock,
            _subscription(max_active_orders=2),
            [(("a1", "a2"), 3_000), (("b1", "b2"), 3_200)],
        )
        workers = (
            RenewalWorker(
                repository=SqlAlchemyRenewalRepository(factory),
                processor=processor,
                batch_size=1,
            ),
            RenewalWorker(
                repository=SqlAlchemyRenewalRepository(factory),
                processor=processor,
                batch_size=1,
            ),
        )
        counts = await asyncio.gather(*(worker.run_once(now=now) for worker in workers))
        assert sum(counts) == 2
        assert sorted(processor.calls) == ["candidate-1", "candidate-2"]
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 2
        await dispose()

    asyncio.run(scenario())


def test_restart_after_intent_does_not_create_another_cycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(tmp_path / "intent.sqlite", candidate_count=1)
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        repository = SqlAlchemyRenewalRepository(factory)
        tasks = await repository.claim_due(now=now, limit=1)
        assert len(tasks) == 1
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            outcome = await BookingPlanner().plan(
                uow.session,
                candidate_id="candidate-1",
                subscription=_subscription(),
                reserved_total=Money(4_000),
                selected_seat_ids=("a1", "a2"),
                now=now,
            )
            assert outcome.state is PlanningState.PLANNED

        restarted_processor = FakeBookingProcessor(
            factory, FakeClock(now), _subscription(), [(("b1", "b2"), 4_000)]
        )
        restarted = RenewalWorker(
            repository=SqlAlchemyRenewalRepository(factory),
            processor=restarted_processor,
        )
        assert await restarted.recover_startup() == 0
        assert await restarted.run_once(now=now + timedelta(seconds=1201)) == 0
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(RenewalCycleModel)) == 1
            assert await database.scalar(select(func.count()).select_from(CheckoutIntentModel)) == 1
        await dispose()

    asyncio.run(scenario())


def test_pause_stop_watch_until_and_cycle_limit_prevent_new_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(tmp_path / "stops.sqlite", candidate_count=4)
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        async with factory() as database, database.begin():
            stopped = await database.get(CandidateModel, "candidate-1")
            expired = await database.get(CandidateModel, "candidate-2")
            limited = await database.get(CandidateModel, "candidate-3")
            assert stopped is not None and expired is not None and limited is not None
            stopped.tracking_state = "stopped"
            stopped.stop_reason = "user_stop"
            expired.watch_until = now
            limited.current_cycle_no = 1
            subscription = await database.get(SubscriptionModel, "subscription")
            assert subscription is not None
            subscription.config = {**subscription.config, "max_cycles_per_session": 1}

        tasks = await SqlAlchemyRenewalRepository(factory).claim_due(now=now, limit=10)
        assert [task.candidate_id for task in tasks] == ["candidate-4"]
        async with factory() as database:
            assert (
                await database.scalar(
                    select(CandidateModel.tracking_state).where(CandidateModel.id == "candidate-2")
                )
                == "stopped"
            )
            assert (
                await database.scalar(
                    select(CandidateModel.tracking_state).where(CandidateModel.id == "candidate-3")
                )
                == "stopped"
            )
        await SqlAlchemyRenewalRepository(factory).release_claim(task=tasks[0])
        async with factory() as database, database.begin():
            subscription = await database.get(SubscriptionModel, "subscription")
            assert subscription is not None
            subscription.enabled = False
        assert await SqlAlchemyRenewalRepository(factory).claim_due(now=now, limit=10) == ()
        await dispose()

    asyncio.run(scenario())


def test_elapsed_allocations_are_released_while_paused_or_stopped(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, dispose = await _setup_database(tmp_path / "release.sqlite", candidate_count=2)
        started = datetime(2026, 9, 20, 9, tzinfo=UTC)
        clock = FakeClock(started)
        processor = FakeBookingProcessor(
            factory,
            clock,
            _subscription(max_active_orders=2),
            [(("a1", "a2"), 3_000), (("b1", "b2"), 3_200)],
        )
        worker = RenewalWorker(repository=SqlAlchemyRenewalRepository(factory), processor=processor)
        assert await worker.run_once(now=started) == 2
        async with factory() as database, database.begin():
            stopped = await database.get(CandidateModel, "candidate-1")
            subscription = await database.get(SubscriptionModel, "subscription")
            assert stopped is not None and subscription is not None
            stopped.tracking_state = "stopped"
            stopped.next_run_at = None
            stopped.stop_reason = "user_stop"
            subscription.enabled = False

        clock.now = started + timedelta(seconds=1200)
        assert await worker.run_once(now=clock.now) == 0
        async with factory() as database:
            assert (
                await database.scalar(
                    select(func.count(BudgetAllocationModel.id)).where(
                        BudgetAllocationModel.active.is_(True)
                    )
                )
                == 0
            )
            assert (
                await database.scalar(
                    select(func.count(RenewalCycleModel.id)).where(
                        RenewalCycleModel.allocation_released_at.is_not(None)
                    )
                )
                == 2
            )
        await dispose()

    asyncio.run(scenario())


async def _setup_database(
    database: Path, *, candidate_count: int, max_cycles: int | None = None
) -> tuple[async_sessionmaker[AsyncSession], object]:
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
                    telegram_user_id="1",
                    telegram_chat_id="1",
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
                    config={
                        "renewal_interval_seconds": 1200,
                        "availability_retry_seconds": 180,
                        "max_cycles_per_session": max_cycles,
                    },
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
                    discovered_session_ids=[
                        f"session-{index}" for index in range(1, candidate_count + 1)
                    ],
                    created_at=now,
                ),
            ]
        )
        await uow.session.flush()
        for index in range(1, candidate_count + 1):
            uow.session.add(
                SessionModel(
                    id=f"session-{index}",
                    provider="quicktickets",
                    theatre_alias="theatre",
                    provider_session_id=str(index),
                    event_id="event",
                    hall_id="hall",
                    title=f"Title {index}",
                    starts_at=now + timedelta(days=1),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        await uow.session.flush()
        for index in range(1, candidate_count + 1):
            uow.session.add(
                CandidateModel(
                    id=f"candidate-{index}",
                    buyer_id="buyer",
                    subscription_id="subscription",
                    session_id=f"session-{index}",
                    discovery_batch_id="batch",
                    booking_mode="live",
                    tracking_state="queued",
                    current_cycle_no=0,
                    watch_until=now + timedelta(hours=12),
                )
            )

    async def dispose() -> None:
        await engine.dispose()

    return factory, dispose


def _subscription(*, max_cycles: int | None = None, max_active_orders: int = 4) -> Subscription:
    return Subscription(
        subscription_id="subscription",
        buyer_id="buyer",
        theatre_alias="theatre",
        ticket_count=2,
        seat_profile_id="hall",
        max_sessions_per_batch=4,
        max_active_orders=max_active_orders,
        max_active_total=Money(100_000),
        max_batch_total=Money(100_000),
        renewal_policy=RenewalPolicy(max_cycles_per_session=max_cycles),
    )


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None)
