"""SQLite runtime lease, diagnostics and owner-scoped status queries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import (
    BuyerModel,
    CandidateModel,
    CatalogueSnapshotModel,
    CheckoutIntentModel,
    OrderModel,
    OutboxMessageModel,
    RenewalCycleModel,
    RuntimeLockModel,
    RuntimeWorkerModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.repositories import SubscriptionRepository
from theater_tickets.application.runtime import WorkerRun
from theater_tickets.application.status import CandidateStatus, RuntimeStatus
from theater_tickets.domain.models import Subscription

LOCK_NAME = "theater_tickets"


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class SqlAlchemyRuntimeStateStore:
    """Use a renewable SQLite row as the single-instance process lease."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def acquire_lock(self, *, owner_id: str, now: datetime, lease_seconds: int) -> bool:
        self._validate(owner_id, now, lease_seconds)
        expires_at = now + timedelta(seconds=lease_seconds)
        statement = sqlite_insert(RuntimeLockModel).values(
            name=LOCK_NAME,
            owner_id=owner_id,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[RuntimeLockModel.name],
            set_={
                "owner_id": owner_id,
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": expires_at,
            },
            where=(RuntimeLockModel.expires_at <= now) | (RuntimeLockModel.owner_id == owner_id),
        )
        async with self._session_factory() as database, database.begin():
            result = cast(CursorResult[Any], await database.execute(statement))
            return result.rowcount == 1

    async def heartbeat(self, *, owner_id: str, now: datetime, lease_seconds: int) -> bool:
        self._validate(owner_id, now, lease_seconds)
        async with self._session_factory() as database, database.begin():
            result = cast(
                CursorResult[Any],
                await database.execute(
                    update(RuntimeLockModel)
                    .where(
                        RuntimeLockModel.name == LOCK_NAME,
                        RuntimeLockModel.owner_id == owner_id,
                        RuntimeLockModel.expires_at > now,
                    )
                    .values(
                        heartbeat_at=now,
                        expires_at=now + timedelta(seconds=lease_seconds),
                    )
                ),
            )
            return result.rowcount == 1

    async def release_lock(self, *, owner_id: str) -> None:
        async with self._session_factory() as database, database.begin():
            await database.execute(
                delete(RuntimeLockModel).where(
                    RuntimeLockModel.name == LOCK_NAME,
                    RuntimeLockModel.owner_id == owner_id,
                )
            )

    async def record_worker(self, run: WorkerRun) -> None:
        statement = sqlite_insert(RuntimeWorkerModel).values(
            name=run.name,
            state=run.state,
            last_started_at=run.started_at,
            last_succeeded_at=run.last_succeeded_at,
            last_error_code=run.last_error_code,
            consecutive_failures=run.consecutive_failures,
            next_run_at=run.next_run_at,
            last_duration_ms=run.duration_ms,
            updated_at=run.updated_at,
        )
        insert_statement = statement
        statement = insert_statement.on_conflict_do_update(
            index_elements=[RuntimeWorkerModel.name],
            set_={
                "state": run.state,
                "last_started_at": run.started_at,
                "last_succeeded_at": func.coalesce(
                    insert_statement.excluded.last_succeeded_at,
                    RuntimeWorkerModel.last_succeeded_at,
                ),
                "last_error_code": run.last_error_code,
                "consecutive_failures": run.consecutive_failures,
                "next_run_at": run.next_run_at,
                "last_duration_ms": run.duration_ms,
                "updated_at": run.updated_at,
            },
        )
        async with self._session_factory() as database, database.begin():
            await database.execute(statement)

    @staticmethod
    def _validate(owner_id: str, now: datetime, lease_seconds: int) -> None:
        if not owner_id.strip():
            raise ValueError("runtime owner ID must not be empty")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("runtime lock timestamp must be timezone-aware")
        if lease_seconds <= 0:
            raise ValueError("runtime lease must be positive")


