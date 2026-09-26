"""Explicitly opted-in live test: one payment link for each bookable session."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram import Bot
from sqlalchemy import select

from theater_tickets.adapters.persistence.booking import (
    SqlAlchemyBatchSummaryScheduler,
    SqlAlchemyBookingRepository,
)
from theater_tickets.adapters.persistence.checkout import SqlAlchemyCheckoutStageRecorder
from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    CandidateModel,
    CheckoutIntentModel,
    OrderModel,
    OutboxMessageModel,
    RenewalCycleModel,
    SessionModel,
)
from theater_tickets.adapters.persistence.outbox import (
    SqlAlchemyOrderOutboxWriter,
    SqlAlchemyOutboxRepository,
)
from theater_tickets.adapters.persistence.renewals import SqlAlchemyRenewalRepository
from theater_tickets.adapters.persistence.repositories import SubscriptionRepository
from theater_tickets.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from theater_tickets.adapters.quicktickets.browser import PlaywrightQuickTicketsBrowserDriver
from theater_tickets.adapters.quicktickets.checkout import (
    QuickTicketsBrowserCheckoutTransport,
    QuickTicketsCheckoutAdapter,
)
from theater_tickets.adapters.quicktickets.client import QuickTicketsClient
from theater_tickets.adapters.quicktickets.profiles import DirectorySeatProfileSource
from theater_tickets.adapters.quicktickets.provider import QuickTicketsProvider
from theater_tickets.adapters.telegram.notifier import AiogramNotificationTransport
from theater_tickets.application.booking import CandidateEvaluator, EvaluationState
from theater_tickets.application.checkout import CheckoutErrorCode, CheckoutStage
from theater_tickets.application.discovery import DiscoveryService
from theater_tickets.application.ports import TheatreProvider
from theater_tickets.domain.models import (
    BookingMode,
    Money,
    RenewalPolicy,
    SaleCapabilities,
    Seat,
    SeatAvailability,
    Session,
    SessionKey,
    Subscription,
)
from theater_tickets.operations.database import migrate_database, sqlite_path
from theater_tickets.settings import BookingMode as SettingsBookingMode
from theater_tickets.settings import Settings
from theater_tickets.workers.booking import BookingWorkflow, DryRunWorker, LiveCandidateProcessor
from theater_tickets.workers.outbox import OutboxWorker
from theater_tickets.workers.renewals import RenewalWorker


class _RecordingCheckoutStages:
    """Keep non-secret checkout milestones visible if a live test stops."""

    def __init__(self, delegate: SqlAlchemyCheckoutStageRecorder) -> None:
        self._delegate = delegate
        self.stages: list[str] = []

    async def record(
        self,
        intent_id: str,
        stage: CheckoutStage,
        error_code: CheckoutErrorCode | None = None,
    ) -> None:
        await self._delegate.record(intent_id, stage, error_code)
        self.stages.append(stage.value)


class _ExcludingSeatProvider:
    """Keep previous test seats out of a fresh selection without changing provider data."""

    def __init__(self, delegate: TheatreProvider, excluded_ids: frozenset[str]) -> None:
        self._delegate = delegate
        self._excluded_ids = excluded_ids

    async def fetch_session(self, key: SessionKey) -> Session:
        return await self._delegate.fetch_session(key)

    async def fetch_sale_capabilities(self, key: SessionKey) -> SaleCapabilities:
        return await self._delegate.fetch_sale_capabilities(key)

    async def fetch_inventory(self, key: SessionKey) -> tuple[Seat, ...]:
        seats = await self._delegate.fetch_inventory(key)
        return tuple(
            replace(seat, availability=SeatAvailability.HELD)
            if seat.provider_id in self._excluded_ids
            else seat
            for seat in seats
        )


def test_excluded_seat_cannot_be_selected_again() -> None:
    class FakeProvider:
        async def fetch_inventory(self, key: SessionKey) -> tuple[Seat, ...]:
            del key
            return (
                Seat("old", "hall", "Партер", "1", "1", Money(100), SeatAvailability.FREE),
                Seat("new", "hall", "Партер", "1", "2", Money(100), SeatAvailability.FREE),
            )

    provider = _ExcludingSeatProvider(FakeProvider(), frozenset({"old"}))
    seats = asyncio.run(provider.fetch_inventory(SessionKey("quicktickets", "theatre", "1")))
    assert [seat.availability for seat in seats] == [
        SeatAvailability.HELD,
        SeatAvailability.FREE,
    ]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"Live E2E requires {name}", pytrace=False)
    return value


def _configuration() -> tuple[Settings, str, Path, Money, Money]:
    if os.environ.get("RUN_LIVE_E2E") != "1":
        pytest.skip("live checkout is disabled; set RUN_LIVE_E2E=1 explicitly")
    if os.environ.get("LIVE_E2E_CONFIRM_ALL_SESSIONS") != "YES":
        pytest.fail(
            "Set LIVE_E2E_CONFIRM_ALL_SESSIONS=YES to permit all-session holds", pytrace=False
        )
    settings = Settings.from_environ()
    if settings.booking_mode is not SettingsBookingMode.LIVE:
        pytest.fail("Live E2E requires BOOKING_MODE=live", pytrace=False)
    _required("TELEGRAM_BOT_TOKEN")
    _required("ADMIN_TELEGRAM_USER_ID")
    _required("QUICKTICKETS_PAYMENT_TERMINAL_CHOICE")
    database_url = _required("LIVE_E2E_DATABASE_URL")
    path = sqlite_path(database_url)
    if path.exists():
        pytest.fail("LIVE_E2E_DATABASE_URL must point to a new database file", pytrace=False)
    if settings.database_url is not None and path == sqlite_path(settings.database_url):
        pytest.fail("Live E2E database must differ from DATABASE_URL", pytrace=False)
    max_ticket = Money.from_rubles(_required("LIVE_E2E_MAX_TICKET_PRICE_RUB"))
    max_order = Money.from_rubles(_required("LIVE_E2E_MAX_ORDER_TOTAL_RUB"))
    if max_ticket.minor_units == 0 or max_order.minor_units < max_ticket.minor_units:
        pytest.fail("Live E2E price limits must be positive and consistent", pytrace=False)
    for name in (
        "LIVE_E2E_LASTNAME",
        "LIVE_E2E_FIRSTNAME",
        "LIVE_E2E_MIDDLENAME",
        "LIVE_E2E_EMAIL",
        "LIVE_E2E_PHONE",
    ):
        _required(name)
    return settings, database_url, path, max_ticket, max_order


async def _run_live_e2e(
    settings: Settings,
    database_url: str,
    max_ticket: Money,
    max_order: Money,
) -> None:
    assert settings.telegram_bot_token is not None
    assert settings.administrator_telegram_user_id is not None
    assert settings.quicktickets_payment_terminal_choice is not None
    engine, factory = create_session_factory(database_url)
    client = QuickTicketsClient(theatre_alias=settings.theatre_alias, min_request_interval=0.25)
    bot = Bot(token=settings.telegram_bot_token)

    def now() -> datetime:
        return datetime.now(UTC)

    try:
        try:
            recipient = await bot.get_chat(chat_id=int(settings.administrator_telegram_user_id))
        except Exception:
            pytest.fail(
                "Telegram recipient chat is unavailable; open the bot in a private chat first",
                pytrace=False,
            )
        if recipient.type != "private":
            pytest.fail("Live E2E recipient must be a private Telegram chat", pytrace=False)
        provider = QuickTicketsProvider(client)
        catalogue = await provider.fetch_catalogue(settings.theatre_alias)
        assert catalogue.complete, "catalogue must be complete"
        future_sessions = tuple(item for item in catalogue.sessions if item.starts_at > now())
        selected_ids = {
            value.strip()
            for value in os.environ.get("LIVE_E2E_SESSION_IDS", "").split(",")
            if value.strip()
        }
        if selected_ids:
            available_ids = {item.key.session_id for item in future_sessions}
            assert selected_ids <= available_ids, (
                "requested E2E session is absent from future catalogue"
            )
            future_sessions = tuple(
                item for item in future_sessions if item.key.session_id in selected_ids
            )
        assert future_sessions, "catalogue has no future sessions"

        subscription = Subscription(
            subscription_id=str(uuid4()),
            buyer_id=settings.administrator_telegram_user_id,
            theatre_alias=settings.theatre_alias,
            ticket_count=1,
            seat_profile_id="auto",
            max_sessions_per_batch=None,
            max_ticket_price=max_ticket,
            max_order_total=max_order,
            max_active_orders=None,
            booking_mode=BookingMode.LIVE,
            renewal_policy=RenewalPolicy(max_cycles_per_session=1),
        )
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            repository = SubscriptionRepository(uow.session)
            await repository.add(
                subscription, telegram_chat_id=settings.administrator_telegram_user_id
            )
            await repository.save_buyer_profile(
                telegram_user_id=settings.administrator_telegram_user_id,
                telegram_chat_id=settings.administrator_telegram_user_id,
                lastname=_required("LIVE_E2E_LASTNAME"),
                firstname=_required("LIVE_E2E_FIRSTNAME"),
                middlename=_required("LIVE_E2E_MIDDLENAME"),
                email=_required("LIVE_E2E_EMAIL"),
                phone=_required("LIVE_E2E_PHONE"),
            )

        discovery = DiscoveryService()
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            baseline = await discovery.process(
                uow.session,
                subscription=subscription,
                sessions=(),
                fetched_at=now(),
                fingerprint=hashlib.sha256(b"live-e2e-empty-baseline").hexdigest(),
                complete=True,
            )
        assert baseline.baseline_established
        async with SqlAlchemyUnitOfWork(factory) as uow:
            assert uow.session is not None
            discovered = await discovery.process(
                uow.session,
                subscription=subscription,
                sessions=future_sessions,
                fetched_at=now(),
                fingerprint=catalogue.fingerprint,
                complete=True,
            )
        assert len(discovered.candidate_ids) == len(future_sessions)

        booking_repository = SqlAlchemyBookingRepository(factory)
        excluded_ids = frozenset(
            value.strip()
            for value in os.environ.get("LIVE_E2E_EXCLUDE_SEAT_IDS", "").split(",")
            if value.strip()
        )
        evaluator = CandidateEvaluator(
            provider=_ExcludingSeatProvider(provider, excluded_ids),
            profiles=DirectorySeatProfileSource(settings.hall_profiles_path),
        )
        bookable_ids: set[str] = set()
        for candidate_id in discovered.candidate_ids:
            context = await booking_repository.load(candidate_id)
            assert context is not None
            evaluation = await evaluator.evaluate(context, now=now())
            if evaluation.state is EvaluationState.NEEDS_ATTENTION:
                pytest.fail(
                    f"Read-only preflight requires attention for session "
                    f"{context.session.key.session_id}: {evaluation.reason}",
                    pytrace=False,
                )
            if evaluation.state is EvaluationState.READY:
                bookable_ids.add(context.session.key.session_id)
        assert bookable_ids, "no future session has a bookable seat within the price limits"

        stage_recorder = _RecordingCheckoutStages(SqlAlchemyCheckoutStageRecorder(factory, now=now))
        checkout = QuickTicketsCheckoutAdapter(
            booking_mode=BookingMode.LIVE,
            transport=QuickTicketsBrowserCheckoutTransport(
                lambda: PlaywrightQuickTicketsBrowserDriver(
                    theatre_alias=settings.theatre_alias,
                    payment_terminal_choice=settings.quicktickets_payment_terminal_choice or "",
                    now=now,
                )
            ),
            recorder=stage_recorder,
        )
        renewal_worker = RenewalWorker(
            repository=SqlAlchemyRenewalRepository(factory),
            processor=LiveCandidateProcessor(
                session_factory=factory,
                contexts=booking_repository,
                evaluator=evaluator,
                checkout=checkout,
                order_writer=SqlAlchemyOrderOutboxWriter(factory),
                failures=booking_repository,
                now=now,
            ),
        )
        workflow = BookingWorkflow(
            session_factory=factory,
            renewal_worker=renewal_worker,
            dry_run_worker=DryRunWorker(
                repository=booking_repository,
                contexts=booking_repository,
                evaluator=evaluator,
            ),
            outbox_worker=OutboxWorker(
                repository=SqlAlchemyOutboxRepository(factory),
                transport=AiogramNotificationTransport(bot),
            ),
            summaries=SqlAlchemyBatchSummaryScheduler(factory),
        )
        for _ in range((len(discovered.candidate_ids) + 19) // 20):
            outcome = await workflow.run_booking_due(now=now())
            if outcome.live_processed == 0:
                break
        for _ in range((len(discovered.candidate_ids) + 19) // 20 + 1):
            if await workflow.run_outbox_due(now=now()) == 0:
                break

        async with factory() as database:
            rows = (
                await database.execute(
                    select(
                        SessionModel.provider_session_id,
                        OrderModel.state,
                        OrderModel.actual_seat_ids,
                        OutboxMessageModel.state,
                        OutboxMessageModel.telegram_message_id,
                    )
                    .join(CandidateModel, CandidateModel.session_id == SessionModel.id)
                    .join(RenewalCycleModel, RenewalCycleModel.candidate_id == CandidateModel.id)
                    .join(
                        CheckoutIntentModel,
                        CheckoutIntentModel.renewal_cycle_id == RenewalCycleModel.id,
                    )
                    .join(OrderModel, OrderModel.checkout_intent_id == CheckoutIntentModel.id)
                    .join(OutboxMessageModel, OutboxMessageModel.order_id == OrderModel.id)
                    .where(CandidateModel.subscription_id == subscription.subscription_id)
                )
            ).all()
        actual_ids = [row[0] for row in rows]
        assert set(actual_ids) == bookable_ids, (
            f"Expected {len(bookable_ids)} booked sessions; got {len(set(actual_ids))}. "
            f"Checkout stages: {stage_recorder.stages}. "
            "Check the retained E2E database for unresolved intents."
        )
        assert len(actual_ids) == len(bookable_ids), "more than one order was created per session"
        assert all(
            state == "awaiting_payment"
            and len(seat_ids) == 1
            and message_state == "sent"
            and telegram_message_id
            for _, state, seat_ids, message_state, telegram_message_id in rows
        ), "a hold or Telegram payment notification was not confirmed"
    finally:
        await client.aclose()
        await bot.session.close()
        await engine.dispose()


@pytest.mark.live_e2e
def test_current_catalogue_one_live_ticket_and_payment_link_per_bookable_session() -> None:
    settings, database_url, path, max_ticket, max_order = _configuration()
    try:
        migrate_database(database_url)
        asyncio.run(_run_live_e2e(settings, database_url, max_ticket, max_order))
    except Exception as exc:
        pytest.fail(
            f"Live E2E stopped ({type(exc).__name__}); inspect retained database {path} "
            "before any new run. No failed checkout is retried automatically.",
            pytrace=False,
        )
