from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from io import StringIO

from theater_tickets.adapters.logging import SafeJsonLogger
from theater_tickets.application.runtime import WorkerRun, WorkerSchedule
from theater_tickets.application.status import (
    CandidateStatus,
    RuntimeStatus,
    render_status,
)
from theater_tickets.workers.runtime import ResilientPollingWorker, RuntimeSupervisor


class MemoryStateStore:
    def __init__(self) -> None:
        self.owner: str | None = None
        self.runs: list[WorkerRun] = []

    async def acquire_lock(self, *, owner_id: str, now: datetime, lease_seconds: int) -> bool:
        del now, lease_seconds
        if self.owner not in (None, owner_id):
            return False
        self.owner = owner_id
        return True

    async def heartbeat(self, *, owner_id: str, now: datetime, lease_seconds: int) -> bool:
        del now, lease_seconds
        return self.owner == owner_id

    async def release_lock(self, *, owner_id: str) -> None:
        if self.owner == owner_id:
            self.owner = None

    async def record_worker(self, run: WorkerRun) -> None:
        self.runs.append(run)


class MemoryLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class RateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds


def test_catalogue_schedule_measures_interval_between_starts() -> None:
    async def scenario() -> None:
        elapsed = 0.0
        durations = iter((0.5, 3.0))

        async def callback() -> None:
            nonlocal elapsed
            elapsed += next(durations)

        worker = ResilientPollingWorker(
            name="catalogue",
            callback=callback,
            schedule=WorkerSchedule(2.5, start_to_start=True),
            state_store=MemoryStateStore(),
            event_log=MemoryLog(),
            now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
            monotonic=lambda: elapsed,
        )
        assert await worker.execute_once() == 2.0
        assert await worker.execute_once() == 0.0

    asyncio.run(scenario())


def test_worker_uses_exponential_backoff_retry_after_and_resets_after_success() -> None:
    async def scenario() -> None:
        outcomes: list[Exception | None] = [RuntimeError("private detail"), RuntimeError(), None]

        async def callback() -> None:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome

        now = datetime(2026, 9, 21, tzinfo=UTC)
        store = MemoryStateStore()
        log = MemoryLog()
        worker = ResilientPollingWorker(
            name="catalogue",
            callback=callback,
            schedule=WorkerSchedule(60, jitter_seconds=10, initial_backoff_seconds=2),
            state_store=store,
            event_log=log,
            now=lambda: now,
            jitter=lambda low, high: 5,
        )
        assert await worker.execute_once() == 2
        assert await worker.execute_once() == 4
        assert await worker.execute_once() == 65
        assert store.runs[-1].consecutive_failures == 0
        assert log.events[0][1]["error_code"] == "runtime_error"
        assert "private detail" not in repr(log.events)

        async def limited() -> None:
            raise RateLimited(45)

        limited_worker = ResilientPollingWorker(
            name="telegram",
            callback=limited,
            schedule=WorkerSchedule(5, max_backoff_seconds=30),
            state_store=store,
            event_log=log,
            now=lambda: now,
        )
        assert await limited_worker.execute_once() == 45

    asyncio.run(scenario())


def test_supervisor_keeps_catalogue_running_and_cancels_slow_checkout_on_deadline() -> None:
    async def scenario() -> None:
        store = MemoryStateStore()
        log = MemoryLog()
        catalogue_calls = 0
        checkout_started = asyncio.Event()
        checkout_cancelled = asyncio.Event()
        telegram_started = asyncio.Event()
        telegram_cancelled = asyncio.Event()
        recovered = False

        async def recover() -> None:
            nonlocal recovered
            recovered = True

        async def catalogue() -> None:
            nonlocal catalogue_calls
            assert recovered
            catalogue_calls += 1

        async def checkout() -> None:
            checkout_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                checkout_cancelled.set()
                raise

        async def telegram() -> None:
            telegram_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                telegram_cancelled.set()
                raise

        schedule = WorkerSchedule(0.01, max_backoff_seconds=1)
        supervisor = RuntimeSupervisor(
            workers=(
                ResilientPollingWorker(
                    name="catalogue",
                    callback=catalogue,
                    schedule=schedule,
                    state_store=store,
                    event_log=log,
                    jitter=lambda low, high: 0,
                ),
                ResilientPollingWorker(
                    name="booking",
                    callback=checkout,
                    schedule=schedule,
                    state_store=store,
                    event_log=log,
                    jitter=lambda low, high: 0,
                ),
                ResilientPollingWorker(
                    name="telegram",
                    callback=telegram,
                    schedule=schedule,
                    state_store=store,
                    event_log=log,
                    jitter=lambda low, high: 0,
                ),
            ),
            recoveries=(recover,),
            state_store=store,
            event_log=log,
            lease_seconds=3,
            shutdown_grace_seconds=0.02,
            owner_id="runtime-one",
        )
        running = asyncio.create_task(supervisor.run())
        await asyncio.wait_for(checkout_started.wait(), timeout=1)
        await asyncio.wait_for(telegram_started.wait(), timeout=1)
        await asyncio.sleep(0.045)
        assert catalogue_calls >= 3
        supervisor.request_shutdown()
        await asyncio.wait_for(running, timeout=1)
        assert checkout_cancelled.is_set()
        assert telegram_cancelled.is_set()
        assert store.owner is None
        assert any(run.name == "booking" and run.state == "interrupted" for run in store.runs)

    asyncio.run(scenario())


