from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.checkout import SqlAlchemyCheckoutStageRecorder
from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    BuyerModel,
    CandidateModel,
    CheckoutIntentModel,
    DiscoveryBatchModel,
    RenewalCycleModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.reconciliation import (
    SqlAlchemyRecoveryRepository,
    SqlAlchemyRecoveryRequestLoader,
)
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.checkout import (
    CheckoutBuyer,
    CheckoutErrorCode,
    CheckoutStage,
)
from theater_tickets.application.planning import BookingPlanner, PlanningState
from theater_tickets.application.reconciliation import (
    RecoveryDisposition,
    RecoveryOutcome,
)
from theater_tickets.domain.models import Money, Subscription


def test_planner_reserves_limits_and_reuses_expired_cycle_budget(tmp_path: Path) -> None:
    database = tmp_path / "planning.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        now = datetime(2026, 9, 17, 9, tzinfo=UTC)
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=2,
            max_batch_total=Money(10_000),
            max_active_orders=2,
            max_active_total=Money(10_000),
        )
        await _seed(factory, now, candidate_count=3)
        planner = BookingPlanner()

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            first = await planner.plan(
                uow.session,
                candidate_id="candidate-1",
                subscription=subscription,
                reserved_total=Money(4_000),
                selected_seat_ids=("a1", "a2"),
                now=now,
            )
            second = await planner.plan(
                uow.session,
                candidate_id="candidate-2",
                subscription=subscription,
                reserved_total=Money(4_000),
                selected_seat_ids=("b1", "b2"),
                now=now,
            )
            assert first.state is PlanningState.PLANNED
            assert second.state is PlanningState.PLANNED

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            third = await planner.plan(
                uow.session,
                candidate_id="candidate-3",
                subscription=subscription,
                reserved_total=Money(1_000),
                selected_seat_ids=("c1", "c2"),
                now=now,
            )
            assert third.state is PlanningState.SKIPPED_LIMIT
            assert (
                await uow.session.scalar(
                    select(CandidateModel.tracking_state).where(CandidateModel.id == "candidate-3")
                )
                == "skipped_limit"
            )

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            assert await planner.release_cycle_allocation(
                uow.session, candidate_id="candidate-1", cycle_no=1, now=now
            )
            renewed = await planner.plan(
                uow.session,
                candidate_id="candidate-1",
                subscription=subscription,
                reserved_total=Money(6_000),
                selected_seat_ids=("d1", "d2"),
                now=now,
            )
            assert renewed.state is PlanningState.PLANNED
            assert renewed.cycle_no == 2

        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            assert (
                await uow.session.scalar(select(func.count()).select_from(RenewalCycleModel))
            ) == 3
            assert (
                await uow.session.scalar(select(func.count()).select_from(CheckoutIntentModel))
            ) == 3
            active_total = await uow.session.scalar(
                select(func.sum(BudgetAllocationModel.reserved_total_minor)).where(
                    BudgetAllocationModel.active.is_(True)
                )
            )
            assert active_total == 10_000
        await engine.dispose()

    asyncio.run(scenario())


def test_planner_keeps_unknown_active_intent_and_budget_limit(tmp_path: Path) -> None:
    database = tmp_path / "unknown.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        now = datetime(2026, 9, 17, 9, tzinfo=UTC)
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=3,
            max_active_orders=1,
            max_active_total=Money(5_000),
        )
        await _seed(factory, now, candidate_count=2)
        planner = BookingPlanner()
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            planned = await planner.plan(
                uow.session,
                candidate_id="candidate-1",
                subscription=subscription,
                reserved_total=Money(5_000),
                selected_seat_ids=("a1", "a2"),
                now=now,
            )
            assert planned.state is PlanningState.PLANNED
            intent = await uow.session.get(CheckoutIntentModel, planned.checkout_intent_id)
            assert intent is not None
            intent.state = "unknown"
            blocked = await planner.plan(
                uow.session,
                candidate_id="candidate-2",
                subscription=subscription,
                reserved_total=Money(1_000),
                selected_seat_ids=("b1", "b2"),
                now=now,
            )
            assert blocked.state is PlanningState.WAITING_BUDGET
        await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_planning_cannot_exceed_active_total(tmp_path: Path) -> None:
    database = tmp_path / "concurrent.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        now = datetime(2026, 9, 17, 9, tzinfo=UTC)
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=2,
            max_active_orders=2,
            max_active_total=Money(5_000),
        )
        await _seed(factory, now, candidate_count=2)

        async def plan_candidate(candidate_id: str) -> PlanningState:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                assert uow.session is not None
                outcome = await BookingPlanner().plan(
                    uow.session,
                    candidate_id=candidate_id,
                    subscription=subscription,
                    reserved_total=Money(4_000),
                    selected_seat_ids=(f"{candidate_id}-1", f"{candidate_id}-2"),
                    now=now,
                )
                return outcome.state

        outcomes = await asyncio.gather(
            plan_candidate("candidate-1"), plan_candidate("candidate-2")
        )
        assert sorted(outcomes) == [PlanningState.PLANNED, PlanningState.WAITING_BUDGET]
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            active_total = await uow.session.scalar(
                select(func.sum(BudgetAllocationModel.reserved_total_minor)).where(
                    BudgetAllocationModel.active.is_(True)
                )
            )
            assert active_total == 4_000
        await engine.dispose()

    asyncio.run(scenario())


