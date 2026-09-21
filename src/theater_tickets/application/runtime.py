"""Provider-neutral lifecycle contracts for resilient background workers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class RuntimeAlreadyRunning(RuntimeError):
    """Raised when another process owns the non-expired runtime lease."""


@dataclass(frozen=True, slots=True)
class WorkerSchedule:
    interval_seconds: float
    jitter_seconds: float = 0.0
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("worker interval must be positive")
        if self.jitter_seconds < 0:
            raise ValueError("worker jitter must not be negative")
        if self.initial_backoff_seconds <= 0:
            raise ValueError("initial backoff must be positive")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff must not be below initial backoff")


@dataclass(frozen=True, slots=True)
class WorkerRun:
    name: str
    state: str
    started_at: datetime
    updated_at: datetime
    consecutive_failures: int
    next_run_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    last_error_code: str | None = None
    duration_ms: int | None = None


class RuntimeStateStore(Protocol):
    async def acquire_lock(self, *, owner_id: str, now: datetime, lease_seconds: int) -> bool: ...

    async def heartbeat(self, *, owner_id: str, now: datetime, lease_seconds: int) -> bool: ...

    async def release_lock(self, *, owner_id: str) -> None: ...

    async def record_worker(self, run: WorkerRun) -> None: ...


class RuntimeEventLog(Protocol):
    def emit(self, event: str, **fields: object) -> None: ...


WorkerCallback = Callable[[], Awaitable[object]]
RecoveryCallback = Callable[[], Awaitable[object]]
