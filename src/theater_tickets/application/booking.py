"""Provider-neutral decisions for one candidate before any checkout write."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from theater_tickets.application.checkout import CheckoutRequest, CheckoutResult
from theater_tickets.application.ports import TheatreProvider
from theater_tickets.application.renewals import RenewalTask
from theater_tickets.domain.models import Money, Session, Subscription
from theater_tickets.domain.seating.candidates import (
    RankedGroup,
    SelectionPreferences,
    rank_groups,
)
from theater_tickets.domain.seating.topology import HallProfile

MOSCOW = ZoneInfo("Europe/Moscow")


class EvaluationState(StrEnum):
    READY = "ready"
    PAUSED = "paused"
    WAITING_AVAILABILITY = "waiting_availability"
    STOPPED = "stopped"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True, slots=True)
class SeatSelectionConfiguration:
    profile: HallProfile
    preferences: SelectionPreferences


@dataclass(frozen=True, slots=True)
class BookingCandidateContext:
    candidate_id: str
    buyer_id: str
    subscription_version: int
    profile_ref: str | None
    subscription: Subscription
    session: Session


@dataclass(frozen=True, slots=True)
class BookingEvaluation:
    state: EvaluationState
    reason: str
    session: Session | None = None
    group: RankedGroup | None = None

    def __post_init__(self) -> None:
        if (self.state is EvaluationState.READY) != (
            self.session is not None and self.group is not None
        ):
            raise ValueError("ready evaluation requires a session and seat group")


@dataclass(frozen=True, slots=True)
class DryRunReport:
    candidate_id: str
    state: str
    selected_seat_ids: tuple[str, ...] = ()
    total: Money | None = None
    explanation: str | None = None


class SeatProfileSource(Protocol):
    def load(self, profile_id: str) -> SeatSelectionConfiguration:
        """Return one validated topology and its deterministic preferences."""


class CheckoutSubmitter(Protocol):
    async def submit(self, request: CheckoutRequest) -> CheckoutResult: ...


class CandidateContextRepository(Protocol):
    async def load(self, candidate_id: str) -> BookingCandidateContext | None: ...


class DryRunRepository(Protocol):
    async def recover_claims(self) -> int: ...

    async def claim(self, *, now: datetime, limit: int) -> tuple[str, ...]: ...

    async def save(self, report: DryRunReport, *, now: datetime) -> None: ...

    async def release(self, candidate_id: str) -> None: ...


class CheckoutFailureRepository(Protocol):
    async def rejected(
        self,
        *,
        intent_id: str,
        error_code: str,
        now: datetime,
        retry_seconds: int,
        stop: bool = False,
    ) -> None: ...

    async def needs_attention(self, *, intent_id: str, error_code: str, now: datetime) -> None: ...


class BatchSummaryScheduler(Protocol):
    async def enqueue_for_candidates(
        self, candidate_ids: tuple[str, ...], *, now: datetime
    ) -> tuple[str, ...]: ...


class StartupRecovery(Protocol):
    async def recover_startup(self) -> object: ...


class ResumableCheckoutRepository(Protocol):
    async def list_resumable(self, *, limit: int) -> tuple[str, ...]: ...

    async def renewal_task(self, intent_id: str) -> tuple[RenewalTask, int]: ...


class CandidateEvaluator:
    """Re-read current provider state and choose the best verified adjacent group."""

    def __init__(self, *, provider: TheatreProvider, profiles: SeatProfileSource) -> None:
        self._provider = provider
        self._profiles = profiles

    async def evaluate(
        self, context: BookingCandidateContext, *, now: datetime
    ) -> BookingEvaluation:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("evaluation timestamp must be timezone-aware")
        if not context.subscription.enabled:
            return BookingEvaluation(EvaluationState.PAUSED, "subscription_paused")
        session = await self._provider.fetch_session(context.session.key)
        if session.key != context.session.key:
            return BookingEvaluation(EvaluationState.NEEDS_ATTENTION, "session_key_mismatch")
        if session.starts_at <= now:
            return BookingEvaluation(EvaluationState.STOPPED, "session_started")
        if not context.subscription.matches(
            session, local_starts_at=session.starts_at.astimezone(MOSCOW)
        ):
            return BookingEvaluation(EvaluationState.STOPPED, "filters_changed")
        capabilities = await self._provider.fetch_sale_capabilities(session.key)
        if not capabilities.allows_regular_sale(context.subscription.ticket_count):
            return BookingEvaluation(EvaluationState.WAITING_AVAILABILITY, "sale_unavailable")
        inventory = await self._provider.fetch_inventory(session.key)
        configuration = self._profiles.load(context.subscription.seat_profile_id)
        preferences = replace(
            configuration.preferences,
            ticket_count=context.subscription.ticket_count,
            max_ticket_price=context.subscription.max_ticket_price,
            max_order_total=context.subscription.max_order_total,
        )
        ranking = rank_groups(inventory, configuration.profile, preferences)
        if ranking.reason == "profile_mismatch":
            return BookingEvaluation(EvaluationState.NEEDS_ATTENTION, ranking.reason)
        if not ranking.groups:
            return BookingEvaluation(
                EvaluationState.WAITING_AVAILABILITY,
                ranking.reason or "no_adjacent_group",
            )
        selected = ranking.groups[0]
        if not context.subscription.allows_seat_group(selected.group):
            return BookingEvaluation(EvaluationState.WAITING_AVAILABILITY, "budget_filter")
        return BookingEvaluation(
            EvaluationState.READY,
            "selected",
            session=session,
            group=selected,
        )
