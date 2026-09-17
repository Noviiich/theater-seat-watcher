from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.planning import BookingPlanner, PlanningState
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
