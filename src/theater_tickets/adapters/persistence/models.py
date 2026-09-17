"""SQLAlchemy models kept separate from pure domain objects."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for persistence-only models."""


class BuyerModel(Base):
    __tablename__ = "buyers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SubscriptionModel(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    theatre_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    ticket_count: Mapped[int] = mapped_column(Integer, nullable=False)
    seat_profile_id: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)


class SessionModel(Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("provider", "theatre_alias", "provider_session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    theatre_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    hall_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CatalogueSnapshotModel(Base):
    __tablename__ = "catalogue_snapshots"
    __table_args__ = (UniqueConstraint("theatre_alias", "fingerprint"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    theatre_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)


class DiscoveryBatchModel(Base):
    __tablename__ = "discovery_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("catalogue_snapshots.id"), unique=True, nullable=False
    )
    discovered_session_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandidateModel(Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("buyer_id", "session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    discovery_batch_id: Mapped[str] = mapped_column(
        ForeignKey("discovery_batches.id"), nullable=False
    )
    tracking_state: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    current_cycle_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(255))
