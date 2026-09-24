"""One-shot delivery worker for persistent notification messages."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from theater_tickets.application.outbox import (
    NotificationRateLimited,
    NotificationTransport,
    OutboxRepository,
)
from theater_tickets.application.status import remaining_ttl_line


class OutboxWorker:
    def __init__(
        self,
        *,
        repository: OutboxRepository,
        transport: NotificationTransport,
        batch_size: int = 20,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._repository = repository
        self._transport = transport
        self._batch_size = batch_size

    async def recover_startup(self, *, now: datetime) -> int:
        self._validate_time(now)
        return await self._repository.recover_claims(now=now)

    async def run_once(self, *, now: datetime) -> int:
        self._validate_time(now)
        items = await self._repository.claim_due(now=now, limit=self._batch_size)
        for item in items:
            if item.destination_chat_id != item.destination_user_id:
                await self._repository.reject(
                    outbox_id=item.outbox_id,
                    error_code="invalid_recipient",
                    now=now,
                )
                continue
            if item.previous_message_id is not None:
                try:
                    await self._transport.expire(
                        chat_id=item.destination_chat_id,
                        message_id=item.previous_message_id,
                    )
                except Exception:
                    pass
            try:
                delivered = item
                if item.expires_at is not None:
                    delivered = replace(
                        item,
                        text=(
                            f"{item.text}\n"
                            f"{remaining_ttl_line(expires_at=item.expires_at, now=now)}"
                        ),
                    )
                message_id = await self._transport.send(delivered)
            except NotificationRateLimited as exc:
                await self._repository.schedule_retry(
                    outbox_id=item.outbox_id,
                    now=now,
                    next_attempt_at=now + timedelta(seconds=exc.retry_after_seconds),
                    error_code="rate_limited",
                )
            except Exception:
                delay = min(30 * (2 ** max(item.attempts - 1, 0)), 1800)
                await self._repository.schedule_retry(
                    outbox_id=item.outbox_id,
                    now=now,
                    next_attempt_at=now + timedelta(seconds=delay),
                    error_code="transport_error",
                )
            else:
                await self._repository.mark_sent(
                    outbox_id=item.outbox_id,
                    message_id=message_id,
                    sent_at=now,
                )
        return len(items)

    @staticmethod
    def _validate_time(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("outbox worker timestamp must be timezone-aware")
