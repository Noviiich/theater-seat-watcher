"""SQLAlchemy state transitions for interrupted checkout recovery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import (
    BuyerModel,
    CandidateModel,
    CheckoutIntentModel,
    RenewalCycleModel,
    SessionModel,
)
from theater_tickets.adapters.quicktickets.buyer import validate_buyer_profile
from theater_tickets.application.checkout import CheckoutRequest
from theater_tickets.application.reconciliation import (
    IncompleteCheckout,
    RecoveryDisposition,
    RecoveryOutcome,
)
from theater_tickets.domain.models import Money, SessionKey

_INCOMPLETE_STATES = ("pending", "submitting", "validating", "unknown", "validated")


class SqlAlchemyRecoveryRepository:
    """Keep allocations active while classifying interrupted writes."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def list_incomplete(self) -> tuple[IncompleteCheckout, ...]:
        async with self._session_factory() as database:
            rows = await database.execute(
                select(CheckoutIntentModel.id, CheckoutIntentModel.write_started_at)
                .where(CheckoutIntentModel.state.in_(_INCOMPLETE_STATES))
                .order_by(CheckoutIntentModel.created_at, CheckoutIntentModel.id)
            )
            return tuple(
                IncompleteCheckout(intent_id, write_started_at is not None)
                for intent_id, write_started_at in rows
            )

    async def apply(self, outcome: RecoveryOutcome) -> None:
        timestamp = self._now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("recovery timestamp must be timezone-aware")
        async with self._session_factory() as database, database.begin():
            intent = await database.get(CheckoutIntentModel, outcome.intent_id)
            if intent is None:
                raise LookupError("checkout intent not found")
            cycle = await database.get(RenewalCycleModel, intent.renewal_cycle_id)
            if cycle is None:
                raise LookupError("renewal cycle not found")
            candidate = await database.get(CandidateModel, cycle.candidate_id)
            if candidate is None:
                raise LookupError("candidate not found")

            intent.updated_at = timestamp
            intent.last_error_code = (
                outcome.error_code.value if outcome.error_code is not None else None
            )
            if outcome.disposition is RecoveryDisposition.RETRY_ALLOWED:
                if intent.write_started_at is None:
                    intent.state = "pending"
                    intent.remote_stage = "planned"
                else:
                    intent.state = "retry_allowed"
                    intent.remote_stage = "reconciled_not_found"
                    intent.write_completed_at = timestamp
                cycle.state = "submitting"
                if candidate.tracking_state not in ("stopped", "paused"):
                    candidate.tracking_state = "submitting"
            elif outcome.disposition is RecoveryDisposition.CONFIRMED:
                intent.state = "validated"
                intent.remote_stage = "reconciled_found"
                intent.write_completed_at = timestamp
                cycle.state = "submitting"
                candidate.tracking_state = "submitting"
            else:
                intent.state = "unknown"
                intent.remote_stage = "needs_attention"
                intent.write_completed_at = timestamp
                cycle.state = "needs_attention"
                candidate.tracking_state = "needs_attention"


class SqlAlchemyRecoveryRequestLoader:
    """Rebuild the immutable checkout snapshot from the persisted buyer profile."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def load(self, intent_id: str) -> CheckoutRequest:
        async with self._session_factory() as database:
            row = (
                await database.execute(
                    select(
                        CheckoutIntentModel.selected_seat_ids,
                        CheckoutIntentModel.expected_total_minor,
                        CheckoutIntentModel.reserved_total_minor,
                        CheckoutIntentModel.currency,
                        CheckoutIntentModel.expected_hold_ttl_seconds,
                        CheckoutIntentModel.price_unlimited,
                        BuyerModel.lastname,
                        BuyerModel.firstname,
                        BuyerModel.middlename,
                        BuyerModel.email,
                        BuyerModel.phone,
                        BuyerModel.personal_data_consent,
                        SessionModel.provider,
                        SessionModel.theatre_alias,
                        SessionModel.provider_session_id,
                    )
                    .join(
                        RenewalCycleModel,
                        RenewalCycleModel.id == CheckoutIntentModel.renewal_cycle_id,
                    )
                    .join(CandidateModel, CandidateModel.id == RenewalCycleModel.candidate_id)
                    .join(BuyerModel, BuyerModel.id == CandidateModel.buyer_id)
                    .join(SessionModel, SessionModel.id == CandidateModel.session_id)
                    .where(CheckoutIntentModel.id == intent_id)
                )
            ).one_or_none()
        if row is None:
            raise LookupError("checkout intent not found")
        (
            selected_seat_ids,
            expected_total_minor,
            reserved_total_minor,
            currency,
            expected_hold_ttl_seconds,
            price_unlimited,
            lastname,
            firstname,
            middlename,
            email,
            phone,
            personal_data_consent,
            provider,
            theatre_alias,
            provider_session_id,
        ) = row
        if any(value is None for value in (lastname, firstname, middlename, email, phone)):
            raise LookupError("checkout buyer profile is missing")
        buyer = validate_buyer_profile(
            lastname=lastname,
            firstname=firstname,
            middlename=middlename,
            email=email,
            phone=phone,
            personal_data_consent=personal_data_consent,
        )
        return CheckoutRequest(
            intent_id=intent_id,
            session_key=SessionKey(provider, theatre_alias, provider_session_id),
            seat_ids=tuple(selected_seat_ids),
            expected_total=Money(expected_total_minor, currency),
            reserved_total=Money(reserved_total_minor, currency),
            expected_hold_ttl_seconds=expected_hold_ttl_seconds,
            buyer=buyer,
            price_unlimited=price_unlimited,
        )
