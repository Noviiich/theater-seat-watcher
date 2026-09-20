from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.booking import (
    SqlAlchemyBatchSummaryScheduler,
    SqlAlchemyBookingRepository,
    SqlAlchemyConfirmedRecoveryHandler,
)
from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    BuyerModel,
    CandidateModel,
    CheckoutIntentModel,
    DryRunReportModel,
    OrderModel,
    OutboxMessageModel,
    RenewalCycleModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.outbox import (
    SqlAlchemyOrderOutboxWriter,
    SqlAlchemyOutboxRepository,
)
from theater_tickets.adapters.persistence.reconciliation import (
    SqlAlchemyRecoveryRequestLoader,
)
from theater_tickets.adapters.persistence.renewals import SqlAlchemyRenewalRepository
from theater_tickets.adapters.persistence.repositories import SubscriptionRepository
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.booking import (
    CandidateEvaluator,
    SeatSelectionConfiguration,
)
from theater_tickets.application.checkout import (
    CheckoutBuyer,
    CheckoutErrorCode,
    CheckoutRequest,
    CheckoutResult,
    CheckoutState,
    ConfirmedOrder,
)
from theater_tickets.application.outbox import OutboxItem
from theater_tickets.application.planning import BookingPlanner, PlanningState
from theater_tickets.domain.models import (
    BookingMode,
    Money,
    RenewalPolicy,
    SaleCapabilities,
    Seat,
    SeatAvailability,
    Session,
    SessionKey,
    Subscription,
)
from theater_tickets.domain.seating.candidates import ScoringWeights, SelectionPreferences
from theater_tickets.domain.seating.topology import (
    HallProfile,
    RowSegment,
    SeatingMode,
    topology_fingerprint,
)
from theater_tickets.workers.booking import (
    BookingWorkflow,
    CheckoutResumeWorker,
    DryRunWorker,
    LiveCandidateProcessor,
)
from theater_tickets.workers.outbox import OutboxWorker
from theater_tickets.workers.renewals import RenewalWorker


@dataclass
class FakeClock:
    now: datetime


class FakeProvider:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.inventory: dict[str, tuple[Seat, ...]] = {}
        self.sale: dict[str, SaleCapabilities] = {}
        self.inventory_calls: list[str] = []

    async def fetch_session(self, key: SessionKey) -> Session:
        return self.sessions[key.session_id]

    async def fetch_inventory(self, key: SessionKey) -> tuple[Seat, ...]:
        self.inventory_calls.append(key.session_id)
        return self.inventory[key.session_id]

    async def fetch_sale_capabilities(self, key: SessionKey) -> SaleCapabilities:
        return self.sale[key.session_id]


class StaticProfiles:
    def __init__(self, value: SeatSelectionConfiguration) -> None:
        self._value = value

    def load(self, profile_id: str) -> SeatSelectionConfiguration:
        if profile_id != self._value.profile.profile_id:
            raise LookupError("profile missing")
        return self._value


class FakeCheckout:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[CheckoutRequest] = []
        self.outcomes: list[CheckoutState] = []

    async def submit(self, request: CheckoutRequest) -> CheckoutResult:
        self.calls.append(request)
        state = self.outcomes.pop(0) if self.outcomes else CheckoutState.CONFIRMED
        if state is CheckoutState.REJECTED:
            return CheckoutResult(state, error_code=CheckoutErrorCode.SEAT_CONFLICT)
        if state is CheckoutState.AMBIGUOUS:
            return CheckoutResult(state, error_code=CheckoutErrorCode.TRANSPORT_TIMEOUT)
        assert state is CheckoutState.CONFIRMED
        cycle_no = len(self.calls)
        return CheckoutResult(
            CheckoutState.CONFIRMED,
            order=ConfirmedOrder(
                provider_order_id=f"provider-{cycle_no}",
                seat_ids=request.seat_ids,
                total=request.expected_total,
                payment_url=f"https://quicktickets.ru/payment/order/fake-{cycle_no}",
                held_at=self.clock.now,
                expires_at=self.clock.now + timedelta(seconds=1200),
            ),
        )


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[OutboxItem] = []
        self.edited: list[str] = []

    async def send(self, item: OutboxItem) -> str:
        self.sent.append(item)
        return str(len(self.sent))

    async def expire(self, *, chat_id: str, message_id: str) -> None:
        self.edited.append(message_id)


