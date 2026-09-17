"""Small repositories used by persistence and migration tests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from theater_tickets.adapters.persistence.models import CatalogueSnapshotModel, SessionModel
from theater_tickets.domain.models import Session


class CatalogueRepository:
    """Persists stable session IDs and complete catalogue snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_session(self, value: Session, *, seen_at: datetime) -> SessionModel:
        statement = select(SessionModel).where(
            SessionModel.provider == value.key.provider,
            SessionModel.theatre_alias == value.key.theatre_alias,
            SessionModel.provider_session_id == value.key.session_id,
        )
        existing = await self._session.scalar(statement)
        if existing is not None:
            existing.event_id = value.event_id
            existing.hall_id = value.hall_id
            existing.title = value.title
            existing.starts_at = value.starts_at
            existing.last_seen_at = seen_at
            return existing
        model = SessionModel(
            id=f"{value.key.provider}:{value.key.theatre_alias}:{value.key.session_id}",
            provider=value.key.provider,
            theatre_alias=value.key.theatre_alias,
            provider_session_id=value.key.session_id,
            event_id=value.event_id,
            hall_id=value.hall_id,
            title=value.title,
            starts_at=value.starts_at,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        self._session.add(model)
        return model

    async def get_or_add_snapshot(
        self, *, snapshot_id: str, theatre_alias: str, fetched_at: datetime, fingerprint: str
    ) -> CatalogueSnapshotModel:
        statement = select(CatalogueSnapshotModel).where(
            CatalogueSnapshotModel.theatre_alias == theatre_alias,
            CatalogueSnapshotModel.fingerprint == fingerprint,
        )
        existing = await self._session.scalar(statement)
        if existing is not None:
            return existing
        model = CatalogueSnapshotModel(
            id=snapshot_id,
            theatre_alias=theatre_alias,
            fetched_at=fetched_at,
            complete=True,
            fingerprint=fingerprint,
        )
        self._session.add(model)
        return model
