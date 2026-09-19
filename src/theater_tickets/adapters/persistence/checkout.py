"""Durable checkout-stage recording in independent short transactions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import CheckoutIntentModel
from theater_tickets.application.checkout import CheckoutErrorCode, CheckoutStage

_TERMINAL_STAGES = frozenset(
    {
        CheckoutStage.VALIDATED,
        CheckoutStage.REJECTED,
        CheckoutStage.REQUIRES_USER_ACTION,
        CheckoutStage.AMBIGUOUS,
    }
)


class SqlAlchemyCheckoutStageRecorder:
    """Commit every remote milestone before the workflow advances."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def record(
        self,
        intent_id: str,
        stage: CheckoutStage,
        error_code: CheckoutErrorCode | None = None,
    ) -> None:
        timestamp = self._now()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("checkout stage timestamp must be timezone-aware")
        async with self._session_factory() as database, database.begin():
            intent = await database.get(CheckoutIntentModel, intent_id)
            if intent is None:
                raise LookupError("checkout intent not found")
            intent.remote_stage = stage.value
            intent.updated_at = timestamp
            intent.last_error_code = error_code.value if error_code is not None else None
            if stage is CheckoutStage.WRITE_STARTED and intent.write_started_at is None:
                intent.write_started_at = timestamp
                intent.state = "submitting"
            elif stage in _TERMINAL_STAGES:
                intent.write_completed_at = timestamp
                if stage is CheckoutStage.VALIDATED:
                    # The future booking worker persists Order and advances to
                    # confirmed in its own atomic transaction.
                    intent.state = "validated"
                    intent.last_error_code = None
                elif stage is CheckoutStage.REJECTED:
                    intent.state = "rejected"
                elif stage is CheckoutStage.REQUIRES_USER_ACTION:
                    intent.state = "requires_user_action"
                else:
                    intent.state = "unknown"
