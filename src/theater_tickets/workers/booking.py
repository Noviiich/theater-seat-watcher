"""One-shot orchestration of selection, checkout, renewals and notification."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.outbox import SqlAlchemyOrderOutboxWriter
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.booking import (
    BatchSummaryScheduler,
    BookingCandidateContext,
    CandidateContextRepository,
    CandidateEvaluator,
    CheckoutFailureRepository,
    CheckoutSubmitter,
    DryRunReport,
    DryRunRepository,
    EvaluationState,
    ResumableCheckoutRepository,
    StartupRecovery,
)
from theater_tickets.application.checkout import (
    CheckoutErrorCode,
    CheckoutRequest,
    CheckoutState,
)
from theater_tickets.application.discovery import DiscoveryService
from theater_tickets.application.planning import BookingPlanner, PlanningState
from theater_tickets.application.reconciliation import RecoveryRequestLoader
from theater_tickets.application.renewals import (
    RenewalProcessResult,
    RenewalProcessState,
    RenewalRepository,
    RenewalTask,
)
from theater_tickets.application.runtime import RuntimeEventLog
from theater_tickets.domain.models import Session, Subscription
from theater_tickets.workers.outbox import OutboxWorker
from theater_tickets.workers.renewals import RenewalWorker


class BuyerCheckoutLocks:
    """Serialize use of each buyer's provider checkout context in one process."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def for_buyer(self, buyer_id: str) -> asyncio.Lock:
        return self._locks.setdefault(buyer_id, asyncio.Lock())


