"""Independent resilient polling loops and their single-process lifecycle."""

from __future__ import annotations

import asyncio
import random
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from theater_tickets.application.runtime import (
    RecoveryCallback,
    RuntimeAlreadyRunning,
    RuntimeEventLog,
    RuntimeStateStore,
    WorkerCallback,
    WorkerRun,
    WorkerSchedule,
)


class ResilientPollingWorker:
    """Run one callback repeatedly with bounded backoff and interruptible waits."""

    def __init__(
        self,
        *,
        name: str,
        callback: WorkerCallback,
        schedule: WorkerSchedule,
        state_store: RuntimeStateStore,
        event_log: RuntimeEventLog,
        now: Callable[[], datetime] | None = None,
        jitter: Callable[[float, float], float] | None = None,
        wake_event: asyncio.Event | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("worker name must not be empty")
        self.name = name
        self._callback = callback
        self._schedule = schedule
        self._state_store = state_store
        self._event_log = event_log
        self._now = now or (lambda: datetime.now(UTC))
        self._jitter = jitter or random.uniform
        self._wake_event = wake_event
        self._monotonic = monotonic or time.monotonic
        self._failures = 0
        self._backoff = schedule.initial_backoff_seconds
        self._last_success: datetime | None = None

    async def execute_once(self) -> float:
        """Execute one iteration and return its deterministic next delay."""
        started = self._now()
        _validate_time(started)
        monotonic_started = self._monotonic()
        await self._record(
            WorkerRun(
                self.name,
                "running",
                started,
                started,
                self._failures,
                last_succeeded_at=self._last_success,
            )
        )
        try:
            await self._callback()
        except asyncio.CancelledError:
            cancelled_at = self._now()
            await self._record(
                WorkerRun(
                    self.name,
                    "interrupted",
                    started,
                    cancelled_at,
                    self._failures,
                    last_succeeded_at=self._last_success,
                    last_error_code="cancelled",
                    duration_ms=_duration_ms(monotonic_started, self._monotonic),
                )
            )
            raise
        except Exception as exc:
            self._failures += 1
            finished = self._now()
            error_code = _error_code(exc)
            delay = self._failure_delay(exc)
            next_run = finished + timedelta(seconds=delay)
            await self._record(
                WorkerRun(
                    self.name,
                    "backoff",
                    started,
                    finished,
                    self._failures,
                    next_run_at=next_run,
                    last_succeeded_at=self._last_success,
                    last_error_code=error_code,
                    duration_ms=_duration_ms(monotonic_started, self._monotonic),
                )
            )
            self._event_log.emit(
                "worker_failed",
                worker=self.name,
                error_code=error_code,
                attempt=self._failures,
                next_run_seconds=round(delay, 3),
            )
            return delay

        finished = self._now()
        self._failures = 0
        self._backoff = self._schedule.initial_backoff_seconds
        self._last_success = finished
        interval = max(
            0.0,
            self._schedule.interval_seconds
            + self._jitter(-self._schedule.jitter_seconds, self._schedule.jitter_seconds),
        )
        delay = (
            max(0.0, interval - (self._monotonic() - monotonic_started))
            if self._schedule.start_to_start
            else interval
        )
        next_run = finished + timedelta(seconds=delay)
        await self._record(
            WorkerRun(
                self.name,
                "sleeping",
                started,
                finished,
                0,
                next_run_at=next_run,
                last_succeeded_at=finished,
                duration_ms=_duration_ms(monotonic_started, self._monotonic),
            )
        )
        self._event_log.emit(
            "worker_succeeded",
            worker=self.name,
            duration_ms=_duration_ms(monotonic_started, self._monotonic),
            next_run_seconds=round(delay, 3),
        )
        return delay

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self._wake_event is not None:
                self._wake_event.clear()
            delay = await self.execute_once()
            if self._wake_event is None or self._failures:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    continue
            else:
                stop_wait = asyncio.create_task(stop.wait())
                wake_wait = asyncio.create_task(self._wake_event.wait())
                try:
                    await asyncio.wait(
                        (stop_wait, wake_wait),
                        timeout=delay,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in (stop_wait, wake_wait):
                        task.cancel()
                    await asyncio.gather(stop_wait, wake_wait, return_exceptions=True)

    def _failure_delay(self, exc: Exception) -> float:
        retry_after = getattr(exc, "retry_after_seconds", None)
        if isinstance(retry_after, int | float) and not isinstance(retry_after, bool):
            return max(float(retry_after), self._schedule.initial_backoff_seconds)
        delay = self._backoff
        self._backoff = min(self._backoff * 2, self._schedule.max_backoff_seconds)
        return delay

    async def _record(self, run: WorkerRun) -> None:
        try:
            await self._state_store.record_worker(run)
        except Exception:
            self._event_log.emit(
                "runtime_diagnostic_write_failed",
                worker=self.name,
                error_code="diagnostic_store_error",
            )


class RuntimeSupervisor:
    """Own worker tasks, recovery, lease heartbeat and bounded shutdown."""

    def __init__(
        self,
        *,
        workers: tuple[ResilientPollingWorker, ...],
        recoveries: tuple[RecoveryCallback, ...],
        state_store: RuntimeStateStore,
        event_log: RuntimeEventLog,
        lease_seconds: int = 30,
        shutdown_grace_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
        owner_id: str | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("runtime lease must be positive")
        if shutdown_grace_seconds <= 0:
            raise ValueError("shutdown grace must be positive")
        names = [worker.name for worker in workers]
        if len(names) != len(set(names)):
            raise ValueError("runtime worker names must be unique")
        self._workers = workers
        self._recoveries = recoveries
        self._state_store = state_store
        self._event_log = event_log
        self._lease_seconds = lease_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._owner_id = owner_id or str(uuid4())
        self._stop = asyncio.Event()
        self._lease_lost = False

    def request_shutdown(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        now = self._now()
        if not await self._state_store.acquire_lock(
            owner_id=self._owner_id,
            now=now,
            lease_seconds=self._lease_seconds,
        ):
            raise RuntimeAlreadyRunning("another theater_tickets runtime owns the database lock")
        self._event_log.emit("runtime_started", owner_id=self._owner_id)
        tasks: list[asyncio.Task[None]] = []
        heartbeat: asyncio.Task[None] | None = None
        recovery_task: asyncio.Task[None] | None = None
        stop_wait = asyncio.create_task(self._stop.wait())
        try:
            heartbeat = asyncio.create_task(self._heartbeat(), name="runtime:heartbeat")
            recovery_task = asyncio.create_task(self._recover(), name="runtime:recovery")
            await asyncio.wait((recovery_task, stop_wait), return_when=asyncio.FIRST_COMPLETED)
            if self._stop.is_set():
                return
            await recovery_task
            tasks = [
                asyncio.create_task(worker.run(self._stop), name=f"worker:{worker.name}")
                for worker in self._workers
            ]
            done, _ = await asyncio.wait((*tasks, stop_wait), return_when=asyncio.FIRST_COMPLETED)
            if not self._stop.is_set():
                for task in tasks:
                    if task in done:
                        raise RuntimeError(
                            "runtime worker stopped unexpectedly"
                        ) from task.exception()
        finally:
            self._stop.set()
            if recovery_task is not None:
                recovery_task.cancel()
                await asyncio.gather(recovery_task, return_exceptions=True)
            if tasks:
                deadline = asyncio.get_running_loop().time() + self._shutdown_grace_seconds
                pending = set(tasks)
                while pending and not self._lease_lost:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    watched = (*pending, heartbeat) if heartbeat is not None else tuple(pending)
                    done, _ = await asyncio.wait(
                        watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                    )
                    pending.difference_update(done)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await asyncio.gather(*tasks, return_exceptions=True)
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
            await self._state_store.release_lock(owner_id=self._owner_id)
            self._event_log.emit("runtime_stopped", owner_id=self._owner_id)

    async def _recover(self) -> None:
        for recovery in self._recoveries:
            await recovery()

    async def _heartbeat(self) -> None:
        interval = self._lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            try:
                owned = await self._state_store.heartbeat(
                    owner_id=self._owner_id,
                    now=self._now(),
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                self._event_log.emit("runtime_lock_lost", error_code="heartbeat_failed")
                self._lease_lost = True
                self._stop.set()
                return
            if not owned:
                self._event_log.emit("runtime_lock_lost", error_code="lease_not_owned")
                self._lease_lost = True
                self._stop.set()
                return


async def run_until_signalled(supervisor: RuntimeSupervisor) -> None:
    """Translate SIGINT/SIGTERM into the supervisor's bounded shutdown path."""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, supervisor.request_shutdown)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    try:
        await supervisor.run()
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)


def _duration_ms(started: float, monotonic: Callable[[], float]) -> int:
    return max(0, int((monotonic() - started) * 1000))


def _error_code(exc: Exception) -> str:
    name = type(exc).__name__
    parts: list[str] = []
    current = ""
    for char in name:
        if char.isupper() and current:
            parts.append(current)
            current = char.lower()
        else:
            current += char.lower()
    if current:
        parts.append(current)
    return "_".join(parts)[:64]


def _validate_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime clock must return a timezone-aware timestamp")