def test_baseline_multiple_sessions_renewal_and_stop_are_end_to_end(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        factory, engine = await _database(tmp_path / "workflow.sqlite")
        subscription = _subscription(BookingMode.LIVE, max_sessions=3, max_active=3)
        await _persist_subscription(factory, subscription)
        provider = FakeProvider()
        base = _session("base", now + timedelta(days=4))
        first = _session("new-1", now + timedelta(days=5))
        second = _session("new-2", now + timedelta(days=6))
        _configure(provider, (base, first, second), _seats())
        clock = FakeClock(now)
        checkout = FakeCheckout(clock)
        telegram = FakeTelegram()
        workflow = _workflow(factory, provider, checkout, telegram, clock)

        baseline = await workflow.process_snapshot(
            subscription=subscription,
            sessions=(base,),
            fetched_at=now,
            fingerprint="baseline",
            complete=True,
        )
        assert baseline.discovered_candidates == ()
        assert checkout.calls == []

        discovered = await workflow.process_snapshot(
            subscription=subscription,
            sessions=(base, first, second),
            fetched_at=now + timedelta(minutes=1),
            fingerprint="new-sessions",
            complete=True,
        )
        assert len(discovered.discovered_candidates) == 2
        assert discovered.live_processed == 2
        assert len(checkout.calls) == 2
        assert len([item for item in telegram.sent if item.payment_url is not None]) == 2
        assert any("Итог обработки" in item.text for item in telegram.sent)

        async with factory() as database:
            candidates = (
                await database.execute(
                    select(CandidateModel.id, SessionModel.provider_session_id)
                    .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                    .order_by(SessionModel.provider_session_id)
                )
            ).all()
            assert (
                await database.scalar(
                    select(func.sum(BudgetAllocationModel.reserved_total_minor)).where(
                        BudgetAllocationModel.active.is_(True)
                    )
                )
                == 6_000
            )
        stopped_id = candidates[0].id
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            assert await SubscriptionRepository(uow.session).stop_candidate(
                candidate_id=stopped_id,
                telegram_user_id="10",
            )

        changed = _seats(prices=(1300, 1200, 900, 1000))
        provider.inventory["new-1"] = changed
        provider.inventory["new-2"] = changed
        clock.now = now + timedelta(minutes=1, seconds=1200)
        renewed = await workflow.run_due(now=clock.now)
        assert renewed.live_processed == 1
        assert len(checkout.calls) == 3
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 3
            stopped = await database.get(CandidateModel, stopped_id)
            assert stopped is not None and stopped.tracking_state == "stopped"
            other = await database.scalar(
                select(CandidateModel).where(CandidateModel.id != stopped_id)
            )
            assert other is not None and other.current_cycle_no == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_sale_later_no_group_and_rejected_best_group_retry_without_empty_link(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        factory, engine = await _database(tmp_path / "later.sqlite")
        subscription = _subscription(BookingMode.LIVE)
        await _persist_subscription(factory, subscription)
        provider = FakeProvider()
        base = _session("base", now + timedelta(days=4))
        target = _session("target", now + timedelta(days=5))
        _configure(provider, (base, target), _seats())
        provider.sale["target"] = _capabilities(available=False)
        clock = FakeClock(now)
        checkout = FakeCheckout(clock)
        telegram = FakeTelegram()
        workflow = _workflow(factory, provider, checkout, telegram, clock)
        await workflow.process_snapshot(
            subscription=subscription,
            sessions=(base,),
            fetched_at=now,
            fingerprint="baseline",
            complete=True,
        )
        await workflow.process_snapshot(
            subscription=subscription,
            sessions=(base, target),
            fetched_at=now + timedelta(minutes=1),
            fingerprint="target",
            complete=True,
        )
        assert checkout.calls == []
        assert all(item.payment_url is None for item in telegram.sent)

        provider.sale["target"] = _capabilities()
        provider.inventory["target"] = _seats(
            states=(
                SeatAvailability.FREE,
                SeatAvailability.SOLD,
                SeatAvailability.FREE,
                SeatAvailability.SOLD,
            )
        )
        clock.now = now + timedelta(minutes=4)
        assert (await workflow.run_due(now=clock.now)).live_processed == 1
        assert checkout.calls == []

        provider.inventory["target"] = _seats()
        checkout.outcomes.append(CheckoutState.REJECTED)
        clock.now += timedelta(seconds=180)
        await workflow.run_due(now=clock.now)
        assert len(checkout.calls) == 1
        assert all(item.payment_url is None for item in telegram.sent)
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 0
            assert await database.scalar(select(func.count()).select_from(OutboxMessageModel)) == 1

        provider.inventory["target"] = _seats(
            states=(
                SeatAvailability.SOLD,
                SeatAvailability.SOLD,
                SeatAvailability.FREE,
                SeatAvailability.FREE,
            )
        )
        clock.now += timedelta(seconds=180)
        await workflow.run_due(now=clock.now)
        assert len(checkout.calls) == 2
        assert checkout.calls[-1].seat_ids == ("3", "4")
        assert len([item for item in telegram.sent if item.payment_url is not None]) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_dry_run_is_durable_and_never_becomes_retroactive_live_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        factory, engine = await _database(tmp_path / "dry.sqlite")
        dry = _subscription(BookingMode.DRY_RUN)
        await _persist_subscription(factory, dry)
        provider = FakeProvider()
        base = _session("base", now + timedelta(days=4))
        simulated = _session("simulated", now + timedelta(days=5))
        later = _session("later", now + timedelta(days=6))
        _configure(provider, (base, simulated, later), _seats())
        clock = FakeClock(now)
        checkout = FakeCheckout(clock)
        telegram = FakeTelegram()
        workflow = _workflow(factory, provider, checkout, telegram, clock)
        await workflow.process_snapshot(
            subscription=dry,
            sessions=(base,),
            fetched_at=now,
            fingerprint="baseline",
            complete=True,
        )
        result = await workflow.process_snapshot(
            subscription=dry,
            sessions=(base, simulated),
            fetched_at=now + timedelta(minutes=1),
            fingerprint="simulated",
            complete=True,
        )
        assert result.dry_run_processed == 1
        assert checkout.calls == []
        async with factory() as database:
            report = await database.scalar(select(DryRunReportModel))
            assert report is not None
            assert report.state == "would_book"
            assert report.selected_seat_ids == ["2", "3"]
            assert await database.scalar(select(func.count()).select_from(RenewalCycleModel)) == 0
            assert await database.scalar(select(func.count()).select_from(CheckoutIntentModel)) == 0
            assert (
                await database.scalar(select(func.count()).select_from(BudgetAllocationModel)) == 0
            )
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 0

        live = _subscription(BookingMode.LIVE)
        async with factory() as database, database.begin():
            model = await database.get(SubscriptionModel, "subscription")
            assert model is not None
            model.config = {**model.config, "booking_mode": "live"}
            model.version += 1
        clock.now = now + timedelta(minutes=2)
        same = await workflow.process_snapshot(
            subscription=live,
            sessions=(base, simulated),
            fetched_at=clock.now,
            fingerprint="simulated",
            complete=True,
        )
        assert same.discovered_candidates == ()
        assert checkout.calls == []

        clock.now = now + timedelta(minutes=3)
        fresh = await workflow.process_snapshot(
            subscription=live,
            sessions=(base, simulated, later),
            fetched_at=clock.now,
            fingerprint="later",
            complete=True,
        )
        assert fresh.live_processed == 1
        assert len(checkout.calls) == 1
        async with factory() as database:
            modes = list(
                await database.scalars(
                    select(CandidateModel.booking_mode).order_by(CandidateModel.booking_mode)
                )
            )
            assert modes == ["dry_run", "live"]
        await engine.dispose()

    asyncio.run(scenario())


def test_paused_queued_candidate_and_unscheduled_confirmed_order_recover_safely(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        factory, engine = await _database(tmp_path / "recovery.sqlite")
        subscription = _subscription(BookingMode.LIVE)
        await _persist_subscription(factory, subscription)
        await _seed_live_candidate(factory, now)
        async with factory() as database, database.begin():
            model = await database.get(SubscriptionModel, "subscription")
            assert model is not None
            model.enabled = False
        assert await SqlAlchemyRenewalRepository(factory).claim_due(now=now, limit=10) == ()
        async with factory() as database, database.begin():
            model = await database.get(SubscriptionModel, "subscription")
            assert model is not None
            model.enabled = True

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            planned = await BookingPlanner().plan(
                uow.session,
                candidate_id="candidate",
                subscription=subscription,
                reserved_total=Money(3_000),
                selected_seat_ids=("2", "3"),
                now=now,
                subscription_version=1,
            )
            assert planned.state is PlanningState.PLANNED
            assert planned.checkout_intent_id is not None
        order = ConfirmedOrder(
            "provider-recovered",
            ("2", "3"),
            Money(3_000),
            "https://quicktickets.ru/payment/order/recovered",
            now,
            now + timedelta(seconds=1200),
        )
        await SqlAlchemyOrderOutboxWriter(factory).record_confirmed_order(
            intent_id=planned.checkout_intent_id,
            order=order,
            recorded_at=now,
        )
        handler = SqlAlchemyConfirmedRecoveryHandler(factory, now=lambda: now)
        assert await handler.recover_unscheduled() == 1
        assert await handler.recover_unscheduled() == 0
        async with factory() as database:
            candidate = await database.get(CandidateModel, "candidate")
            assert candidate is not None
            assert candidate.tracking_state == "renewal_waiting"
            assert candidate.next_run_at == (now + timedelta(seconds=1200)).replace(tzinfo=None)
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 1
            assert await database.scalar(select(func.count()).select_from(OutboxMessageModel)) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_restart_before_checkout_resumes_same_intent_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 20, 9, tzinfo=UTC)
        factory, engine = await _database(tmp_path / "resume.sqlite")
        subscription = _subscription(BookingMode.LIVE)
        await _persist_subscription(factory, subscription)
        await _seed_live_candidate(factory, now)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            planned = await BookingPlanner().plan(
                uow.session,
                candidate_id="candidate",
                subscription=subscription,
                reserved_total=Money(5_000),
                expected_total=Money(3_000),
                selected_seat_ids=("2", "3"),
                now=now,
                subscription_version=1,
            )
            assert planned.state is PlanningState.PLANNED
            assert planned.checkout_intent_id is not None

        clock = FakeClock(now)
        checkout = FakeCheckout(clock)
        repository = SqlAlchemyBookingRepository(factory)

        def buyer_loader(_: str) -> CheckoutBuyer:
            return CheckoutBuyer(
                "Иванов",
                "Иван",
                "Иванович",
                "buyer@example.test",
                "+70000000000",
                True,
            )

        resume = CheckoutResumeWorker(
            repository=repository,
            request_loader=SqlAlchemyRecoveryRequestLoader(
                factory,
                buyer_loader=buyer_loader,
            ),
            checkout=checkout,
            order_writer=SqlAlchemyOrderOutboxWriter(factory),
            failures=repository,
            renewals=SqlAlchemyRenewalRepository(factory),
        )
        assert await resume.run_once(now=now) == 1
        assert await resume.run_once(now=now) == 0
        assert len(checkout.calls) == 1
        assert checkout.calls[0].intent_id == planned.checkout_intent_id
        async with factory() as database:
            assert await database.scalar(select(func.count()).select_from(OrderModel)) == 1
            candidate = await database.get(CandidateModel, "candidate")
            assert candidate is not None and candidate.tracking_state == "renewal_waiting"
        await engine.dispose()

    asyncio.run(scenario())


def _workflow(
    factory: async_sessionmaker[AsyncSession],
    provider: FakeProvider,
    checkout: FakeCheckout,
    telegram: FakeTelegram,
    clock: FakeClock,
) -> BookingWorkflow:
    repository = SqlAlchemyBookingRepository(factory)
    evaluator = CandidateEvaluator(
        provider=provider,
        profiles=StaticProfiles(_selection_configuration(_seats())),
    )
    processor = LiveCandidateProcessor(
        session_factory=factory,
        contexts=repository,
        evaluator=evaluator,
        checkout=checkout,
        order_writer=SqlAlchemyOrderOutboxWriter(factory),
        failures=repository,
        buyer_loader=lambda _: CheckoutBuyer(
            "Иванов",
            "Иван",
            "Иванович",
            "buyer@example.test",
            "+70000000000",
            True,
        ),
        now=lambda: clock.now,
    )
    renewal = RenewalWorker(
        repository=SqlAlchemyRenewalRepository(factory),
        processor=processor,
    )
    dry = DryRunWorker(repository=repository, contexts=repository, evaluator=evaluator)
    outbox = OutboxWorker(
        repository=SqlAlchemyOutboxRepository(factory),
        transport=telegram,
        allowed_user_ids=frozenset({"10"}),
    )
    return BookingWorkflow(
        session_factory=factory,
        renewal_worker=renewal,
        dry_run_worker=dry,
        outbox_worker=outbox,
        summaries=SqlAlchemyBatchSummaryScheduler(factory),
    )


def _selection_configuration(inventory: tuple[Seat, ...]) -> SeatSelectionConfiguration:
    profile = HallProfile(
        "profile",
        "hall",
        topology_fingerprint(inventory),
        SeatingMode.AUTOMATIC,
        (RowSegment("main", "Партер", "1", ("1", "2", "3", "4")),),
    )
    preferences = SelectionPreferences(
        ticket_count=2,
        max_ticket_price=Money(2_000),
        max_order_total=Money(5_000),
        row_quality={"1": Decimal("1")},
        weights=ScoringWeights(Decimal("0.25"), Decimal("0.5"), Decimal("0.15"), Decimal("0.1")),
        min_quality=Decimal("0"),
        view_axis_x=Decimal("25"),
        normalization_width=Decimal("40"),
        aisle_quality={"main": Decimal("1")},
    )
    return SeatSelectionConfiguration(profile, preferences)


def _seats(
    *,
    states: tuple[SeatAvailability, ...] | None = None,
    prices: tuple[int, ...] = (1000, 1500, 1500, 1000),
) -> tuple[Seat, ...]:
    states = states or (SeatAvailability.FREE,) * 4
    return tuple(
        Seat(
            str(index),
            "hall",
            "Партер",
            "1",
            str(index),
            Money(price),
            state,
            x=index * 10,
            y=0,
        )
        for index, (price, state) in enumerate(zip(prices, states, strict=True), 1)
    )


def _capabilities(*, available: bool = True) -> SaleCapabilities:
    return SaleCapabilities(available, 4 if available else 0, False, 0, False, None, None)


def _configure(
    provider: FakeProvider,
    sessions: tuple[Session, ...],
    inventory: tuple[Seat, ...],
) -> None:
    for session in sessions:
        provider.sessions[session.key.session_id] = session
        provider.inventory[session.key.session_id] = inventory
        provider.sale[session.key.session_id] = _capabilities()


def _session(identifier: str, starts_at: datetime) -> Session:
    return Session(
        SessionKey("quicktickets", "theatre", identifier),
        "event",
        "hall",
        f"Title {identifier}",
        starts_at,
    )


def _subscription(mode: BookingMode, *, max_sessions: int = 1, max_active: int = 1) -> Subscription:
    return Subscription(
        "subscription",
        "10",
        "theatre",
        2,
        "profile",
        max_sessions,
        max_ticket_price=Money(2_000),
        max_order_total=Money(5_000),
        max_batch_total=Money(20_000),
        max_active_orders=max_active,
        max_active_total=Money(20_000),
        booking_mode=mode,
        renewal_policy=RenewalPolicy(),
    )


async def _persist_subscription(
    factory: async_sessionmaker[AsyncSession], subscription: Subscription
) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        await SubscriptionRepository(uow.session).add(subscription, telegram_chat_id="10")
        await uow.session.flush()
        buyer = await uow.session.scalar(select(BuyerModel))
        assert buyer is not None
        buyer.profile_ref = "buyer-profile.json"


async def _seed_live_candidate(factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        from theater_tickets.adapters.persistence.models import (
            CatalogueSnapshotModel,
            DiscoveryBatchModel,
        )

        uow.session.add_all(
            [
                CatalogueSnapshotModel(
                    id="snapshot",
                    theatre_alias="theatre",
                    fetched_at=now,
                    complete=True,
                    fingerprint="seed",
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
                    provider_session_id="session",
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
        buyer_id = await uow.session.scalar(select(BuyerModel.id))
        assert buyer_id is not None
        uow.session.add(
            CandidateModel(
                id="candidate",
                buyer_id=buyer_id,
                subscription_id="subscription",
                session_id="session",
                discovery_batch_id="batch",
                subscription_version=1,
                booking_mode="live",
                tracking_state="queued",
                current_cycle_no=0,
                watch_until=now + timedelta(days=1),
            )
        )


async def _database(database: Path) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
    return factory, engine
