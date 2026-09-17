"""Dependency ports owned by the application layer."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from theater_tickets.domain.models import Seat, Session, SessionKey


class Clock(Protocol):
    """Supplies UTC time so timing policies are deterministic in tests."""

    def now(self) -> datetime:
        """Return an aware UTC timestamp."""


class TheatreProvider(Protocol):
    """Read contract needed before any future checkout capability."""

    async def fetch_session(self, key: SessionKey) -> Session:
        """Return normalized immutable session details."""

    async def fetch_inventory(self, key: SessionKey) -> tuple[Seat, ...]:
        """Return an inventory whose availability is already resolved."""
