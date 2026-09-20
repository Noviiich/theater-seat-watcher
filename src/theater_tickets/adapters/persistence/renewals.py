"""SQLite-backed scheduling state for repeated booking cycles."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    CandidateModel,
    CheckoutIntentModel,
    RenewalCycleModel,
    SubscriptionModel,
)
from theater_tickets.application.renewals import (
    RenewalTask,
    RenewalWaitReason,
    renewal_due_at,
    retry_due_at,
)

_RUNNABLE_STATES = (
    "queued",
    "waiting_availability",
    "waiting_budget",
    "renewal_waiting",
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _integer(config: dict[str, object], name: str, default: int) -> int:
    value = config.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


class SqlAlchemyRenewalRepository:
    """Claim due work and persist every scheduler transition in short transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def recover_claims(self) -> int:
        async with self._session_factory() as database, database.begin():
            result = cast(
                CursorResult[Any],
                await database.execute(
                    update(CandidateModel)
                    .where(CandidateModel.tracking_state == "renewal_claimed")
                    .values(tracking_state="queued", next_run_at=None)
                ),
            )
            return result.rowcount

    async def claim_due(self, *, now: datetime, limit: int) -> tuple[RenewalTask, ...]:
        self._validate_time(now)
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._session_factory() as database, database.begin():
            await self._release_elapsed_allocations(database, now=now)
            due_ids = (
                select(CandidateModel.id)
                .join(
                    SubscriptionModel,
                    SubscriptionModel.id == CandidateModel.subscription_id,
                )
                .where(
                    SubscriptionModel.enabled.is_(True),
                    CandidateModel.tracking_state.in_(_RUNNABLE_STATES),
                    or_(
                        CandidateModel.next_run_at.is_(None),
                        CandidateModel.next_run_at <= now,
                    ),
                )
                .order_by(CandidateModel.next_run_at, CandidateModel.id)
                .limit(limit)
            )
            claimed_ids = tuple(
                (
                    await database.execute(
                        update(CandidateModel)
                        .where(
                            CandidateModel.id.in_(due_ids),
                            CandidateModel.tracking_state.in_(_RUNNABLE_STATES),
                        )
                        .values(tracking_state="renewal_claimed")
                        .returning(CandidateModel.id)
                    )
                ).scalars()
            )
            if not claimed_ids:
                return ()
            rows = (
                await database.execute(
                    select(CandidateModel, SubscriptionModel)
                    .join(
                        SubscriptionModel,
                        SubscriptionModel.id == CandidateModel.subscription_id,
                    )
                    .where(
                        CandidateModel.id.in_(claimed_ids),
                        CandidateModel.tracking_state == "renewal_claimed",
                    )
                    .order_by(CandidateModel.next_run_at, CandidateModel.id)
                )
            ).all()
            tasks: list[RenewalTask] = []
            for candidate, subscription in rows:
                if len(tasks) >= limit:
                    break
                config = subscription.config
                interval = _integer(config, "renewal_interval_seconds", 1200)
                retry = _integer(config, "availability_retry_seconds", 180)
                maximum_value = config.get("max_cycles_per_session")
                maximum = (
                    maximum_value
                    if isinstance(maximum_value, int) and not isinstance(maximum_value, bool)
                    else None
                )
                due_at = _aware(candidate.next_run_at)
                watch_until = _aware(candidate.watch_until)
                if watch_until is not None and watch_until <= now:
                    self._stop_candidate(candidate, "watch_until")
                    continue
                if maximum is not None and candidate.current_cycle_no >= maximum:
                    self._stop_candidate(candidate, "max_cycles_per_session")
                    continue
                tasks.append(
                    RenewalTask(
                        candidate_id=candidate.id,
                        subscription_id=subscription.id,
                        previous_cycle_no=candidate.current_cycle_no,
                        due_at=due_at,
                        renewal_interval_seconds=interval,
                        availability_retry_seconds=retry,
                        max_cycles_per_session=maximum,
                    )
                )
            return tuple(tasks)

    async def schedule_after_hold(
        self,
        *,
        task: RenewalTask,
        cycle_no: int,
        held_at: datetime,
    ) -> None:
        self._validate_time(held_at)
        if cycle_no != task.previous_cycle_no + 1:
            raise ValueError("successful renewal must advance cycle_no exactly once")
        due_at = renewal_due_at(held_at, task.renewal_interval_seconds)
        async with self._session_factory() as database, database.begin():
            candidate = await database.get(CandidateModel, task.candidate_id)
            cycle = await database.scalar(
                select(RenewalCycleModel).where(
                    RenewalCycleModel.candidate_id == task.candidate_id,
                    RenewalCycleModel.cycle_no == cycle_no,
                )
            )
            if candidate is None or cycle is None or candidate.current_cycle_no != cycle_no:
                raise LookupError("renewal cycle is not current")
            cycle.started_at = held_at
            cycle.due_at = due_at
            if task.max_cycles_per_session is not None and cycle_no >= task.max_cycles_per_session:
                cycle.state = "held"
                self._stop_candidate(candidate, "max_cycles_per_session")
                return
            candidate.tracking_state = "renewal_waiting"
            candidate.next_run_at = due_at
            candidate.stop_reason = None
            cycle.state = "renewal_waiting"

    async def schedule_wait(
        self,
        *,
        task: RenewalTask,
        reason: RenewalWaitReason,
        now: datetime,
    ) -> None:
        self._validate_time(now)
        async with self._session_factory() as database, database.begin():
            candidate = await database.get(CandidateModel, task.candidate_id)
            if candidate is None or candidate.current_cycle_no != task.previous_cycle_no:
                return
            due_at = retry_due_at(now, task.availability_retry_seconds)
            watch_until = _aware(candidate.watch_until)
            if watch_until is not None and due_at >= watch_until:
                self._stop_candidate(candidate, "watch_until")
                return
            candidate.tracking_state = (
                "waiting_budget" if reason is RenewalWaitReason.BUDGET else "waiting_availability"
            )
            candidate.next_run_at = due_at
            candidate.stop_reason = (
                reason.value if reason is RenewalWaitReason.TRANSIENT_ERROR else None
            )

    async def mark_needs_attention(self, *, task: RenewalTask) -> None:
        async with self._session_factory() as database, database.begin():
            candidate = await database.get(CandidateModel, task.candidate_id)
            if candidate is None:
                return
            candidate.tracking_state = "needs_attention"
            candidate.next_run_at = None
            candidate.stop_reason = "ambiguous_checkout"
            cycle = await self._current_cycle(database, candidate)
            if cycle is not None:
                cycle.state = "needs_attention"

    async def stop(self, *, task: RenewalTask, reason: str) -> None:
        async with self._session_factory() as database, database.begin():
            candidate = await database.get(CandidateModel, task.candidate_id)
            if candidate is not None:
                self._stop_candidate(candidate, reason)

    async def release_claim(self, *, task: RenewalTask) -> None:
        async with self._session_factory() as database, database.begin():
            await database.execute(
                update(CandidateModel)
                .where(
                    CandidateModel.id == task.candidate_id,
                    CandidateModel.tracking_state == "renewal_claimed",
                    CandidateModel.current_cycle_no == task.previous_cycle_no,
                )
                .values(tracking_state="queued", next_run_at=None)
            )

    async def _release_elapsed_allocations(self, database: AsyncSession, *, now: datetime) -> None:
        cycles = (
            await database.scalars(
                select(RenewalCycleModel).where(
                    RenewalCycleModel.due_at.is_not(None),
                    RenewalCycleModel.due_at <= now,
                    RenewalCycleModel.allocation_released_at.is_(None),
                )
            )
        ).all()
        for cycle in cycles:
            intent_ids = select(CheckoutIntentModel.id).where(
                CheckoutIntentModel.renewal_cycle_id == cycle.id
            )
            await database.execute(
                update(BudgetAllocationModel)
                .where(
                    BudgetAllocationModel.checkout_intent_id.in_(intent_ids),
                    BudgetAllocationModel.active.is_(True),
                )
                .values(active=False, released_at=now)
            )
            cycle.allocation_released_at = now
            cycle.ended_at = now
            if cycle.state == "renewal_waiting":
                cycle.state = "locally_elapsed"

    async def _current_cycle(
        self, database: AsyncSession, candidate: CandidateModel
    ) -> RenewalCycleModel | None:
        if candidate.current_cycle_no == 0:
            return None
        cycle = await database.scalar(
            select(RenewalCycleModel).where(
                RenewalCycleModel.candidate_id == candidate.id,
                RenewalCycleModel.cycle_no == candidate.current_cycle_no,
            )
        )
        return cycle if isinstance(cycle, RenewalCycleModel) else None

    @staticmethod
    def _stop_candidate(candidate: CandidateModel, reason: str) -> None:
        candidate.tracking_state = "stopped"
        candidate.next_run_at = None
        candidate.stop_reason = reason

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler timestamps must be timezone-aware")
