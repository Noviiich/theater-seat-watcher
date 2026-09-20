from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from theater_tickets.application.renewals import (
    RenewalProcessResult,
    RenewalProcessState,
    RenewalTask,
    renewal_due_at,
    retry_due_at,
)
from theater_tickets.workers.renewals import RenewalWorker


def test_renewal_and_retry_deadlines_are_exact() -> None:
    held_at = datetime(2026, 9, 20, 9, tzinfo=UTC)

    assert renewal_due_at(held_at, 1200) == held_at + timedelta(seconds=1200)
    assert retry_due_at(held_at, 180) == held_at + timedelta(seconds=180)


def test_renewal_result_requires_complete_hold_timing() -> None:
    with pytest.raises(ValueError, match="requires cycle_no and held_at"):
        RenewalProcessResult(RenewalProcessState.HELD, cycle_no=1)


def test_worker_turns_processor_exception_into_persisted_retry() -> None:
    now = datetime(2026, 9, 20, 9, tzinfo=UTC)
    task = RenewalTask("candidate", "subscription", 0, None, 1200, 180, None)

    class Repository:
        wait_reason: object | None = None

        async def recover_claims(self) -> int:
            return 0

        async def claim_due(self, *, now: datetime, limit: int) -> tuple[RenewalTask, ...]:
            return (task,)

        async def schedule_after_hold(self, **kwargs: object) -> None:
            raise AssertionError

        async def schedule_wait(self, **kwargs: object) -> None:
            self.wait_reason = kwargs["reason"]

        async def mark_needs_attention(self, **kwargs: object) -> None:
            raise AssertionError

        async def stop(self, **kwargs: object) -> None:
            raise AssertionError

        async def release_claim(self, **kwargs: object) -> None:
            raise AssertionError

    class Processor:
        async def process(self, task: RenewalTask) -> RenewalProcessResult:
            raise RuntimeError("temporary provider failure")

    repository = Repository()
    worker = RenewalWorker(repository=repository, processor=Processor())

    assert asyncio.run(worker.run_once(now=now)) == 1
    assert str(repository.wait_reason) == "transient_error"