def test_checkout_stages_are_committed_in_short_transactions(tmp_path: Path) -> None:
    database = tmp_path / "checkout-stages.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        now = datetime(2026, 9, 19, 16, 30, tzinfo=UTC)
        await _seed(factory, now, candidate_count=1)
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=1,
        )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            planned = await BookingPlanner().plan(
                uow.session,
                candidate_id="candidate-1",
                subscription=subscription,
                reserved_total=Money(4_000),
                selected_seat_ids=("seat-1", "seat-2"),
                now=now,
            )
        assert planned.checkout_intent_id is not None

        recorder = SqlAlchemyCheckoutStageRecorder(factory, now=lambda: now)
        await recorder.record(planned.checkout_intent_id, CheckoutStage.WRITE_STARTED)
        await recorder.record(
            planned.checkout_intent_id,
            CheckoutStage.AMBIGUOUS,
            CheckoutErrorCode.TRANSPORT_TIMEOUT,
        )

        async with factory() as session:
            intent = await session.get(CheckoutIntentModel, planned.checkout_intent_id)
            assert intent is not None
            assert intent.state == "unknown"
            assert intent.remote_stage == "ambiguous"
            assert intent.write_started_at == now.replace(tzinfo=None)
            assert intent.write_completed_at == now.replace(tzinfo=None)
            assert intent.last_error_code == "transport_timeout"
        await engine.dispose()

    asyncio.run(scenario())


def test_recovery_state_transitions_keep_allocations_active(tmp_path: Path) -> None:
    database = tmp_path / "recovery.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        now = datetime(2026, 9, 19, 16, 30, tzinfo=UTC)
        await _seed(factory, now, candidate_count=4)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            buyer = await uow.session.get(BuyerModel, "buyer")
            assert buyer is not None
            buyer.profile_ref = "private-profile-ref"
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=4,
            max_active_orders=4,
        )
        intent_ids: list[str] = []
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            for index in range(1, 5):
                planned = await BookingPlanner().plan(
                    uow.session,
                    candidate_id=f"candidate-{index}",
                    subscription=subscription,
                    reserved_total=Money(1_000),
                    expected_total=Money(900),
                    selected_seat_ids=(f"seat-{index}-1", f"seat-{index}-2"),
                    now=now,
                )
                assert planned.checkout_intent_id is not None
                intent_ids.append(planned.checkout_intent_id)

        recorder = SqlAlchemyCheckoutStageRecorder(factory, now=lambda: now)
        for intent_id in intent_ids[1:]:
            await recorder.record(intent_id, CheckoutStage.WRITE_STARTED)
        repository = SqlAlchemyRecoveryRepository(factory, now=lambda: now)
        incomplete = await repository.list_incomplete()
        by_intent = {item.intent_id: item.write_started for item in incomplete}
        assert set(by_intent) == set(intent_ids)
        assert by_intent[intent_ids[0]] is False
        assert all(by_intent[intent_id] for intent_id in intent_ids[1:])

        loaded_refs: list[str] = []

        def load_buyer(reference: str) -> CheckoutBuyer:
            loaded_refs.append(reference)
            return CheckoutBuyer(
                lastname="Tester",
                firstname="Test",
                middlename="Example",
                email="buyer@example.test",
                phone="+79990000000",
                personal_data_consent=True,
            )

        recovered_request = await SqlAlchemyRecoveryRequestLoader(
            factory, buyer_loader=load_buyer
        ).load(intent_ids[0])
        assert loaded_refs == ["private-profile-ref"]
        assert recovered_request.expected_total == Money(900)
        assert recovered_request.reserved_total == Money(1_000)
        assert recovered_request.seat_ids == ("seat-1-1", "seat-1-2")

        await repository.apply(RecoveryOutcome(intent_ids[0], RecoveryDisposition.RETRY_ALLOWED))
        await repository.apply(RecoveryOutcome(intent_ids[1], RecoveryDisposition.RETRY_ALLOWED))
        await repository.apply(RecoveryOutcome(intent_ids[2], RecoveryDisposition.CONFIRMED))
        await repository.apply(
            RecoveryOutcome(
                intent_ids[3],
                RecoveryDisposition.NEEDS_ATTENTION,
                error_code=CheckoutErrorCode.PARTIAL_RESULT,
            )
        )

        async with factory() as session:
            intents = [await session.get(CheckoutIntentModel, value) for value in intent_ids]
            assert all(intent is not None for intent in intents)
            assert [intent.state for intent in intents if intent is not None] == [
                "pending",
                "retry_allowed",
                "validated",
                "unknown",
            ]
            assert (
                await session.scalar(
                    select(func.count(BudgetAllocationModel.id)).where(
                        BudgetAllocationModel.active.is_(True)
                    )
                )
                == 4
            )
            assert (
                await session.scalar(
                    select(CandidateModel.tracking_state).where(CandidateModel.id == "candidate-4")
                )
                == "needs_attention"
            )
        await engine.dispose()

    asyncio.run(scenario())


async def _seed(
    factory: async_sessionmaker[AsyncSession], now: datetime, *, candidate_count: int
) -> None:
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert uow.session is not None
        uow.session.add_all(
            (
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
                    config={},
                ),
                DiscoveryBatchModel(
                    id="batch",
                    snapshot_id="snapshot",
                    discovered_session_ids=[
                        f"session-{index}" for index in range(1, candidate_count + 1)
                    ],
                    created_at=now,
                ),
            )
        )
        # The batch fixture only needs a valid foreign key; the snapshot is not otherwise read.
        # It is inserted directly after the required parent so SQLite verifies the complete graph.
        from theater_tickets.adapters.persistence.models import CatalogueSnapshotModel

        uow.session.add(
            CatalogueSnapshotModel(
                id="snapshot",
                theatre_alias="theatre",
                fetched_at=now,
                complete=True,
                fingerprint="fp",
            )
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
                    title="Title",
                    starts_at=now,
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
                    tracking_state="queued",
                    current_cycle_no=0,
                )
            )
