from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from theater_tickets.adapters.persistence.access import SqlAlchemyTelegramAccess
from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import BuyerModel, SessionModel
from theater_tickets.adapters.persistence.repositories import (
    CatalogueRepository,
    SubscriptionRepository,
)
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.domain.models import (
    BookingMode,
    Money,
    RenewalPolicy,
    Session,
    SessionKey,
    Subscription,
)


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
            renewal_policy=RenewalPolicy(
                renewal_interval_seconds=1300,
                expected_hold_ttl_seconds=1250,
                availability_retry_seconds=190,
                max_cycles_per_session=4,
            ),
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
            repository = SubscriptionRepository(uow.session)
            listed = await repository.list_for_telegram_user("100")
            assert listed[0].enabled is False
            assert not await repository.delete(
                subscription_id="subscription", telegram_user_id="other"
            )
            assert await repository.delete(subscription_id="subscription", telegram_user_id="100")
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            repository = SubscriptionRepository(uow.session)
            assert await repository.list_for_telegram_user("100") == ()
            assert await repository.list_enabled() == ()
        await engine.dispose()

    asyncio.run(scenario())


def test_buyer_profile_is_persisted_per_telegram_owner_and_can_be_updated(tmp_path: Path) -> None:
    database = tmp_path / "buyer-profile.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            repository = SubscriptionRepository(uow.session)
            assert await repository.buyer_profile("100") is None
            saved = await repository.save_buyer_profile(
                telegram_user_id="100",
                telegram_chat_id="200",
                lastname="Иванов",
                firstname="Иван",
                middlename="Иванович",
                email="ivan@example.test",
                phone="+7 (999) 000-00-00",
            )
            assert saved.phone == "+79990000000"
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            repository = SubscriptionRepository(uow.session)
            profile = await repository.buyer_profile("100")
            assert profile is not None and profile.email == "ivan@example.test"
            await repository.save_buyer_profile(
                telegram_user_id="100",
                telegram_chat_id="201",
                lastname="Петров",
                firstname="Пётр",
                middlename="Петрович",
                email="petr@example.test",
                phone="+79991112233",
            )
            assert await repository.buyer_profile("101") is None
        async with factory() as session:
            buyer = await session.scalar(
                select(BuyerModel).where(BuyerModel.telegram_user_id == "100")
            )
            assert buyer is not None
            assert buyer.telegram_chat_id == "201"
            assert buyer.lastname == "Петров"
            assert buyer.email == "petr@example.test"
        await engine.dispose()

    asyncio.run(scenario())


def test_administrator_access_decision_is_persistent_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "access.sqlite"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")

    async def scenario() -> None:
        engine, factory = create_session_factory(f"sqlite+aiosqlite:///{database}")
        access = SqlAlchemyTelegramAccess(factory)
        assert await access.request(telegram_user_id="100", telegram_chat_id="200")
        assert not await access.is_granted("100")
        assert not await access.request(telegram_user_id="100", telegram_chat_id="201")
        assert await access.decide(telegram_user_id="100", granted=True) == "201"
        assert await access.is_granted("100")
        assert await access.decide(telegram_user_id="100", granted=False) is None
        await engine.dispose()

    asyncio.run(scenario())
