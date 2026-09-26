from __future__ import annotations

import asyncio
from typing import Any

import pytest

from theater_tickets.adapters.quicktickets.client import QuickTicketsContext
from theater_tickets.adapters.quicktickets.errors import QuickTicketsContractError
from theater_tickets.adapters.quicktickets.provider import (
    QuickTicketsProvider,
    parse_session_identity,
)
from theater_tickets.domain.models import SeatAvailability, Session


class FakeClient:
    theatre_alias = "theatre"

    async def get_catalogue_page(self) -> str:
        return (
            '<div data-elem-type="event" data-elem-id="386" '
            'data-filter-name="Спектакль">'
            '<a href="/theatre/s3159" data-date="2026-10-24">24 октября</a>'
            "</div>"
        )

    async def get_session_page(self, session_id: str) -> tuple[str, QuickTicketsContext]:
        assert session_id == "3159"
        return (
            '<script type="application/ld+json">{"startDate":"2026-10-24T16:00:00+03:00"}</script>',
            QuickTicketsContext("token", "https://example.test/s3159"),
        )

    async def get_context(self, session_id: str) -> QuickTicketsContext:
        assert session_id == "3159"
        return QuickTicketsContext("token", "https://example.test/s3159")

    async def get_json(
        self,
        path: str,
        *,
        context: QuickTicketsContext,
        elem_id: str,
        elem_type: str = "session",
    ) -> dict[str, Any]:
        del context, elem_type
        assert elem_id == "3159"
        if path == "anysession/anysession":
            return {
                "response": {
                    "events": [
                        {
                            "id": 386,
                            "sessions": [
                                {
                                    "id": 3159,
                                    "hall_id": 16,
                                    "sell": {"available": True, "max": 4},
                                    "book": {"available": False, "max": 0},
                                    "collectiveSell": {"available": False},
                                }
                            ],
                        }
                    ]
                }
            }
        if path == "hall/hall":
            return {
                "response": {
                    "hall_id": 16,
                    "places": [
                        {
                            "id": 1,
                            "block": "Партер",
                            "series": "1",
                            "place": "1",
                            "price": 700,
                            "status": "free",
                            "x": 1,
                            "y": 2,
                        }
                    ],
                    "entranceplaces": [],
                }
            }
        if path == "anyticket/anyticket":
            return {"response": {"places": {"1": {"status": "free"}}}}
        raise AssertionError(path)


def test_provider_builds_complete_catalogue_and_refreshes_public_reads() -> None:
    async def scenario() -> None:
        provider = QuickTicketsProvider(FakeClient())  # type: ignore[arg-type]
        catalogue = await provider.fetch_catalogue("theatre")
        assert catalogue.complete
        assert len(catalogue.fingerprint) == 64
        session = catalogue.sessions[0]
        assert session.event_id == "386"
        assert session.hall_id == "16"
        assert session.title == "Спектакль"
        assert await provider.fetch_session(session.key) == session
        inventory = await provider.fetch_inventory(session.key)
        assert len(inventory) == 1
        assert inventory[0].availability is SeatAvailability.FREE
        assert (await provider.fetch_sale_capabilities(session.key)).sell_max == 4

    asyncio.run(scenario())


def test_provider_reuses_known_sessions_until_full_refresh() -> None:
    class CountingClient(FakeClient):
        def __init__(self) -> None:
            self.session_pages = 0
            self.session_details = 0

        async def get_session_page(self, session_id: str) -> tuple[str, QuickTicketsContext]:
            self.session_pages += 1
            return await super().get_session_page(session_id)

        async def get_json(
            self,
            path: str,
            *,
            context: QuickTicketsContext,
            elem_id: str,
            elem_type: str = "session",
        ) -> dict[str, Any]:
            if path == "anysession/anysession":
                self.session_details += 1
            return await super().get_json(
                path, context=context, elem_id=elem_id, elem_type=elem_type
            )

    async def scenario() -> None:
        known: dict[str, Session] = {}

        async def load_known(_: str) -> dict[str, Session]:
            return known

        client = CountingClient()
        provider = QuickTicketsProvider(client, known_sessions=load_known)  # type: ignore[arg-type]
        first = await provider.fetch_catalogue("theatre")
        known = {item.key.session_id: item for item in first.sessions}
        second = await provider.fetch_catalogue("theatre")
        assert second.fingerprint == first.fingerprint
        assert (client.session_pages, client.session_details) == (1, 1)

        full = QuickTicketsProvider(  # type: ignore[arg-type]
            client, known_sessions=load_known, full_refresh_seconds=0
        )
        await full.fetch_catalogue("theatre")
        assert (client.session_pages, client.session_details) == (2, 2)

    asyncio.run(scenario())


def test_session_identity_rejects_missing_or_duplicated_explicit_ids() -> None:
    with pytest.raises(QuickTicketsContractError, match="explicit"):
        parse_session_identity(
            {"response": {"events": [{"id": 1, "sessions": [{"id": 2}]}]}},
            session_id="2",
        )
    with pytest.raises(QuickTicketsContractError, match="duplicated"):
        parse_session_identity(
            {
                "response": {
                    "events": [
                        {"id": 1, "sessions": [{"id": 2, "hall_id": 3}]},
                        {"id": 1, "sessions": [{"id": 2, "hall_id": 3}]},
                    ]
                }
            },
            session_id="2",
        )
