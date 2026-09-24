"""SQLAlchemy models kept separate from pure domain objects."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for persistence-only models."""


class BuyerModel(Base):
    __tablename__ = "buyers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    telegram_user_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    lastname: Mapped[str | None] = mapped_column(String(255))
    firstname: Mapped[str | None] = mapped_column(String(255))
    middlename: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))
    personal_data_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TelegramAccessModel(Base):
    """A user request approved or rejected by the configured administrator."""

    __tablename__ = "telegram_access"

    telegram_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class SubscriptionBaselineModel(Base):
    """The complete catalogue snapshot from which a rule starts observing."""

    __tablename__ = "subscription_baselines"

    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("catalogue_snapshots.id"), nullable=False)
    established_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    subscription_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    booking_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="dry_run")
    tracking_state: Mapped[str] = mapped_column(String(64), nullable=False, default="queued")
    current_cycle_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_reason: Mapped[str | None] = mapped_column(String(255))


class RenewalCycleModel(Base):
    """One planned booking cycle; retries remain within the same cycle."""

    __tablename__ = "renewal_cycles"
    __table_args__ = (UniqueConstraint("candidate_id", "cycle_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    cycle_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allocation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckoutIntentModel(Base):
    """A durable record written before a future changing provider request."""

    __tablename__ = "checkout_intents"
    __table_args__ = (UniqueConstraint("renewal_cycle_id", "attempt_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    renewal_cycle_id: Mapped[str] = mapped_column(ForeignKey("renewal_cycles.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_seat_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reserved_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    expected_hold_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1200)
    remote_stage: Mapped[str] = mapped_column(String(64), nullable=False, default="planned")
    write_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BudgetAllocationModel(Base):
    """The locally reserved upper bound for one checkout intent."""

    __tablename__ = "budget_allocations"
    __table_args__ = (UniqueConstraint("checkout_intent_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    discovery_batch_id: Mapped[str] = mapped_column(
        ForeignKey("discovery_batches.id"), nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), nullable=False)
    checkout_intent_id: Mapped[str] = mapped_column(
        ForeignKey("checkout_intents.id"), nullable=False
    )
    reserved_total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderModel(Base):
    """Provider order history, including locally expired cycles."""

    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("checkout_intent_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkout_intent_id: Mapped[str] = mapped_column(
        ForeignKey("checkout_intents.id"), nullable=False
    )
    provider_order_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_seat_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="RUB")
    payment_url: Mapped[str | None] = mapped_column(Text)
    held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxMessageModel(Base):
    """A durable notification reference; secret URLs remain on the related order."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("dedup_key"),
        Index("ix_outbox_due", "state", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    discovery_batch_id: Mapped[str | None] = mapped_column(ForeignKey("discovery_batches.id"))
    buyer_id: Mapped[str] = mapped_column(ForeignKey("buyers.id"), nullable=False)
    destination_chat_id: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DryRunReportModel(Base):
    """One durable seat-selection decision that can never become a live intent."""

    __tablename__ = "dry_run_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id"), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_seat_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    total_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(3))
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeLockModel(Base):
    """A renewable lease preventing a second local runtime from starting."""

    __tablename__ = "runtime_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeWorkerModel(Base):
    """Latest safe diagnostic state for one independently scheduled worker."""

    __tablename__ = "runtime_workers"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_duration_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