class SqlAlchemyActiveSubscriptionSource:
    """Open a fresh SQLAlchemy session for every catalogue polling pass."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_enabled(self) -> tuple[Subscription, ...]:
        async with self._session_factory() as database:
            return await SubscriptionRepository(database).list_enabled()


class SqlAlchemyStatusReader:
    """Build status from durable queues and history, scoped to one Telegram owner."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def for_user(self, telegram_user_id: str, *, now: datetime) -> RuntimeStatus:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("status timestamp must be timezone-aware")
        async with self._session_factory() as database:
            buyer_id = await database.scalar(
                select(BuyerModel.id).where(BuyerModel.telegram_user_id == telegram_user_id)
            )
            last_catalogue = _aware(
                await database.scalar(
                    select(func.max(CatalogueSnapshotModel.fetched_at)).where(
                        CatalogueSnapshotModel.complete.is_(True)
                    )
                )
            )
            workers = (
                await database.scalars(select(RuntimeWorkerModel).order_by(RuntimeWorkerModel.name))
            ).all()
            if buyer_id is None:
                return RuntimeStatus(
                    last_complete_catalogue_at=last_catalogue,
                    pending_outbox=0,
                    oldest_outbox_at=None,
                    ambiguous_writes=0,
                    candidates=(),
                    workers=tuple(
                        (
                            worker.name,
                            worker.state,
                            _aware(worker.last_succeeded_at),
                            worker.last_error_code,
                        )
                        for worker in workers
                    ),
                )

            subscription_counts = (
                await database.execute(
                    select(SubscriptionModel.enabled, func.count())
                    .where(
                        SubscriptionModel.buyer_id == buyer_id,
                        SubscriptionModel.deleted_at.is_(None),
                    )
                    .group_by(SubscriptionModel.enabled)
                )
            ).all()
            active_subscriptions = sum(count for enabled, count in subscription_counts if enabled)
            paused_subscriptions = sum(
                count for enabled, count in subscription_counts if not enabled
            )

            pending_states = ("pending", "retry", "sending")
            pending_outbox, oldest_outbox = (
                await database.execute(
                    select(func.count(), func.min(OutboxMessageModel.created_at)).where(
                        OutboxMessageModel.buyer_id == buyer_id,
                        OutboxMessageModel.state.in_(pending_states),
                    )
                )
            ).one()
            ambiguous = await database.scalar(
                select(func.count(CheckoutIntentModel.id))
                .join(RenewalCycleModel)
                .join(CandidateModel)
                .where(
                    CandidateModel.buyer_id == buyer_id,
                    CheckoutIntentModel.state.in_(("submitting", "unknown")),
                )
            )
            latest_expiry = (
                select(func.max(OrderModel.expires_at))
                .join(CheckoutIntentModel)
                .join(RenewalCycleModel)
                .where(RenewalCycleModel.candidate_id == CandidateModel.id)
                .correlate(CandidateModel)
                .scalar_subquery()
            )
            candidate_rows = (
                await database.execute(
                    select(
                        CandidateModel,
                        SessionModel,
                        SubscriptionModel.enabled,
                        SubscriptionModel.deleted_at,
                        latest_expiry.label("expires_at"),
                    )
                    .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                    .join(SubscriptionModel, SubscriptionModel.id == CandidateModel.subscription_id)
                    .where(CandidateModel.buyer_id == buyer_id)
                    .order_by(SessionModel.starts_at, CandidateModel.id)
                )
            ).all()
            candidates = []
            for candidate, session, enabled, deleted_at, expires_at in candidate_rows:
                candidates.append(
                    CandidateStatus(
                        candidate.id,
                        session.title,
                        candidate.tracking_state,
                        candidate.current_cycle_no,
                        _aware(candidate.next_run_at),
                        candidate.stop_reason,
                        _aware(expires_at),
                        _aware(session.starts_at),
                        enabled and deleted_at is None,
                        _aware(candidate.watch_until),
                    )
                )
            return RuntimeStatus(
                last_complete_catalogue_at=last_catalogue,
                pending_outbox=int(pending_outbox or 0),
                oldest_outbox_at=_aware(oldest_outbox),
                ambiguous_writes=int(ambiguous or 0),
                candidates=tuple(candidates),
                workers=tuple(
                    (
                        worker.name,
                        worker.state,
                        _aware(worker.last_succeeded_at),
                        worker.last_error_code,
                    )
                    for worker in workers
                ),
                active_subscriptions=active_subscriptions,
                paused_subscriptions=paused_subscriptions,
            )
