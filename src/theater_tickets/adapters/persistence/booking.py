"""Persistence boundaries used by the one-shot booking orchestration workers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    BuyerModel,
    CandidateModel,
    CheckoutIntentModel,
    DryRunReportModel,
    OrderModel,
    RenewalCycleModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.outbox import SqlAlchemyOrderOutboxWriter
from theater_tickets.adapters.persistence.renewals import SqlAlchemyRenewalRepository
from theater_tickets.adapters.persistence.repositories import _subscription_from_model
from theater_tickets.adapters.quicktickets.buyer import validate_buyer_profile
from theater_tickets.application.booking import BookingCandidateContext, DryRunReport
from theater_tickets.application.checkout import CheckoutBuyer, ConfirmedOrder
from theater_tickets.application.outbox import BatchSessionResult
from theater_tickets.application.renewals import RenewalTask
from theater_tickets.domain.models import Session, SessionKey


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _checkout_buyer(buyer: BuyerModel) -> CheckoutBuyer | None:
    lastname, firstname, middlename, email, phone = (
        buyer.lastname,
        buyer.firstname,
        buyer.middlename,
        buyer.email,
        buyer.phone,
    )
    if (
        lastname is None
        or firstname is None
        or middlename is None
        or email is None
        or phone is None
    ):
        return None
    try:
        return validate_buyer_profile(
            lastname=lastname,
            firstname=firstname,
            middlename=middlename,
            email=email,
            phone=phone,
            personal_data_consent=buyer.personal_data_consent,
        )
    except ValueError:
        return None


class SqlAlchemyBookingRepository:
    """Load candidates, persist dry-run decisions and finalize failed checkout attempts."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(self, candidate_id: str) -> BookingCandidateContext | None:
        async with self._session_factory() as database:
            row = (
                await database.execute(
                    select(
                        CandidateModel,
                        SubscriptionModel,
                        BuyerModel,
                        SessionModel,
                    )
                    .join(
                        SubscriptionModel,
                        SubscriptionModel.id == CandidateModel.subscription_id,
                    )
                    .join(BuyerModel, BuyerModel.id == CandidateModel.buyer_id)
                    .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                    .where(CandidateModel.id == candidate_id)
                )
            ).one_or_none()
        if row is None:
            return None
        candidate, subscription, buyer, session = row
        return BookingCandidateContext(
            candidate_id=candidate.id,
            buyer_id=buyer.id,
            subscription_version=subscription.version,
            buyer=_checkout_buyer(buyer),
            subscription=_subscription_from_model(subscription, buyer.telegram_user_id),
            session=Session(
                key=SessionKey(
                    session.provider,
                    session.theatre_alias,
                    session.provider_session_id,
                ),
                event_id=session.event_id,
                hall_id=session.hall_id,
                title=session.title,
                starts_at=_aware(session.starts_at),
            ),
        )

    async def list_resumable(self, *, limit: int) -> tuple[str, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._session_factory() as database:
            values = await database.scalars(
                select(CheckoutIntentModel.id)
                .where(CheckoutIntentModel.state.in_(("pending", "retry_allowed")))
                .order_by(CheckoutIntentModel.created_at, CheckoutIntentModel.id)
                .limit(limit)
            )
            return tuple(values)

    async def renewal_task(self, intent_id: str) -> tuple[RenewalTask, int]:
        async with self._session_factory() as database:
            row = (
                await database.execute(
                    select(RenewalCycleModel, CandidateModel, SubscriptionModel)
                    .join(
                        CheckoutIntentModel,
                        CheckoutIntentModel.renewal_cycle_id == RenewalCycleModel.id,
                    )
                    .join(CandidateModel, CandidateModel.id == RenewalCycleModel.candidate_id)
                    .join(
                        SubscriptionModel,
                        SubscriptionModel.id == CandidateModel.subscription_id,
                    )
                    .where(CheckoutIntentModel.id == intent_id)
                )
            ).one_or_none()
        if row is None:
            raise LookupError("checkout graph not found")
        cycle, candidate, subscription = row
        config = subscription.config
        maximum_value = config.get("max_cycles_per_session")
        maximum = (
            maximum_value
            if isinstance(maximum_value, int) and not isinstance(maximum_value, bool)
            else None
        )
        return (
            RenewalTask(
                candidate.id,
                subscription.id,
                cycle.cycle_no - 1,
                None,
                self._integer(config.get("renewal_interval_seconds"), 1200),
                self._integer(config.get("availability_retry_seconds"), 180),
                maximum,
            ),
            cycle.cycle_no,
        )

    async def claim(self, *, now: datetime, limit: int) -> tuple[str, ...]:
        self._validate(now)
        if limit <= 0:
            raise ValueError("limit must be positive")
        async with self._session_factory() as database, database.begin():
            ids = (
                select(CandidateModel.id)
                .join(
                    SubscriptionModel,
                    SubscriptionModel.id == CandidateModel.subscription_id,
                )
                .where(
                    CandidateModel.booking_mode == "dry_run",
                    CandidateModel.tracking_state == "dry_run_queued",
                    SubscriptionModel.enabled.is_(True),
                )
                .order_by(CandidateModel.id)
                .limit(limit)
            )
            return tuple(
                (
                    await database.execute(
                        update(CandidateModel)
                        .where(
                            CandidateModel.id.in_(ids),
                            CandidateModel.tracking_state == "dry_run_queued",
                        )
                        .values(tracking_state="dry_run_claimed")
                        .returning(CandidateModel.id)
                    )
                ).scalars()
            )

    async def recover_claims(self) -> int:
        async with self._session_factory() as database, database.begin():
            result = await database.execute(
                update(CandidateModel)
                .where(CandidateModel.tracking_state == "dry_run_claimed")
                .values(tracking_state="dry_run_queued")
                .returning(CandidateModel.id)
            )
            return len(tuple(result.scalars()))

    async def save(self, report: DryRunReport, *, now: datetime) -> None:
        self._validate(now)
        async with self._session_factory() as database, database.begin():
            candidate = await database.get(CandidateModel, report.candidate_id)
            if candidate is None:
                raise LookupError("dry-run candidate not found")
            existing = await database.scalar(
                select(DryRunReportModel).where(
                    DryRunReportModel.candidate_id == report.candidate_id
                )
            )
            if existing is None:
                database.add(
                    DryRunReportModel(
                        id=str(uuid4()),
                        candidate_id=report.candidate_id,
                        state=report.state,
                        selected_seat_ids=list(report.selected_seat_ids),
                        total_minor=report.total.minor_units if report.total else None,
                        currency=report.total.currency if report.total else None,
                        explanation=report.explanation,
                        created_at=now,
                    )
                )
            candidate.subscription_version = (
                await database.scalar(
                    select(SubscriptionModel.version).where(
                        SubscriptionModel.id == candidate.subscription_id
                    )
                )
                or candidate.subscription_version
            )
            candidate.tracking_state = "dry_run_completed"
            candidate.next_run_at = None
            candidate.stop_reason = report.state

    async def release(self, candidate_id: str) -> None:
        async with self._session_factory() as database, database.begin():
            await database.execute(
                update(CandidateModel)
                .where(
                    CandidateModel.id == candidate_id,
                    CandidateModel.tracking_state == "dry_run_claimed",
                )
                .values(tracking_state="dry_run_queued")
            )

    async def rejected(
        self,
        *,
        intent_id: str,
        error_code: str,
        now: datetime,
        retry_seconds: int,
        stop: bool = False,
    ) -> None:
        self._validate(now)
        async with self._session_factory() as database, database.begin():
            intent, cycle, candidate = await self._checkout_graph(database, intent_id)
            allocation = await database.scalar(
                select(BudgetAllocationModel).where(
                    BudgetAllocationModel.checkout_intent_id == intent.id
                )
            )
            if allocation is not None and allocation.active:
                allocation.active = False
                allocation.released_at = now
                cycle.allocation_released_at = now
            intent.state = "rejected"
            intent.last_error_code = error_code
            intent.updated_at = now
            cycle.state = "rejected"
            cycle.ended_at = now
            if stop:
                candidate.tracking_state = "stopped"
                candidate.next_run_at = None
                candidate.stop_reason = "max_cycles_per_session"
            else:
                candidate.tracking_state = "waiting_availability"
                candidate.next_run_at = now + timedelta(seconds=retry_seconds)
                candidate.stop_reason = error_code

    async def needs_attention(self, *, intent_id: str, error_code: str, now: datetime) -> None:
        self._validate(now)
        async with self._session_factory() as database, database.begin():
            intent, cycle, candidate = await self._checkout_graph(database, intent_id)
            intent.state = "unknown"
            intent.last_error_code = error_code
            intent.updated_at = now
            cycle.state = "needs_attention"
            candidate.tracking_state = "needs_attention"
            candidate.next_run_at = None
            candidate.stop_reason = error_code

    async def _checkout_graph(
        self, database: AsyncSession, intent_id: str
    ) -> tuple[CheckoutIntentModel, RenewalCycleModel, CandidateModel]:
        intent = await database.get(CheckoutIntentModel, intent_id)
        if intent is None:
            raise LookupError("checkout intent not found")
        cycle = await database.get(RenewalCycleModel, intent.renewal_cycle_id)
        if cycle is None:
            raise LookupError("renewal cycle not found")
        candidate = await database.get(CandidateModel, cycle.candidate_id)
        if candidate is None:
            raise LookupError("candidate not found")
        return intent, cycle, candidate

    @staticmethod
    def _validate(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("booking persistence timestamp must be timezone-aware")

    @staticmethod
    def _integer(value: object, default: int) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else default


class SqlAlchemyBatchSummaryScheduler:
    """Create one initial batch summary after newly discovered candidates are processed."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._writer = SqlAlchemyOrderOutboxWriter(session_factory)

    async def enqueue_for_candidates(
        self, candidate_ids: tuple[str, ...], *, now: datetime
    ) -> tuple[str, ...]:
        if not candidate_ids:
            return ()
        async with self._session_factory() as database:
            rows = (
                await database.execute(
                    select(CandidateModel, SessionModel)
                    .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                    .where(CandidateModel.id.in_(candidate_ids))
                    .order_by(SessionModel.starts_at, CandidateModel.id)
                )
            ).all()
        grouped: dict[tuple[str, str], list[BatchSessionResult]] = {}
        for candidate, session in rows:
            grouped.setdefault((candidate.discovery_batch_id, candidate.buyer_id), []).append(
                BatchSessionResult(
                    session.title,
                    self._status(candidate.tracking_state, candidate.stop_reason),
                )
            )
        outbox_ids: list[str] = []
        for (batch_id, buyer_id), results in grouped.items():
            outbox_ids.append(
                await self._writer.enqueue_batch_summary(
                    discovery_batch_id=batch_id,
                    buyer_id=buyer_id,
                    results=tuple(results),
                    recorded_at=now,
                )
            )
        return tuple(outbox_ids)

    async def enqueue_pending(self, *, now: datetime) -> tuple[str, ...]:
        """Enqueue summaries only after every candidate in a buyer/batch group was attempted."""
        async with self._session_factory() as database:
            rows = (
                await database.execute(
                    select(CandidateModel, SessionModel)
                    .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                    .order_by(
                        CandidateModel.discovery_batch_id,
                        CandidateModel.buyer_id,
                        SessionModel.starts_at,
                        CandidateModel.id,
                    )
                )
            ).all()
        grouped: dict[tuple[str, str], list[tuple[CandidateModel, SessionModel]]] = {}
        for candidate, session in rows:
            grouped.setdefault((candidate.discovery_batch_id, candidate.buyer_id), []).append(
                (candidate, session)
            )
        outbox_ids: list[str] = []
        unfinished = {"queued", "processing", "dry_run_queued", "dry_run_processing"}
        for (batch_id, buyer_id), values in grouped.items():
            if any(candidate.tracking_state in unfinished for candidate, _ in values):
                continue
            results = tuple(
                BatchSessionResult(
                    session.title,
                    self._status(candidate.tracking_state, candidate.stop_reason),
                )
                for candidate, session in values
            )
            outbox_ids.append(
                await self._writer.enqueue_batch_summary(
                    discovery_batch_id=batch_id,
                    buyer_id=buyer_id,
                    results=results,
                    recorded_at=now,
                )
            )
        return tuple(outbox_ids)

    @staticmethod
    def _status(state: str, reason: str | None) -> str:
        return {
            "renewal_waiting": "ссылка на оплату подготовлена",
            "awaiting_payment": "ссылка на оплату подготовлена",
            "waiting_availability": "нет подходящих соседних мест",
            "waiting_budget": "ожидание доступного бюджета",
            "dry_run_completed": f"dry-run: {reason or 'решение сохранено'}",
            "needs_attention": "требуется действие пользователя",
            "skipped_limit": "пропущено по лимиту",
            "stopped": f"остановлено: {reason or 'условия изменились'}",
        }.get(state, state)


class SqlAlchemyConfirmedRecoveryHandler:
    """Complete recovered confirmations and repair a crash before renewal scheduling."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._writer = SqlAlchemyOrderOutboxWriter(session_factory)
        self._renewals = SqlAlchemyRenewalRepository(session_factory)

    async def finalize(self, intent_id: str, order: ConfirmedOrder) -> None:
        await self._writer.record_confirmed_order(
            intent_id=intent_id,
            order=order,
            recorded_at=self._now(),
        )
        task, cycle_no = await SqlAlchemyBookingRepository(self._session_factory).renewal_task(
            intent_id
        )
        await self._renewals.schedule_after_hold(
            task=task,
            cycle_no=cycle_no,
            held_at=order.held_at,
        )

    async def recover_unscheduled(self) -> int:
        async with self._session_factory() as database:
            rows = (
                await database.execute(
                    select(CheckoutIntentModel.id, OrderModel.held_at)
                    .join(
                        RenewalCycleModel,
                        RenewalCycleModel.id == CheckoutIntentModel.renewal_cycle_id,
                    )
                    .join(OrderModel, OrderModel.checkout_intent_id == CheckoutIntentModel.id)
                    .where(
                        CheckoutIntentModel.state == "confirmed",
                        RenewalCycleModel.due_at.is_(None),
                        OrderModel.held_at.is_not(None),
                    )
                    .order_by(OrderModel.created_at, OrderModel.id)
                )
            ).all()
        for intent_id, held_at in rows:
            assert held_at is not None
            task, cycle_no = await SqlAlchemyBookingRepository(self._session_factory).renewal_task(
                intent_id
            )
            await self._renewals.schedule_after_hold(
                task=task,
                cycle_no=cycle_no,
                held_at=_aware(held_at),
            )
        return len(rows)
