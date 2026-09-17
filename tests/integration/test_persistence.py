from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import BuyerModel, SessionModel
from theater_tickets.adapters.persistence.repositories import (
    CatalogueRepository,
    SubscriptionRepository,
)
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.domain.models import BookingMode, Money, Session, SessionKey, Subscription


def test_migration_and_transaction_boundaries(tmp_path: Path) -> None:
    database = tmp_path / "tickets.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        now = datetime(2026, 9, 17, tzinfo=UTC)
        session = Session(SessionKey("quicktickets", "theatre", "1"), "event", "hall", "Title", now)
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            uow.session.add(
                BuyerModel(
                    id="buyer",
                    telegram_user_id="1",
                    telegram_chat_id="1",
                    profile_ref=None,
                    created_at=now,
                )
            )
            repository = CatalogueRepository(uow.session)
            await repository.upsert_session(session, seen_at=now)
            await repository.get_or_add_snapshot(
                snapshot_id="snapshot", theatre_alias="theatre", fetched_at=now, fingerprint="fp"
            )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            assert await uow.session.scalar(select(BuyerModel.id)) == "buyer"
            assert await uow.session.scalar(select(SessionModel.provider_session_id)) == "1"
            repository = CatalogueRepository(uow.session)
            existing = await repository.get_or_add_snapshot(
                snapshot_id="duplicate-snapshot",
                theatre_alias="theatre",
                fetched_at=now,
                fingerprint="fp",
            )
            assert existing.id == "snapshot"
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                assert uow.session is not None
                uow.session.add(
                    BuyerModel(
                        id="rolled-back",
                        telegram_user_id="2",
                        telegram_chat_id="2",
                        profile_ref=None,
                        created_at=now,
                    )
                )
                raise RuntimeError("rollback")
        except RuntimeError:
            pass
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            assert await uow.session.get(BuyerModel, "rolled-back") is None

        async def add_duplicate_buyer(identifier: str) -> bool:
            try:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    assert uow.session is not None
                    uow.session.add(
                        BuyerModel(
                            id=identifier,
                            telegram_user_id="duplicate",
                            telegram_chat_id=identifier,
                            profile_ref=None,
                            created_at=now,
                        )
                    )
                return True
            except IntegrityError:
                return False

        inserted = await asyncio.gather(
            add_duplicate_buyer("duplicate-a"), add_duplicate_buyer("duplicate-b")
        )
        assert sorted(inserted) == [False, True]
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            count = await uow.session.scalar(
                select(func.count())
                .select_from(BuyerModel)
                .where(BuyerModel.telegram_user_id == "duplicate")
            )
            assert count == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_subscription_repository_scopes_changes_to_telegram_owner(tmp_path: Path) -> None:
    database = tmp_path / "subscriptions.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="100",
            theatre_alias="theatre",
            ticket_count=2,
            seat_profile_id="hall-v1",
            max_sessions_per_batch=1,
            max_order_total=Money(12345),
            booking_mode=BookingMode.DRY_RUN,
        )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            await SubscriptionRepository(uow.session).add(subscription, telegram_chat_id="100")
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            repository = SubscriptionRepository(uow.session)
            listed = await repository.list_for_telegram_user("100")
            assert listed == (subscription,)
            assert not await repository.set_enabled(
                subscription_id="subscription", telegram_user_id="other", enabled=False
            )
            assert await repository.set_enabled(
                subscription_id="subscription", telegram_user_id="100", enabled=False
            )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            listed = await SubscriptionRepository(uow.session).list_for_telegram_user("100")
            assert listed[0].enabled is False
        await engine.dispose()

    asyncio.run(scenario())
