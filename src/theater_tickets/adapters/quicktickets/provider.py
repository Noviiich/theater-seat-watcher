"""Concrete public QuickTickets reads for catalogue polling and seat evaluation."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from theater_tickets.adapters.quicktickets.catalogue import (
    CatalogueSessionRef,
    parse_catalogue,
    parse_session_page,
)
from theater_tickets.adapters.quicktickets.client import QuickTicketsClient
from theater_tickets.adapters.quicktickets.errors import QuickTicketsContractError
from theater_tickets.adapters.quicktickets.inventory import QuickTicketsInventoryReader
from theater_tickets.application.runtime import RuntimeEventLog
from theater_tickets.domain.models import SaleCapabilities, Seat, Session, SessionKey
from theater_tickets.workers.polling import CatalogueRead

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    event_id: str
    hall_id: str


def parse_session_identity(payload: dict[str, Any], *, session_id: str) -> SessionIdentity:
    """Extract only explicit event/hall IDs for the requested session."""
    response = payload.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("events"), list):
        raise QuickTicketsContractError("anysession/anysession events are missing")
    matches: list[SessionIdentity] = []
    for event in response["events"]:
        if not isinstance(event, dict) or not isinstance(event.get("sessions"), list):
            continue
        event_id = event.get("id")
        for session in event["sessions"]:
            if not isinstance(session, dict) or str(session.get("id")) != session_id:
                continue
            hall_id = session.get("hall_id", session.get("hallId"))
            if event_id is None or hall_id is None:
                raise QuickTicketsContractError("session details have no explicit event or hall ID")
            matches.append(SessionIdentity(str(event_id), str(hall_id)))
    if len(matches) != 1:
        raise QuickTicketsContractError("requested session details are absent or duplicated")
    return matches[0]


class QuickTicketsProvider:
    """Share one rate-limited HTTP client between discovery and fresh evaluation reads."""

    def __init__(
        self,
        client: QuickTicketsClient,
        *,
        known_sessions: Callable[[str], Awaitable[Mapping[str, Session]]] | None = None,
        full_refresh_seconds: float = 600.0,
        event_log: RuntimeEventLog | None = None,
    ) -> None:
        self._client = client
        self._inventory = QuickTicketsInventoryReader(client)
        self._sessions: dict[SessionKey, Session] = {}
        self._known_sessions = known_sessions
        self._full_refresh_seconds = full_refresh_seconds
        self._last_full_refresh = time.monotonic()
        self._event_log = event_log

    async def fetch_catalogue(self, theatre_alias: str) -> CatalogueRead:
        if theatre_alias != self._client.theatre_alias:
            raise QuickTicketsContractError("runtime requested an unexpected theatre alias")
        started = time.monotonic()
        html = await self._client.get_catalogue_page()
        parsed = parse_catalogue(html)
        if not parsed.complete:
            raise QuickTicketsContractError("public catalogue snapshot is incomplete")
        index_ms = round((time.monotonic() - started) * 1000)
        known = await self._known_sessions(theatre_alias) if self._known_sessions else {}
        force_full = time.monotonic() - self._last_full_refresh >= self._full_refresh_seconds
        sessions_list: list[Session] = []
        reused = 0
        for ref in parsed.sessions:
            cached = known.get(ref.session_id)
            if cached is not None and not force_full and self._ref_matches(ref, cached):
                self._sessions[cached.key] = cached
                sessions_list.append(cached)
                reused += 1
            else:
                sessions_list.append(await self._session_from_ref(ref))
        sessions = tuple(sessions_list)
        if force_full:
            self._last_full_refresh = time.monotonic()
        serialized = json.dumps(
            [
                {
                    "session_id": item.key.session_id,
                    "event_id": item.event_id,
                    "hall_id": item.hall_id,
                    "title": item.title,
                    "starts_at": item.starts_at.isoformat(),
                }
                for item in sessions
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        if self._event_log is not None:
            self._event_log.emit(
                "catalogue_read",
                index_ms=index_ms,
                total_ms=round((time.monotonic() - started) * 1000),
                sessions=len(sessions),
                reused=reused,
                detailed=len(sessions) - reused,
                full_refresh=force_full,
            )
        return CatalogueRead(sessions, fingerprint, True)

    @staticmethod
    def _ref_matches(ref: CatalogueSessionRef, cached: Session) -> bool:
        local_date = cached.starts_at.astimezone(MOSCOW).date().isoformat()
        return (
            ref.title is not None
            and ref.title.strip() == cached.title
            and ref.date_hint == local_date
            and (ref.event_id is None or ref.event_id == cached.event_id)
        )

    async def fetch_session(self, key: SessionKey) -> Session:
        cached = self._sessions.get(key)
        if cached is None:
            raise QuickTicketsContractError(
                "session is not in the latest complete catalogue; retry after catalogue polling"
            )
        html, context = await self._client.get_session_page(key.session_id)
        page = parse_session_page(html)
        payload = await self._client.get_json(
            "anysession/anysession", context=context, elem_id=key.session_id
        )
        identity = parse_session_identity(payload, session_id=key.session_id)
        refreshed = Session(
            key,
            identity.event_id,
            identity.hall_id,
            cached.title,
            page.start_dates[0],
        )
        self._verify_cached(cached, refreshed)
        self._sessions[key] = refreshed
        return refreshed

    async def fetch_inventory(self, key: SessionKey) -> tuple[Seat, ...]:
        session = self._sessions.get(key)
        if session is None:
            raise QuickTicketsContractError("session inventory requested before catalogue details")
        context = await self._client.get_context(key.session_id)
        inventory = await self._inventory.fetch_inventory(
            context=context,
            session_id=key.session_id,
            hall_id=session.hall_id,
        )
        return inventory.seats

    async def fetch_sale_capabilities(self, key: SessionKey) -> SaleCapabilities:
        context = await self._client.get_context(key.session_id)
        return await self._inventory.fetch_sale_capabilities(
            context=context,
            session_id=key.session_id,
        )

    async def _session_from_ref(self, ref: CatalogueSessionRef) -> Session:
        if ref.title is None or not ref.title.strip():
            raise QuickTicketsContractError("catalogue session title is missing")
        html, context = await self._client.get_session_page(ref.session_id)
        page = parse_session_page(html)
        payload = await self._client.get_json(
            "anysession/anysession", context=context, elem_id=ref.session_id
        )
        identity = parse_session_identity(payload, session_id=ref.session_id)
        if ref.event_id is not None and ref.event_id != identity.event_id:
            raise QuickTicketsContractError("catalogue and session event IDs conflict")
        session = Session(
            SessionKey("quicktickets", self._client.theatre_alias, ref.session_id),
            identity.event_id,
            identity.hall_id,
            ref.title.strip(),
            page.start_dates[0],
        )
        self._sessions[session.key] = session
        return session

    @staticmethod
    def _verify_cached(cached: Session, refreshed: Session) -> None:
        if (
            cached.key != refreshed.key
            or cached.event_id != refreshed.event_id
            or cached.hall_id != refreshed.hall_id
            or cached.starts_at != refreshed.starts_at
        ):
            raise QuickTicketsContractError("fresh session details conflict with catalogue")
