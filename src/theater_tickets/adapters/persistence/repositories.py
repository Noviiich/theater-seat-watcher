"""Small repositories used by persistence and migration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from theater_tickets.adapters.persistence.models import (
    BuyerModel,
    CandidateModel,
    CatalogueSnapshotModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.domain.models import BookingMode, Money, RenewalPolicy, Session, Subscription


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


class SubscriptionRepository:
    """Persist Telegram-owned subscriptions without leaking ORM into handlers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_buyer(
        self, *, telegram_user_id: str, telegram_chat_id: str
    ) -> BuyerModel:
        buyer = await self._session.scalar(
            select(BuyerModel).where(BuyerModel.telegram_user_id == telegram_user_id)
        )
        if buyer is None:
            buyer = BuyerModel(
                id=str(uuid4()),
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                created_at=datetime.now(UTC),
            )
            self._session.add(buyer)
            await self._session.flush()
        elif buyer.telegram_chat_id != telegram_chat_id:
            buyer.telegram_chat_id = telegram_chat_id
        return buyer

    async def add(self, value: Subscription, *, telegram_chat_id: str) -> Subscription:
        buyer = await self.get_or_create_buyer(
            telegram_user_id=value.buyer_id, telegram_chat_id=telegram_chat_id
        )
        self._session.add(
            SubscriptionModel(
                id=value.subscription_id,
                buyer_id=buyer.id,
                theatre_alias=value.theatre_alias,
                ticket_count=value.ticket_count,
                seat_profile_id=value.seat_profile_id,
                enabled=value.enabled,
                config=_subscription_config(value),
            )
        )
        return value

    async def list_for_telegram_user(self, telegram_user_id: str) -> tuple[Subscription, ...]:
        rows = (
            await self._session.scalars(
                select(SubscriptionModel)
                .join(BuyerModel)
                .where(BuyerModel.telegram_user_id == telegram_user_id)
                .order_by(SubscriptionModel.id)
            )
        ).all()
        return tuple(_subscription_from_model(row, telegram_user_id) for row in rows)

    async def set_enabled(
        self, *, subscription_id: str, telegram_user_id: str, enabled: bool
    ) -> bool:
        row = await self._owned_subscription(subscription_id, telegram_user_id)
        if row is None:
            return False
        row.enabled = enabled
        row.version += 1
        return True

    async def stop_candidate(self, *, candidate_id: str, telegram_user_id: str) -> bool:
        candidate = await self._session.scalar(
            select(CandidateModel)
            .join(BuyerModel)
            .where(
                CandidateModel.id == candidate_id, BuyerModel.telegram_user_id == telegram_user_id
            )
        )
        if candidate is None:
            return False
        candidate.tracking_state = "stopped"
        candidate.stop_reason = "user_stop"
        candidate.next_run_at = None
        return True

    async def _owned_subscription(
        self, subscription_id: str, telegram_user_id: str
    ) -> SubscriptionModel | None:
        result = await self._session.scalar(
            select(SubscriptionModel)
            .join(BuyerModel)
            .where(
                SubscriptionModel.id == subscription_id,
                BuyerModel.telegram_user_id == telegram_user_id,
            )
        )
        return result if isinstance(result, SubscriptionModel) else None


def _subscription_config(value: Subscription) -> dict[str, object]:
    return {
        "max_sessions_per_batch": value.max_sessions_per_batch,
        "max_ticket_price": value.max_ticket_price.minor_units if value.max_ticket_price else None,
        "max_order_total": value.max_order_total.minor_units if value.max_order_total else None,
        "max_batch_total": value.max_batch_total.minor_units if value.max_batch_total else None,
        "max_active_orders": value.max_active_orders,
        "max_active_total": value.max_active_total.minor_units if value.max_active_total else None,
        "priority": value.priority,
        "booking_mode": value.booking_mode.value,
        "event_ids": sorted(value.event_ids),
        "title_filter": value.title_filter,
    }


def _subscription_from_model(model: SubscriptionModel, telegram_user_id: str) -> Subscription:
    config = model.config

    def money(name: str) -> Money | None:
        value = config.get(name)
        return Money(value) if isinstance(value, int) else None

    def integer(name: str, default: int) -> int:
        value = config.get(name, default)
        return value if isinstance(value, int) else default

    event_ids = config.get("event_ids", [])
    title_filter = config.get("title_filter")

    return Subscription(
        subscription_id=model.id,
        buyer_id=telegram_user_id,
        theatre_alias=model.theatre_alias,
        ticket_count=model.ticket_count,
        seat_profile_id=model.seat_profile_id,
        max_sessions_per_batch=integer("max_sessions_per_batch", 1),
        max_ticket_price=money("max_ticket_price"),
        max_order_total=money("max_order_total"),
        max_batch_total=money("max_batch_total"),
        max_active_orders=integer("max_active_orders", 1),
        max_active_total=money("max_active_total"),
        priority=integer("priority", 0),
        enabled=model.enabled,
        booking_mode=BookingMode(str(config.get("booking_mode", "dry_run"))),
        event_ids=frozenset(str(item) for item in event_ids)
        if isinstance(event_ids, list)
        else frozenset(),
        title_filter=title_filter if isinstance(title_filter, str) else None,
        renewal_policy=RenewalPolicy(),
    )
