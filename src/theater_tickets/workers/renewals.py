"""One-shot persisted renewal scheduler; the outer runtime controls polling."""

from __future__ import annotations

from datetime import datetime

from theater_tickets.application.renewals import (
    RenewalCandidateProcessor,
    RenewalProcessState,
    RenewalRepository,
    RenewalWaitReason,
)


class RenewalWorker:
    """Process each atomically claimed candidate at most once per invocation."""

    def __init__(
        self,
        *,
        repository: RenewalRepository,
        processor: RenewalCandidateProcessor,
        batch_size: int = 20,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._repository = repository
        self._processor = processor
        self._batch_size = batch_size

    async def run_once(self, *, now: datetime) -> int:
        """Run due work once; missed historical ticks are never replayed."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        tasks = await self._repository.claim_due(now=now, limit=self._batch_size)
        for task in tasks:
            try:
                result = await self._processor.process(task)
            except Exception:
                await self._repository.schedule_wait(
                    task=task,
                    reason=RenewalWaitReason.TRANSIENT_ERROR,
                    now=now,
                )
                continue
            if result.state is RenewalProcessState.HELD:
                assert result.cycle_no is not None and result.held_at is not None
                await self._repository.schedule_after_hold(
                    task=task,
                    cycle_no=result.cycle_no,
                    held_at=result.held_at,
                )
            elif result.state is RenewalProcessState.WAITING_AVAILABILITY:
                await self._repository.schedule_wait(
                    task=task,
                    reason=RenewalWaitReason.AVAILABILITY,
                    now=now,
                )
            elif result.state is RenewalProcessState.WAITING_BUDGET:
                await self._repository.schedule_wait(
                    task=task,
                    reason=RenewalWaitReason.BUDGET,
                    now=now,
                )
            elif result.state is RenewalProcessState.TRANSIENT_ERROR:
                await self._repository.schedule_wait(
                    task=task,
                    reason=RenewalWaitReason.TRANSIENT_ERROR,
                    now=now,
                )
            elif result.state is RenewalProcessState.NEEDS_ATTENTION:
                await self._repository.mark_needs_attention(task=task)
            elif result.state is RenewalProcessState.STOPPED:
                await self._repository.stop(
                    task=task,
                    reason=result.stop_reason or "processor_stopped",
                )
            else:
                await self._repository.release_claim(task=task)
        return len(tasks)

    async def recover_startup(self) -> int:
        """Make pre-processing claims from a crashed process runnable again."""
        return await self._repository.recover_claims()
