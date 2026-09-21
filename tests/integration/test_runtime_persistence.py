from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    CandidateModel,
    OutboxMessageModel,
    RuntimeWorkerModel,
    SubscriptionBaselineModel,
)
from theater_tickets.adapters.persistence.repositories import SubscriptionRepository
from theater_tickets.adapters.persistence.runtime import (
    SqlAlchemyActiveSubscriptionSource,
    SqlAlchemyRuntimeStateStore,
    SqlAlchemyStatusReader,
)
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.runtime import WorkerRun
from theater_tickets.application.status import render_status
from theater_tickets.domain.models import BookingMode, Session, SessionKey, Subscription
from theater_tickets.workers.polling import CataloguePollingWorker, CatalogueRead


class FakeCatalogue:
    def __init__(self, reads: list[CatalogueRead | Exception]) -> None:
        self.reads = reads
        self.calls: list[str] = []

    async def fetch_catalogue(self, theatre_alias: str) -> CatalogueRead:
        self.calls.append(theatre_alias)
        value = self.reads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_runtime_lock_rejects_second_owner_until_lease_expires(tmp_path: Path) -> None:
    async def scenario() -> None:
        factory, engine = await _database(tmp_path / "lock.sqlite")
        store = SqlAlchemyRuntimeStateStore(factory)
        now = datetime(2026, 9, 21, 10, tzinfo=UTC)
        assert await store.acquire_lock(owner_id="first", now=now, lease_seconds=30)
        assert not await store.acquire_lock(
            owner_id="second", now=now + timedelta(seconds=29), lease_seconds=30
        )
        assert await store.acquire_lock(
            owner_id="second", now=now + timedelta(seconds=30), lease_seconds=30
        )
        assert not await store.heartbeat(
            owner_id="first", now=now + timedelta(seconds=31), lease_seconds=30
        )
        assert await store.heartbeat(
            owner_id="second", now=now + timedelta(seconds=31), lease_seconds=30
        )
        await store.release_lock(owner_id="second")
        assert await store.acquire_lock(
            owner_id="third", now=now + timedelta(seconds=32), lease_seconds=30
        )
        await engine.dispose()

    asyncio.run(scenario())


def test_catalogue_failure_keeps_baseline_and_status_reports_stale_queues(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        factory, engine = await _database(tmp_path / "status.sqlite")
        subscription = Subscription(
            subscription_id="subscription",
            buyer_id="10",
            theatre_alias="theatre",
            ticket_count=1,
            seat_profile_id="hall-v1",
            max_sessions_per_batch=2,
            booking_mode=BookingMode.DRY_RUN,
        )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            await SubscriptionRepository(uow.session).add(subscription, telegram_chat_id="10")

        stale = datetime(2026, 9, 21, 9, tzinfo=UTC)
        base = _session("base", stale + timedelta(days=3))
        new = _session("new", stale + timedelta(days=4))
        source = FakeCatalogue(
            [
                CatalogueRead((base,), "baseline", True),
                CatalogueRead((base, new), "new-session", True),
                RuntimeError("provider unavailable"),
            ]
        )
        poller = CataloguePollingWorker(
            session_factory=factory,
            source=source,
            subscriptions=SqlAlchemyActiveSubscriptionSource(factory),
        )
        first = await poller.run_once(now=stale)
        assert first.candidates == 0
        second = await poller.run_once(now=stale + timedelta(minutes=1))
        assert second.candidates == 1
        async with factory() as database:
            baseline_before = await database.scalar(select(SubscriptionBaselineModel.snapshot_id))
            candidate = await database.scalar(select(CandidateModel))
            assert candidate is not None
            candidate.tracking_state = "waiting_availability"
            candidate.current_cycle_no = 1
            candidate.next_run_at = stale + timedelta(minutes=5)
            database.add(
                OutboxMessageModel(
                    id="queued-summary",
                    dedup_key="summary:queued",
                    kind="batch_summary",
                    discovery_batch_id=candidate.discovery_batch_id,
                    buyer_id=candidate.buyer_id,
                    destination_chat_id="10",
                    payload={"results": []},
                    state="retry",
                    attempts=3,
                    next_attempt_at=stale + timedelta(minutes=2),
                    last_error_code="rate_limited",
                    created_at=stale + timedelta(minutes=1),
                    updated_at=stale + timedelta(minutes=2),
                )
            )
            await database.commit()

        try:
            await poller.run_once(now=stale + timedelta(minutes=2))
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "transient catalogue failure must remain visible to runtime backoff"
            )
        async with factory() as database:
            baseline_after = await database.scalar(select(SubscriptionBaselineModel.snapshot_id))
            assert baseline_after == baseline_before

        report_time = stale + timedelta(minutes=10)
        await SqlAlchemyRuntimeStateStore(factory).record_worker(
            WorkerRun(
                "catalogue",
                "backoff",
                report_time - timedelta(seconds=1),
                report_time,
                2,
                next_run_at=report_time + timedelta(seconds=30),
                last_succeeded_at=stale + timedelta(minutes=1),
                last_error_code="runtime_error",
                duration_ms=5,
            )
        )
        report = await SqlAlchemyStatusReader(factory).for_user("10", now=report_time)
        text = render_status(report, now=report_time, stale_after_seconds=180)
        assert report.pending_outbox == 1
        assert report.candidates[0].state == "waiting_availability"
        assert "устарела" in text
        assert "Очередь сообщений: 1" in text
        assert "catalogue=backoff (runtime_error)" in text
        async with factory() as database:
            worker = await database.get(RuntimeWorkerModel, "catalogue")
            assert worker is not None and worker.consecutive_failures == 2
        await engine.dispose()

    asyncio.run(scenario())


def _session(session_id: str, starts_at: datetime) -> Session:
    return Session(
        key=SessionKey("quicktickets", "theatre", session_id),
        event_id="event",
        hall_id="hall",
        title=f"Session {session_id}",
        starts_at=starts_at,
    )


async def _database(
    path: Path,
) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    engine, factory = create_session_factory(f"sqlite+aiosqlite:///{path}")
    return factory, engine
