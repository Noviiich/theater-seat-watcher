"""One-shot catalogue polling separated from checkout and notification work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.application.discovery import DiscoveryService
from theater_tickets.application.runtime import RuntimeEventLog
from theater_tickets.domain.models import Session, Subscription


@dataclass(frozen=True, slots=True)
class CatalogueRead:
    sessions: tuple[Session, ...]
    fingerprint: str
    complete: bool


@dataclass(frozen=True, slots=True)
class CataloguePollOutcome:
    theatres: int
    subscriptions: int
    candidates: int


class CatalogueSource(Protocol):
    async def fetch_catalogue(self, theatre_alias: str) -> CatalogueRead: ...


class ActiveSubscriptionSource(Protocol):
    async def list_enabled(self) -> tuple[Subscription, ...]: ...


class CataloguePollingWorker:
    """Fetch each theatre once and durably apply it to every enabled subscription."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        source: CatalogueSource,
        subscriptions: ActiveSubscriptionSource,
        event_log: RuntimeEventLog | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._subscriptions = subscriptions
        self._event_log = event_log

    async def run_once(self, *, now: datetime) -> CataloguePollOutcome:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("catalogue poll timestamp must be timezone-aware")
        subscriptions = await self._subscriptions.list_enabled()
        grouped: dict[str, list[Subscription]] = {}
        for subscription in subscriptions:
            grouped.setdefault(subscription.theatre_alias, []).append(subscription)
        candidate_count = 0
        for theatre_alias, rules in grouped.items():
            snapshot = await self._source.fetch_catalogue(theatre_alias)
            persisted_started = monotonic()
            theatre_candidates = 0
            for subscription in rules:
                async with SqlAlchemyUnitOfWork(self._session_factory) as uow:
                    assert uow.session is not None
                    outcome = await DiscoveryService().process(
                        uow.session,
                        subscription=subscription,
                        sessions=snapshot.sessions,
                        fetched_at=now,
                        fingerprint=snapshot.fingerprint,
                        complete=snapshot.complete,
                    )
                candidate_count += len(outcome.candidate_ids)
                theatre_candidates += len(outcome.candidate_ids)
            if self._event_log is not None:
                self._event_log.emit(
                    "catalogue_discovery",
                    persistence_ms=round((monotonic() - persisted_started) * 1000),
                    subscriptions=len(rules),
                    candidates=theatre_candidates,
                )
        return CataloguePollOutcome(len(grouped), len(subscriptions), candidate_count)