def test_safe_json_log_redacts_urls_tokens_headers_and_sensitive_fields() -> None:
    stream = StringIO()
    logger = logging.getLogger("test-safe-runtime-log")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(stream))
    SafeJsonLogger(logger).emit(
        "failure",
        worker="catalogue",
        detail=(
            "Authorization: Basic abcdef "
            "https://quicktickets.ru/payment/order/private-code?token=query-secret "
            "123456789:telegram_secret_token"
        ),
        payment_url="https://quicktickets.ru/payment/order/private-code",
        nested={"cookie": "session=private", "candidate_id": "candidate-1"},
    )
    raw = stream.getvalue()
    payload = json.loads(raw)
    assert payload["payment_url"] == "[redacted]"
    assert payload["nested"]["cookie"] == "[redacted]"
    assert payload["nested"]["candidate_id"] == "candidate-1"
    for secret in ("abcdef", "private-code", "query-secret", "telegram_secret_token"):
        assert secret not in raw


def test_status_shows_stale_catalogue_next_cycle_stop_reason_and_ttl() -> None:
    now = datetime(2026, 9, 21, 10, tzinfo=UTC)
    report = RuntimeStatus(
        last_complete_catalogue_at=now - timedelta(minutes=4),
        pending_outbox=2,
        oldest_outbox_at=now - timedelta(seconds=75),
        ambiguous_writes=1,
        candidates=(
            CandidateStatus(
                "candidate-1",
                "Спектакль",
                "renewal_waiting",
                2,
                now + timedelta(seconds=90),
                None,
                now + timedelta(seconds=600),
            ),
            CandidateStatus("candidate-2", "Другой", "stopped", 1, None, "user_stop", None),
        ),
        workers=(("catalogue", "backoff", None, "rate_limited"),),
    )
    text = render_status(report, now=now, stale_after_seconds=180)
    assert "устарела" in text
    assert "Очередь сообщений: 2" in text
    assert "следующая проверка через 1 мин 30 с" in text
    assert "ссылка действует ещё 10 мин 0 с" in text
    assert "причина: user_stop" in text
    assert "catalogue=backoff (rate_limited)" in text


def test_backoff_survives_prolonged_outage_and_ignores_wakeup() -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        wake = asyncio.Event()
        calls = 0

        async def failing() -> None:
            nonlocal calls
            calls += 1
            wake.set()
            raise RuntimeError("offline")

        worker = ResilientPollingWorker(
            name="booking",
            callback=failing,
            schedule=WorkerSchedule(1, initial_backoff_seconds=0.05, max_backoff_seconds=0.05),
            state_store=MemoryStateStore(),
            event_log=MemoryLog(),
            wake_event=wake,
        )
        for _ in range(1100):
            assert await worker.execute_once() == 0.05
        calls = 0
        running = asyncio.create_task(worker.run(stop))
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(running, timeout=1)
        assert calls == 1

    asyncio.run(scenario())


def test_supervisor_renews_lease_during_recovery_and_shutdown() -> None:
    async def scenario() -> None:
        renewed = asyncio.Event()
        checkout_started = asyncio.Event()
        finish_checkout = asyncio.Event()

        class Store(MemoryStateStore):
            async def heartbeat(self, **kwargs: object) -> bool:
                renewed.set()
                return True

        async def recover() -> None:
            await asyncio.wait_for(renewed.wait(), timeout=1)

        async def checkout() -> None:
            checkout_started.set()
            await finish_checkout.wait()

        store = Store()
        supervisor = RuntimeSupervisor(
            workers=(
                ResilientPollingWorker(
                    name="booking",
                    callback=checkout,
                    schedule=WorkerSchedule(1),
                    state_store=store,
                    event_log=MemoryLog(),
                ),
            ),
            recoveries=(recover,),
            state_store=store,
            event_log=MemoryLog(),
            lease_seconds=0.06,
            shutdown_grace_seconds=1,
        )
        running = asyncio.create_task(supervisor.run())
        try:
            await asyncio.wait_for(checkout_started.wait(), timeout=1)
            renewed.clear()
            supervisor.request_shutdown()
            await asyncio.wait_for(renewed.wait(), timeout=1)
            assert store.owner is not None
            finish_checkout.set()
            await asyncio.wait_for(running, timeout=1)
            assert store.owner is None
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


def test_lease_loss_cancels_recovery_before_workers_start() -> None:
    async def scenario() -> None:
        cancelled = asyncio.Event()
        calls = 0

        class Store(MemoryStateStore):
            async def heartbeat(self, **kwargs: object) -> bool:
                return False

        async def recover() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def callback() -> None:
            nonlocal calls
            calls += 1

        store = Store()
        supervisor = RuntimeSupervisor(
            workers=(
                ResilientPollingWorker(
                    name="booking",
                    callback=callback,
                    schedule=WorkerSchedule(1),
                    state_store=store,
                    event_log=MemoryLog(),
                ),
            ),
            recoveries=(recover,),
            state_store=store,
            event_log=MemoryLog(),
            lease_seconds=0.03,
        )
        await asyncio.wait_for(supervisor.run(), timeout=1)
        assert cancelled.is_set()
        assert calls == 0
        assert store.owner is None

    asyncio.run(scenario())
