"""Concrete dry-run production composition used by the deployment entrypoint."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot

from theater_tickets.adapters.persistence.booking import (
    SqlAlchemyBatchSummaryScheduler,
    SqlAlchemyBookingRepository,
    SqlAlchemyConfirmedRecoveryHandler,
)
from theater_tickets.adapters.persistence.checkout import SqlAlchemyCheckoutStageRecorder
from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.outbox import (
    SqlAlchemyOrderOutboxWriter,
    SqlAlchemyOutboxRepository,
)
from theater_tickets.adapters.persistence.reconciliation import (
    SqlAlchemyRecoveryRepository,
    SqlAlchemyRecoveryRequestLoader,
)
from theater_tickets.adapters.persistence.renewals import SqlAlchemyRenewalRepository
from theater_tickets.adapters.persistence.runtime import (
    SqlAlchemyActiveSubscriptionSource,
    SqlAlchemyRuntimeStateStore,
)
from theater_tickets.adapters.quicktickets.browser import PlaywrightQuickTicketsBrowserDriver
from theater_tickets.adapters.quicktickets.checkout import (
    QuickTicketsBrowserCheckoutTransport,
    QuickTicketsCheckoutAdapter,
)
from theater_tickets.adapters.quicktickets.client import QuickTicketsClient
from theater_tickets.adapters.quicktickets.profiles import DirectorySeatProfileSource
from theater_tickets.adapters.quicktickets.provider import QuickTicketsProvider
from theater_tickets.adapters.quicktickets.reconciliation import (
    UnsupportedQuickTicketsReconciliationTransport,
)
from theater_tickets.adapters.telegram.app import build_dispatcher
from theater_tickets.adapters.telegram.notifier import AiogramNotificationTransport
from theater_tickets.application.booking import CandidateEvaluator
from theater_tickets.application.checkout import (
    CheckoutRequest,
    CheckoutStageRecorder,
    CheckoutWriteTransport,
    ProviderCheckoutObservation,
)
from theater_tickets.application.reconciliation import CheckoutRecoveryService
from theater_tickets.bootstrap import RuntimeCallbacks, create_runtime_supervisor
from theater_tickets.domain.models import BookingMode
from theater_tickets.settings import Settings
from theater_tickets.workers.booking import BookingWorkflow, DryRunWorker, LiveCandidateProcessor
from theater_tickets.workers.outbox import OutboxWorker
from theater_tickets.workers.polling import CataloguePollingWorker
from theater_tickets.workers.renewals import RenewalWorker
from theater_tickets.workers.runtime import run_until_signalled


class _DisabledWriteTransport:
    async def submit(
        self,
        request: CheckoutRequest,
        *,
        recorder: CheckoutStageRecorder,
    ) -> ProviderCheckoutObservation:
        del request, recorder
        raise AssertionError("checkout write transport is disabled in production dry-run")


async def run_production(settings: Settings) -> None:
    """Run the composed dry-run runtime and close every owned external resource."""
    settings.validate_runtime()
    assert settings.database_url is not None
    assert settings.telegram_bot_token is not None
    assert settings.administrator_telegram_user_id is not None
    engine, session_factory = create_session_factory(settings.database_url)
    bot = Bot(token=settings.telegram_bot_token)
    client = QuickTicketsClient(
        theatre_alias=settings.theatre_alias,
        min_request_interval=0.25,
    )

    def now() -> datetime:
        return datetime.now(UTC)

    try:
        provider = QuickTicketsProvider(client)
        profiles = DirectorySeatProfileSource(settings.hall_profiles_path)
        booking_repository = SqlAlchemyBookingRepository(session_factory)
        evaluator = CandidateEvaluator(provider=provider, profiles=profiles)
        recorder = SqlAlchemyCheckoutStageRecorder(session_factory, now=now)
        runtime_booking_mode = BookingMode(settings.booking_mode.value)
        write_transport: CheckoutWriteTransport
        if runtime_booking_mode is BookingMode.LIVE:
            assert settings.quicktickets_payment_terminal_choice is not None
            write_transport = QuickTicketsBrowserCheckoutTransport(
                lambda: PlaywrightQuickTicketsBrowserDriver(
                    theatre_alias=settings.theatre_alias,
                    payment_terminal_choice=settings.quicktickets_payment_terminal_choice or "",
                    now=now,
                )
            )
        else:
            write_transport = _DisabledWriteTransport()
        checkout = QuickTicketsCheckoutAdapter(
            booking_mode=runtime_booking_mode,
            transport=write_transport,
            recorder=recorder,
        )

        renewal_repository = SqlAlchemyRenewalRepository(session_factory)
        renewal_worker = RenewalWorker(
            repository=renewal_repository,
            processor=LiveCandidateProcessor(
                session_factory=session_factory,
                contexts=booking_repository,
                evaluator=evaluator,
                checkout=checkout,
                order_writer=SqlAlchemyOrderOutboxWriter(session_factory),
                failures=booking_repository,
                now=now,
            ),
        )
        dry_run_worker = DryRunWorker(
            repository=booking_repository,
            contexts=booking_repository,
            evaluator=evaluator,
        )
        outbox_worker = OutboxWorker(
            repository=SqlAlchemyOutboxRepository(session_factory),
            transport=AiogramNotificationTransport(bot),
        )
        recovery_loader = SqlAlchemyRecoveryRequestLoader(session_factory)
        recovery = CheckoutRecoveryService(
            repository=SqlAlchemyRecoveryRepository(session_factory, now=now),
            request_loader=recovery_loader,
            transport=UnsupportedQuickTicketsReconciliationTransport(),
            validator=checkout,
            confirmed_handler=SqlAlchemyConfirmedRecoveryHandler(
                session_factory,
                now=now,
            ),
        )
        workflow = BookingWorkflow(
            session_factory=session_factory,
            renewal_worker=renewal_worker,
            dry_run_worker=dry_run_worker,
            outbox_worker=outbox_worker,
            summaries=SqlAlchemyBatchSummaryScheduler(session_factory),
            checkout_recovery=recovery,
        )
        catalogue = CataloguePollingWorker(
            session_factory=session_factory,
            source=provider,
            subscriptions=SqlAlchemyActiveSubscriptionSource(session_factory),
        )
        dispatcher = build_dispatcher(
            administrator_user_id=settings.administrator_telegram_user_id,
            session_factory=session_factory,
            catalogue_stale_after_seconds=settings.poll_interval_seconds * 3,
            booking_mode=settings.booking_mode,
        )

        async def poll_catalogue() -> object:
            return await catalogue.run_once(now=now())

        async def process_booking() -> object:
            return await workflow.run_booking_due(now=now())

        async def deliver_outbox() -> object:
            return await workflow.run_outbox_due(now=now())

        async def poll_telegram() -> object:
            await dispatcher.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
            )
            return 0

        async def recover() -> object:
            await workflow.recover_startup(now=now())
            return 0

        supervisor = create_runtime_supervisor(
            settings=settings,
            callbacks=RuntimeCallbacks(
                catalogue=poll_catalogue,
                booking=process_booking,
                outbox=deliver_outbox,
                telegram=poll_telegram,
                recoveries=(recover,),
            ),
            state_store=SqlAlchemyRuntimeStateStore(session_factory),
        )
        await run_until_signalled(supervisor)
    finally:
        await client.aclose()
        await bot.session.close()
        await engine.dispose()
