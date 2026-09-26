"""Composition helpers for diagnostics and the background runtime lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from theater_tickets.adapters.logging import SafeJsonLogger
from theater_tickets.application.runtime import RecoveryCallback, RuntimeStateStore, WorkerSchedule
from theater_tickets.settings import Settings
from theater_tickets.workers.runtime import ResilientPollingWorker, RuntimeSupervisor


@dataclass(frozen=True, slots=True)
class Application:
    """A safe application shell with no I/O side effects."""

    settings: Settings

    def status_lines(self) -> tuple[str, ...]:
        """Return operational status without rendering any configured secret."""
        return (
            "theater_tickets: initialized",
            f"booking_mode={self.settings.booking_mode}",
            f"poll_interval_seconds={self.settings.poll_interval_seconds}",
            f"poll_jitter_seconds={self.settings.poll_jitter_seconds}",
            f"worker_interval_seconds={self.settings.worker_interval_seconds}",
            f"runtime_max_backoff_seconds={self.settings.runtime_max_backoff_seconds}",
            f"runtime_lock_lease_seconds={self.settings.runtime_lock_lease_seconds}",
            f"shutdown_grace_seconds={self.settings.shutdown_grace_seconds}",
            f"renewal_interval_seconds={self.settings.renewal_interval_seconds}",
            f"expected_hold_ttl_seconds={self.settings.expected_hold_ttl_seconds}",
            f"availability_retry_seconds={self.settings.availability_retry_seconds}",
            f"telegram_token_configured={self.settings.telegram_token_configured}",
            f"administrator_configured={self.settings.administrator_configured}",
            f"database_url_configured={self.settings.database_url_configured}",
            "network_operations=disabled",
            "ticket_operations=disabled",
        )


def create_application(settings: Settings | None = None) -> Application:
    """Compose the initial shell without starting adapters or workers."""
    return Application(settings=Settings.from_environ() if settings is None else settings)


@dataclass(frozen=True, slots=True)
class RuntimeCallbacks:
    """Already-composed I/O use cases owned by independent runtime loops."""

    catalogue: Callable[[], Awaitable[object]]
    booking: Callable[[], Awaitable[object]]
    outbox: Callable[[], Awaitable[object]]
    telegram: Callable[[], Awaitable[object]]
    recoveries: tuple[RecoveryCallback, ...] = ()


def create_runtime_supervisor(
    *,
    settings: Settings,
    callbacks: RuntimeCallbacks,
    state_store: RuntimeStateStore,
    logger: logging.Logger | None = None,
    booking_wake_event: asyncio.Event | None = None,
    outbox_wake_event: asyncio.Event | None = None,
) -> RuntimeSupervisor:
    """Compose isolated polling tasks around concrete adapters supplied by deployment."""
    event_log = SafeJsonLogger(logger or logging.getLogger("theater_tickets.runtime"))
    workers = (
        ResilientPollingWorker(
            name="catalogue",
            callback=callbacks.catalogue,
            schedule=WorkerSchedule(
                settings.poll_interval_seconds,
                jitter_seconds=settings.poll_jitter_seconds,
                start_to_start=True,
                max_backoff_seconds=settings.runtime_max_backoff_seconds,
            ),
            state_store=state_store,
            event_log=event_log,
        ),
        ResilientPollingWorker(
            name="booking",
            callback=callbacks.booking,
            schedule=WorkerSchedule(
                settings.worker_interval_seconds,
                max_backoff_seconds=settings.runtime_max_backoff_seconds,
            ),
            state_store=state_store,
            event_log=event_log,
            wake_event=booking_wake_event,
        ),
        ResilientPollingWorker(
            name="outbox",
            callback=callbacks.outbox,
            schedule=WorkerSchedule(
                settings.worker_interval_seconds,
                max_backoff_seconds=settings.runtime_max_backoff_seconds,
            ),
            state_store=state_store,
            event_log=event_log,
            wake_event=outbox_wake_event,
        ),
        ResilientPollingWorker(
            name="telegram",
            callback=callbacks.telegram,
            schedule=WorkerSchedule(
                settings.worker_interval_seconds,
                max_backoff_seconds=settings.runtime_max_backoff_seconds,
            ),
            state_store=state_store,
            event_log=event_log,
        ),
    )
    return RuntimeSupervisor(
        workers=workers,
        recoveries=callbacks.recoveries,
        state_store=state_store,
        event_log=event_log,
        lease_seconds=settings.runtime_lock_lease_seconds,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
    )
