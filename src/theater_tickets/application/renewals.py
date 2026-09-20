"""Provider-neutral scheduling contracts for repeated booking cycles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class RenewalWaitReason(StrEnum):
    AVAILABILITY = "availability"
    BUDGET = "budget"
    TRANSIENT_ERROR = "transient_error"


class RenewalProcessState(StrEnum):
    HELD = "held"
    WAITING_AVAILABILITY = "waiting_availability"
    WAITING_BUDGET = "waiting_budget"
    TRANSIENT_ERROR = "transient_error"
    ALREADY_ACTIVE = "already_active"
    NEEDS_ATTENTION = "needs_attention"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RenewalTask:
    candidate_id: str
    subscription_id: str
    previous_cycle_no: int
    due_at: datetime | None
    renewal_interval_seconds: int
    availability_retry_seconds: int
    max_cycles_per_session: int | None


@dataclass(frozen=True, slots=True)
class RenewalProcessResult:
    state: RenewalProcessState
    cycle_no: int | None = None
    held_at: datetime | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        held = self.state is RenewalProcessState.HELD
        if held != (self.cycle_no is not None and self.held_at is not None):
            raise ValueError("held renewal result requires cycle_no and held_at")
        if self.held_at is not None and (
            self.held_at.tzinfo is None or self.held_at.utcoffset() is None
        ):
            raise ValueError("held_at must be timezone-aware")


class RenewalRepository(Protocol):
    async def recover_claims(self) -> int:
        """Return scheduler-only claims left by an interrupted process to the queue."""

    async def claim_due(self, *, now: datetime, limit: int) -> tuple[RenewalTask, ...]:
        """Atomically claim due candidates and release elapsed local allocations."""

    async def schedule_after_hold(
        self,
        *,
        task: RenewalTask,
        cycle_no: int,
        held_at: datetime,
    ) -> None:
        """Schedule one next cycle from actual hold start, not delivery time."""

    async def schedule_wait(
        self,
        *,
        task: RenewalTask,
        reason: RenewalWaitReason,
        now: datetime,
    ) -> None:
        """Persist a short availability/budget retry without consuming a cycle."""

    async def mark_needs_attention(self, *, task: RenewalTask) -> None:
        """Block automatic writes while preserving local allocations."""

    async def stop(self, *, task: RenewalTask, reason: str) -> None:
        """Stop future cycles for one candidate without cancelling an order."""

    async def release_claim(self, *, task: RenewalTask) -> None:
        """Return an unused claim to the immediate queue."""


class RenewalCandidateProcessor(Protocol):
    async def process(self, task: RenewalTask) -> RenewalProcessResult:
        """Re-read inventory, select seats, check limits and attempt one new cycle."""


def renewal_due_at(held_at: datetime, interval_seconds: int) -> datetime:
    if held_at.tzinfo is None or held_at.utcoffset() is None:
        raise ValueError("held_at must be timezone-aware")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    return held_at + timedelta(seconds=interval_seconds)


def retry_due_at(now: datetime, retry_seconds: int) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if retry_seconds <= 0:
        raise ValueError("retry_seconds must be positive")
    return now + timedelta(seconds=retry_seconds)