class LiveCandidateProcessor:
    """Implement one live renewal task without hiding any external write retry."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        contexts: CandidateContextRepository,
        evaluator: CandidateEvaluator,
        checkout: CheckoutSubmitter,
        order_writer: SqlAlchemyOrderOutboxWriter,
        failures: CheckoutFailureRepository,
        now: Callable[[], datetime],
        locks: BuyerCheckoutLocks | None = None,
        event_log: RuntimeEventLog | None = None,
        outbox_wake_event: asyncio.Event | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._contexts = contexts
        self._evaluator = evaluator
        self._checkout = checkout
        self._order_writer = order_writer
        self._failures = failures
        self._now = now
        self._locks = locks or BuyerCheckoutLocks()
        self._event_log = event_log
        self._outbox_wake_event = outbox_wake_event

    async def process(self, task: RenewalTask) -> RenewalProcessResult:
        context = await self._contexts.load(task.candidate_id)
        if context is None:
            return RenewalProcessResult(
                RenewalProcessState.STOPPED, stop_reason="candidate_missing"
            )
        async with self._locks.for_buyer(context.buyer_id):
            return await self._process_locked(task, context)

    async def _process_locked(
        self, task: RenewalTask, context: BookingCandidateContext
    ) -> RenewalProcessResult:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("booking processor clock must return an aware timestamp")
        evaluation_started = monotonic()
        try:
            evaluation = await self._evaluator.evaluate(context, now=now)
        except (LookupError, ValueError) as exc:
            if self._event_log is not None:
                self._event_log.emit(
                    "seat_evaluation",
                    candidate_id=context.candidate_id,
                    duration_ms=round((monotonic() - evaluation_started) * 1000),
                    state="error",
                    error_code=type(exc).__name__.casefold(),
                )
            return RenewalProcessResult(
                RenewalProcessState.NEEDS_ATTENTION,
                stop_reason=type(exc).__name__.casefold(),
            )
        if self._event_log is not None:
            self._event_log.emit(
                "seat_evaluation",
                candidate_id=context.candidate_id,
                duration_ms=round((monotonic() - evaluation_started) * 1000),
                state=evaluation.state.value,
            )
        if evaluation.state is EvaluationState.PAUSED:
            return RenewalProcessResult(RenewalProcessState.PAUSED)
        if evaluation.state is EvaluationState.WAITING_AVAILABILITY:
            return RenewalProcessResult(RenewalProcessState.WAITING_AVAILABILITY)
        if evaluation.state is EvaluationState.STOPPED:
            return RenewalProcessResult(
                RenewalProcessState.STOPPED,
                stop_reason=evaluation.reason,
            )
        if evaluation.state is EvaluationState.NEEDS_ATTENTION:
            return RenewalProcessResult(
                RenewalProcessState.NEEDS_ATTENTION,
                stop_reason=evaluation.reason,
            )
        assert evaluation.group is not None and evaluation.session is not None
        if context.subscription.booking_mode.value != "live":
            return RenewalProcessResult(
                RenewalProcessState.NEEDS_ATTENTION,
                stop_reason="booking_mode_mismatch",
            )
        if (
            context.subscription.max_ticket_price is None
            or context.subscription.max_order_total is None
        ):
            return RenewalProcessResult(
                RenewalProcessState.NEEDS_ATTENTION,
                stop_reason="booking_limits_missing",
            )
        if context.buyer is None:
            return RenewalProcessResult(
                RenewalProcessState.NEEDS_ATTENTION,
                stop_reason="buyer_profile_missing",
            )
        buyer = context.buyer

        group = evaluation.group.group
        reserved_total = context.subscription.max_order_total or group.total
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            planned = await BookingPlanner().plan(
                uow.session,
                candidate_id=context.candidate_id,
                subscription=context.subscription,
                reserved_total=reserved_total,
                expected_total=group.total,
                selected_seat_ids=tuple(seat.provider_id for seat in group.seats),
                now=now,
                subscription_version=context.subscription_version,
            )
        if planned.state is PlanningState.WAITING_BUDGET:
            return RenewalProcessResult(RenewalProcessState.WAITING_BUDGET)
        if planned.state is PlanningState.SKIPPED_LIMIT:
            return RenewalProcessResult(
                RenewalProcessState.STOPPED,
                stop_reason="max_sessions_per_batch",
            )
        if planned.state is not PlanningState.PLANNED:
            return RenewalProcessResult(RenewalProcessState.ALREADY_ACTIVE)
        assert planned.checkout_intent_id is not None and planned.cycle_no is not None
        request = CheckoutRequest(
            intent_id=planned.checkout_intent_id,
            session_key=evaluation.session.key,
            seat_ids=tuple(seat.provider_id for seat in group.seats),
            expected_total=group.total,
            reserved_total=reserved_total,
            expected_hold_ttl_seconds=(
                context.subscription.renewal_policy.expected_hold_ttl_seconds
            ),
            buyer=buyer,
        )
        checkout_started = monotonic()
        result = await self._checkout.submit(request)
        if self._event_log is not None:
            self._event_log.emit(
                "checkout_submit",
                candidate_id=context.candidate_id,
                intent_id=request.intent_id,
                duration_ms=round((monotonic() - checkout_started) * 1000),
                state=result.state.value,
            )
        if result.state is CheckoutState.CONFIRMED:
            assert result.order is not None
            record_started = monotonic()
            await self._order_writer.record_confirmed_order(
                intent_id=request.intent_id,
                order=result.order,
                recorded_at=self._now(),
            )
            if self._outbox_wake_event is not None:
                self._outbox_wake_event.set()
            if self._event_log is not None:
                self._event_log.emit(
                    "order_recorded",
                    candidate_id=context.candidate_id,
                    intent_id=request.intent_id,
                    duration_ms=round((monotonic() - record_started) * 1000),
                )
            return RenewalProcessResult(
                RenewalProcessState.HELD,
                cycle_no=planned.cycle_no,
                held_at=result.order.held_at,
            )
        error_code = (result.error_code or CheckoutErrorCode.CONTRACT_CHANGED).value
        if result.state is CheckoutState.REJECTED:
            await self._failures.rejected(
                intent_id=request.intent_id,
                error_code=error_code,
                now=now,
                retry_seconds=context.subscription.renewal_policy.availability_retry_seconds,
                stop=(
                    task.max_cycles_per_session is not None
                    and planned.cycle_no >= task.max_cycles_per_session
                ),
            )
            return RenewalProcessResult(RenewalProcessState.ALREADY_ACTIVE)
        await self._failures.needs_attention(
            intent_id=request.intent_id,
            error_code=error_code,
            now=now,
        )
        return RenewalProcessResult(
            RenewalProcessState.NEEDS_ATTENTION,
            stop_reason=error_code,
        )


class DryRunWorker:
    """Save the same read/selection decision without intent, allocation or POST."""

    def __init__(
        self,
        *,
        repository: DryRunRepository,
        contexts: CandidateContextRepository,
        evaluator: CandidateEvaluator,
        batch_size: int = 20,
    ) -> None:
        self._repository = repository
        self._contexts = contexts
        self._evaluator = evaluator
        self._batch_size = batch_size

    async def recover_startup(self) -> int:
        return await self._repository.recover_claims()

    async def run_once(self, *, now: datetime) -> int:
        candidate_ids = await self._repository.claim(now=now, limit=self._batch_size)
        for candidate_id in candidate_ids:
            context = await self._contexts.load(candidate_id)
            if context is None:
                await self._repository.release(candidate_id)
                continue
            try:
                evaluation = await self._evaluator.evaluate(context, now=now)
            except Exception:
                await self._repository.release(candidate_id)
                continue
            if evaluation.state is EvaluationState.READY:
                assert evaluation.group is not None
                group = evaluation.group.group
                report = DryRunReport(
                    candidate_id,
                    "would_book",
                    tuple(seat.provider_id for seat in group.seats),
                    group.total,
                    evaluation.group.explanation,
                )
            else:
                report = DryRunReport(
                    candidate_id,
                    evaluation.reason,
                    explanation=evaluation.reason,
                )
            await self._repository.save(report, now=now)
        return len(candidate_ids)


class CheckoutResumeWorker:
    """Continue only intents proven safe to retry by startup reconciliation."""

    def __init__(
        self,
        *,
        repository: ResumableCheckoutRepository,
        request_loader: RecoveryRequestLoader,
        checkout: CheckoutSubmitter,
        order_writer: SqlAlchemyOrderOutboxWriter,
        failures: CheckoutFailureRepository,
        renewals: RenewalRepository,
        batch_size: int = 20,
        outbox_wake_event: asyncio.Event | None = None,
    ) -> None:
        self._repository = repository
        self._request_loader = request_loader
        self._checkout = checkout
        self._order_writer = order_writer
        self._failures = failures
        self._renewals = renewals
        self._batch_size = batch_size
        self._outbox_wake_event = outbox_wake_event

    async def run_once(self, *, now: datetime) -> int:
        intent_ids = await self._repository.list_resumable(limit=self._batch_size, now=now)
        for intent_id in intent_ids:
            request = await self._request_loader.load(intent_id)
            task, cycle_no = await self._repository.renewal_task(intent_id)
            result = await self._checkout.submit(request)
            if result.state is CheckoutState.CONFIRMED:
                assert result.order is not None
                await self._order_writer.record_confirmed_order(
                    intent_id=intent_id,
                    order=result.order,
                    recorded_at=now,
                )
                if self._outbox_wake_event is not None:
                    self._outbox_wake_event.set()
                await self._renewals.schedule_after_hold(
                    task=task,
                    cycle_no=cycle_no,
                    held_at=result.order.held_at,
                )
                continue
            error_code = (result.error_code or CheckoutErrorCode.CONTRACT_CHANGED).value
            if result.state is CheckoutState.REJECTED:
                await self._failures.rejected(
                    intent_id=intent_id,
                    error_code=error_code,
                    now=now,
                    retry_seconds=task.availability_retry_seconds,
                    stop=(
                        task.max_cycles_per_session is not None
                        and cycle_no >= task.max_cycles_per_session
                    ),
                )
            else:
                await self._failures.needs_attention(
                    intent_id=intent_id,
                    error_code=error_code,
                    now=now,
                )
        return len(intent_ids)


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    discovered_candidates: tuple[str, ...]
    live_processed: int
    dry_run_processed: int
    notifications_processed: int


class BookingWorkflow:
    """Run one complete snapshot or due-cycle pass without owning a polling loop."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        renewal_worker: RenewalWorker,
        dry_run_worker: DryRunWorker,
        outbox_worker: OutboxWorker,
        summaries: BatchSummaryScheduler,
        checkout_recovery: StartupRecovery | None = None,
        resume_worker: CheckoutResumeWorker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._renewal_worker = renewal_worker
        self._dry_run_worker = dry_run_worker
        self._outbox_worker = outbox_worker
        self._summaries = summaries
        self._checkout_recovery = checkout_recovery
        self._resume_worker = resume_worker

    async def process_snapshot(
        self,
        *,
        subscription: Subscription,
        sessions: tuple[Session, ...],
        fetched_at: datetime,
        fingerprint: str,
        complete: bool,
    ) -> WorkflowOutcome:
        async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
            assert uow.session is not None
            discovery = await DiscoveryService().process(
                uow.session,
                subscription=subscription,
                sessions=sessions,
                fetched_at=fetched_at,
                fingerprint=fingerprint,
                complete=complete,
            )
        live = await self._renewal_worker.run_once(now=fetched_at)
        dry = await self._dry_run_worker.run_once(now=fetched_at)
        await self._summaries.enqueue_for_candidates(
            discovery.candidate_ids,
            now=fetched_at,
        )
        notifications = await self._outbox_worker.run_once(now=fetched_at)
        return WorkflowOutcome(discovery.candidate_ids, live, dry, notifications)

    async def run_due(self, *, now: datetime) -> WorkflowOutcome:
        booking = await self.run_booking_due(now=now)
        notifications = await self.run_outbox_due(now=now)
        return WorkflowOutcome(
            (),
            booking.live_processed,
            booking.dry_run_processed,
            notifications,
        )

    async def run_booking_due(self, *, now: datetime) -> WorkflowOutcome:
        """Process booking state without waiting for Telegram delivery."""
        if self._resume_worker is not None:
            await self._resume_worker.run_once(now=now)
        live = await self._renewal_worker.run_once(now=now)
        dry = await self._dry_run_worker.run_once(now=now)
        await self._summaries.enqueue_pending(now=now)
        return WorkflowOutcome((), live, dry, 0)

    async def run_outbox_due(self, *, now: datetime) -> int:
        """Deliver notifications independently from provider polling and checkout."""
        return await self._outbox_worker.run_once(now=now)

    async def recover_startup(self, *, now: datetime) -> None:
        if self._checkout_recovery is not None:
            await self._checkout_recovery.recover_startup()
        await self._renewal_worker.recover_startup()
        await self._dry_run_worker.recover_startup()
        await self._outbox_worker.recover_startup(now=now)
