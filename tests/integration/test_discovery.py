from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    BuyerModel,
    CandidateModel,
    DiscoveryBatchModel,
    SubscriptionBaselineModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.discovery import DiscoveryService
from theater_tickets.domain.models import Session, SessionKey, Subscription


def test_discovery_uses_per_subscription_durable_baseline(tmp_path: Path) -> None:
    database = tmp_path / "tickets.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        now = datetime(2026, 9, 17, 9, tzinfo=UTC)
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=3,
        )
        overlapping_subscription = Subscription(
            subscription_id="overlapping-subscription",
            buyer_id="overlapping-buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=3,
        )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            uow.session.add_all(
                [
                    BuyerModel(
                        id="buyer",
                        telegram_user_id="1",
                        telegram_chat_id="1",
                        created_at=now,
                    ),
                    BuyerModel(
                        id="overlapping-buyer",
                        telegram_user_id="2",
                        telegram_chat_id="2",
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
                    SubscriptionModel(
                        id="overlapping-subscription",
                        buyer_id="overlapping-buyer",
                        theatre_alias="theatre",
                        ticket_count=2,
                        seat_profile_id="hall",
                        enabled=True,
                        version=1,
                        config={},
                    ),
                ]
            )

        service = DiscoveryService()
        first = (_session("1", now),)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            outcome = await service.process(
                uow.session,
                subscription=subscription,
                sessions=first,
                fetched_at=now,
                fingerprint="first",
                complete=True,
            )
            assert outcome.baseline_established
            assert not outcome.candidate_ids
            overlapping_outcome = await service.process(
                uow.session,
                subscription=overlapping_subscription,
                sessions=first,
                fetched_at=now,
                fingerprint="first",
                complete=True,
            )
            assert overlapping_outcome.baseline_established

        second = first + (
            _session("2", now + timedelta(days=1)),
            _session("3", now + timedelta(days=2)),
        )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            outcome = await service.process(
                uow.session,
                subscription=subscription,
                sessions=second,
                fetched_at=now + timedelta(minutes=1),
                fingerprint="second",
                complete=True,
            )
            assert len(outcome.discovered_session_ids) == 2
            assert len(outcome.candidate_ids) == 2
            overlapping_outcome = await service.process(
                uow.session,
                subscription=overlapping_subscription,
                sessions=second,
                fetched_at=now + timedelta(minutes=1),
                fingerprint="second",
                complete=True,
            )
            assert len(overlapping_outcome.candidate_ids) == 2

        await engine.dispose()
        restarted_engine, restarted_factory = create_session_factory(
            f"sqlite+aiosqlite:///{database}"
        )
        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            repeated = await service.process(
                uow.session,
                subscription=subscription,
                sessions=second,
                fetched_at=now + timedelta(minutes=2),
                fingerprint="second",
                complete=True,
            )
            assert not repeated.discovered_session_ids
            assert not repeated.candidate_ids
            assert await uow.session.scalar(select(func.count()).select_from(CandidateModel)) == 4
            assert (
                await uow.session.scalar(select(func.count()).select_from(DiscoveryBatchModel)) == 1
            )

        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            incomplete = await service.process(
                uow.session,
                subscription=subscription,
                sessions=(),
                fetched_at=now + timedelta(minutes=3),
                fingerprint="broken",
                complete=False,
            )
            assert not incomplete.baseline_established
            assert (
                await uow.session.scalar(
                    select(func.count()).select_from(SubscriptionBaselineModel)
                )
                == 2
            )

        reappeared = (_session("1", now), _session("3", now + timedelta(days=2)))
        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            outcome = await service.process(
                uow.session,
                subscription=subscription,
                sessions=reappeared,
                fetched_at=now + timedelta(minutes=4),
                fingerprint="reappeared",
                complete=True,
            )
            assert not outcome.discovered_session_ids

        updated = (_session("1", now + timedelta(hours=1)),)
        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            outcome = await service.process(
                uow.session,
                subscription=subscription,
                sessions=updated,
                fetched_at=now + timedelta(minutes=5),
                fingerprint="rescheduled",
                complete=True,
            )
            assert not outcome.candidate_ids

        new_subscription = Subscription(
            subscription_id="new-subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=3,
        )
        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            uow.session.add(
                SubscriptionModel(
                    id="new-subscription",
                    buyer_id="buyer",
                    theatre_alias="theatre",
                    ticket_count=2,
                    seat_profile_id="hall",
                    enabled=True,
                    version=1,
                    config={},
                )
            )
            outcome = await service.process(
                uow.session,
                subscription=new_subscription,
                sessions=second,
                fetched_at=now + timedelta(minutes=6),
                fingerprint="second",
                complete=True,
            )
            assert outcome.baseline_established
            assert not outcome.candidate_ids

        paused = Subscription(
            subscription_id="new-subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=3,
            enabled=False,
        )
        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            outcome = await service.process(
                uow.session,
                subscription=paused,
                sessions=second + (_session("4", now + timedelta(days=3)),),
                fetched_at=now + timedelta(minutes=7),
                fingerprint="paused",
                complete=True,
            )
            assert not outcome.candidate_ids
            assert await uow.session.scalar(select(func.count()).select_from(CandidateModel)) == 4

        resumed = Subscription(
            subscription_id="new-subscription",
            buyer_id="buyer",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall",
            max_sessions_per_batch=3,
        )
        async with SqlAlchemyUnitOfWork(restarted_factory) as uow:
            assert uow.session is not None
            outcome = await service.process(
                uow.session,
                subscription=resumed,
                sessions=second + (_session("4", now + timedelta(days=3)),),
                fetched_at=now + timedelta(minutes=8),
                fingerprint="paused",
                complete=True,
            )
            assert not outcome.candidate_ids
        await restarted_engine.dispose()

    asyncio.run(scenario())


def _session(identifier: str, starts_at: datetime) -> Session:
    return Session(
        key=SessionKey("quicktickets", "theatre", identifier),
        event_id="event",
        hall_id="hall",
        title="Title",
        starts_at=starts_at,
    )
