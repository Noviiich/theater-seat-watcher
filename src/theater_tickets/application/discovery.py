"""Durably turn complete catalogue snapshots into booking candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from theater_tickets.adapters.persistence.models import (
    CandidateModel,
    DiscoveryBatchModel,
    SessionModel,
    SubscriptionBaselineModel,
    SubscriptionModel,
)
from theater_tickets.adapters.persistence.repositories import CatalogueRepository
from theater_tickets.domain.models import Session, Subscription

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """Stable identifiers recorded while processing one catalogue response."""

    baseline_established: bool
    discovered_session_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()


class DiscoveryService:
    """Apply one complete snapshot inside an already-open transaction."""

    async def process(
        self,
        database: AsyncSession,
        *,
        subscription: Subscription,
        sessions: tuple[Session, ...],
        fetched_at: datetime,
        fingerprint: str,
        complete: bool,
    ) -> DiscoveryOutcome:
        """Persist a complete snapshot, establishing or applying its baseline.

        An incomplete response is deliberately ignored: it must not make a
        catalogue look empty or advance a subscription's observation point.
        """
        if not complete:
            return DiscoveryOutcome(baseline_established=False)

        relevant_sessions = tuple(
            item for item in sessions if item.key.theatre_alias == subscription.theatre_alias
        )
        known_provider_ids = (
            set(
                (
                    await database.scalars(
                        select(SessionModel.provider_session_id).where(
                            SessionModel.provider == "quicktickets",
                            SessionModel.theatre_alias == subscription.theatre_alias,
                            SessionModel.provider_session_id.in_(
                                [item.key.session_id for item in relevant_sessions]
                            ),
                        )
                    )
                ).all()
            )
            if relevant_sessions
            else set()
        )

        repository = CatalogueRepository(database)
        persisted_sessions = {
            item.key.session_id: await repository.upsert_session(item, seen_at=fetched_at)
            for item in relevant_sessions
        }
        snapshot = await repository.get_or_add_snapshot(
            snapshot_id=str(uuid4()),
            theatre_alias=subscription.theatre_alias,
            fetched_at=fetched_at,
            fingerprint=fingerprint,
        )
        await database.flush()

        baseline = await database.get(SubscriptionBaselineModel, subscription.subscription_id)
        if baseline is None:
            database.add(
                SubscriptionBaselineModel(
                    subscription_id=subscription.subscription_id,
                    snapshot_id=snapshot.id,
                    established_at=fetched_at,
                )
            )
            return DiscoveryOutcome(baseline_established=True)

        if baseline.snapshot_id == snapshot.id:
            return DiscoveryOutcome(baseline_established=False)

        newly_seen: tuple[Session, ...] = ()
        batch = await database.scalar(
            select(DiscoveryBatchModel).where(DiscoveryBatchModel.snapshot_id == snapshot.id)
        )
        if batch is None:
            newly_seen = tuple(
                item for item in relevant_sessions if item.key.session_id not in known_provider_ids
            )
            if newly_seen:
                batch = DiscoveryBatchModel(
                    id=str(uuid4()),
                    snapshot_id=snapshot.id,
                    discovered_session_ids=[
                        persisted_sessions[item.key.session_id].id for item in newly_seen
                    ],
                    created_at=fetched_at,
                )
                database.add(batch)
                await database.flush()

        baseline.snapshot_id = snapshot.id
        if batch is None or not subscription.enabled:
            return DiscoveryOutcome(baseline_established=False)

        persisted_subscription = await database.get(SubscriptionModel, subscription.subscription_id)
        if persisted_subscription is None:
            raise LookupError("subscription must be persisted before discovery")

        sessions_by_persisted_id = {
            persisted.id: item
            for item, persisted in (
                (item, persisted_sessions[item.key.session_id]) for item in relevant_sessions
            )
        }
        discovered = tuple(
            sessions_by_persisted_id[session_id]
            for session_id in batch.discovered_session_ids
            if session_id in sessions_by_persisted_id
        )
        if not discovered:
            return DiscoveryOutcome(baseline_established=False)

        candidate_ids: list[str] = []
        for item in discovered:
            if not subscription.matches(item, local_starts_at=item.starts_at.astimezone(MOSCOW)):
                continue
            persisted = persisted_sessions[item.key.session_id]
            existing = await database.scalar(
                select(CandidateModel).where(
                    CandidateModel.buyer_id == persisted_subscription.buyer_id,
                    CandidateModel.session_id == persisted.id,
                )
            )
            if existing is not None:
                continue
            candidate_id = str(uuid4())
            database.add(
                CandidateModel(
                    id=candidate_id,
                    buyer_id=persisted_subscription.buyer_id,
                    subscription_id=subscription.subscription_id,
                    session_id=persisted.id,
                    discovery_batch_id=batch.id,
                    subscription_version=persisted_subscription.version,
                    booking_mode=subscription.booking_mode.value,
                    tracking_state=(
                        "queued" if subscription.booking_mode.value == "live" else "dry_run_queued"
                    ),
                    current_cycle_no=0,
                    watch_until=item.starts_at,
                )
            )
            candidate_ids.append(candidate_id)

        return DiscoveryOutcome(
            baseline_established=False,
            discovered_session_ids=tuple(
                persisted_sessions[item.key.session_id].id for item in newly_seen
            ),
            candidate_ids=tuple(candidate_ids),
        )
