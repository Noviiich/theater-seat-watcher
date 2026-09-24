"""Atomic local planning before a checkout adapter is allowed to write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import exists, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    CandidateModel,
    CheckoutIntentModel,
    RenewalCycleModel,
    SubscriptionModel,
)
from theater_tickets.domain.models import Money, Subscription


class PlanningState(StrEnum):
    """A result that lets a worker wait without performing an external write."""

    PLANNED = "planned"
    WAITING_BUDGET = "waiting_budget"
    SKIPPED_LIMIT = "skipped_limit"
    ALREADY_ACTIVE = "already_active"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """The durable result of one attempt to reserve local checkout capacity."""

    state: PlanningState
    candidate_id: str
    cycle_no: int | None = None
    checkout_intent_id: str | None = None


class BookingPlanner:
    """Reserve all local limits in the caller's short write transaction.

    This deliberately has no provider dependency.  A successful result means
    that an intent and its upper-bound allocation have both been committed
    before a later checkout adapter may issue a POST.
    """

    async def plan(
        self,
        database: AsyncSession,
        *,
        candidate_id: str,
        subscription: Subscription,
        reserved_total: Money,
        expected_total: Money | None = None,
        selected_seat_ids: tuple[str, ...],
        now: datetime,
        subscription_version: int | None = None,
    ) -> PlanningOutcome:
        if reserved_total.minor_units <= 0:
            raise ValueError("reserved_total must be positive")
        if len(selected_seat_ids) != subscription.ticket_count or len(
            set(selected_seat_ids)
        ) != len(selected_seat_ids):
            raise ValueError("selected_seat_ids must contain the requested unique seat count")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        quoted_total = expected_total or reserved_total
        if quoted_total.currency != reserved_total.currency or quoted_total > reserved_total:
            raise ValueError("expected_total must use one currency and fit reserved_total")

        claimed = cast(
            CursorResult[Any],
            await database.execute(
                update(CandidateModel)
                .where(
                    CandidateModel.id == candidate_id,
                    CandidateModel.subscription_id == subscription.subscription_id,
                    CandidateModel.tracking_state.in_(
                        ("queued", "waiting_budget", "renewal_claimed")
                    ),
                    exists(
                        select(SubscriptionModel.id).where(
                            SubscriptionModel.id == subscription.subscription_id,
                            SubscriptionModel.enabled.is_(True),
                            *(
                                (SubscriptionModel.version == subscription_version,)
                                if subscription_version is not None
                                else ()
                            ),
                        )
                    ),
                )
                .values(
                    tracking_state="planning",
                    next_run_at=None,
                    **(
                        {"subscription_version": subscription_version}
                        if subscription_version is not None
                        else {}
                    ),
                )
            ),
        )
        if claimed.rowcount != 1:
            candidate = await database.get(CandidateModel, candidate_id)
            if candidate is None or candidate.subscription_id != subscription.subscription_id:
                return PlanningOutcome(PlanningState.NOT_FOUND, candidate_id)
            return PlanningOutcome(PlanningState.ALREADY_ACTIVE, candidate_id)

        candidate = await database.get(CandidateModel, candidate_id)
        assert candidate is not None
        batch_sessions = await self._batch_session_count(database, candidate.discovery_batch_id)
        candidate_has_batch_slot = await database.scalar(
            select(BudgetAllocationModel.id).where(
                BudgetAllocationModel.discovery_batch_id == candidate.discovery_batch_id,
                BudgetAllocationModel.candidate_id == candidate.id,
            )
        )
        if (
            candidate_has_batch_slot is None
            and subscription.max_sessions_per_batch is not None
            and batch_sessions >= subscription.max_sessions_per_batch
        ):
            candidate.tracking_state = "skipped_limit"
            candidate.stop_reason = "max_sessions_per_batch"
            return PlanningOutcome(PlanningState.SKIPPED_LIMIT, candidate_id)

        active_count, active_total = await self._active_buyer_usage(database, candidate.buyer_id)
        if (
            subscription.max_active_orders is not None
            and active_count >= subscription.max_active_orders
        ) or (
            subscription.max_active_total is not None
            and active_total + reserved_total.minor_units
            > subscription.max_active_total.minor_units
        ):
            candidate.tracking_state = "waiting_budget"
            return PlanningOutcome(PlanningState.WAITING_BUDGET, candidate_id)

        batch_total = await self._active_batch_total(database, candidate.discovery_batch_id)
        if subscription.max_batch_total is not None and (
            batch_total + reserved_total.minor_units > subscription.max_batch_total.minor_units
        ):
            candidate.tracking_state = "waiting_budget"
            return PlanningOutcome(PlanningState.WAITING_BUDGET, candidate_id)

        cycle_no = candidate.current_cycle_no + 1
        cycle = RenewalCycleModel(
            id=str(uuid4()),
            candidate_id=candidate.id,
            cycle_no=cycle_no,
            state="submitting",
            started_at=now,
            created_at=now,
        )
        database.add(cycle)
        await database.flush()
        intent = CheckoutIntentModel(
            id=str(uuid4()),
            renewal_cycle_id=cycle.id,
            attempt_no=1,
            state="pending",
            selected_seat_ids=list(selected_seat_ids),
            reserved_total_minor=reserved_total.minor_units,
            expected_total_minor=quoted_total.minor_units,
            currency=quoted_total.currency,
            expected_hold_ttl_seconds=subscription.renewal_policy.expected_hold_ttl_seconds,
            created_at=now,
        )
        database.add(intent)
        await database.flush()
        allocation = BudgetAllocationModel(
            id=str(uuid4()),
            buyer_id=candidate.buyer_id,
            discovery_batch_id=candidate.discovery_batch_id,
            candidate_id=candidate.id,
            checkout_intent_id=intent.id,
            reserved_total_minor=reserved_total.minor_units,
            active=True,
            created_at=now,
        )
        database.add(allocation)
        candidate.current_cycle_no = cycle_no
        candidate.tracking_state = "submitting"
        return PlanningOutcome(PlanningState.PLANNED, candidate_id, cycle_no, intent.id)

    async def release_cycle_allocation(
        self, database: AsyncSession, *, candidate_id: str, cycle_no: int, now: datetime
    ) -> bool:
        """Locally expire one cycle without interpreting it as a payment status."""
        cycle = await database.scalar(
            select(RenewalCycleModel).where(
                RenewalCycleModel.candidate_id == candidate_id,
                RenewalCycleModel.cycle_no == cycle_no,
            )
        )
        if cycle is None or cycle.allocation_released_at is not None:
            return False
        intent = await database.scalar(
            select(CheckoutIntentModel).where(CheckoutIntentModel.renewal_cycle_id == cycle.id)
        )
        if intent is None:
            return False
        allocation = await database.scalar(
            select(BudgetAllocationModel).where(
                BudgetAllocationModel.checkout_intent_id == intent.id
            )
        )
        if allocation is None:
            return False
        allocation.active = False
        allocation.released_at = now
        cycle.allocation_released_at = now
        cycle.state = "renewal_waiting"
        candidate = await database.get(CandidateModel, candidate_id)
        assert candidate is not None
        candidate.tracking_state = "queued"
        return True

    async def _active_buyer_usage(self, database: AsyncSession, buyer_id: str) -> tuple[int, int]:
        row = await database.execute(
            select(
                func.count(BudgetAllocationModel.id),
                func.coalesce(func.sum(BudgetAllocationModel.reserved_total_minor), 0),
            ).where(
                BudgetAllocationModel.buyer_id == buyer_id, BudgetAllocationModel.active.is_(True)
            )
        )
        count, total = row.one()
        return int(count), int(total)

    async def _active_batch_total(self, database: AsyncSession, batch_id: str) -> int:
        return int(
            await database.scalar(
                select(
                    func.coalesce(func.sum(BudgetAllocationModel.reserved_total_minor), 0)
                ).where(
                    BudgetAllocationModel.discovery_batch_id == batch_id,
                    BudgetAllocationModel.active.is_(True),
                )
            )
        )

    async def _batch_session_count(self, database: AsyncSession, batch_id: str) -> int:
        return int(
            await database.scalar(
                select(func.count(func.distinct(BudgetAllocationModel.candidate_id))).where(
                    BudgetAllocationModel.discovery_batch_id == batch_id
                )
            )
        )
