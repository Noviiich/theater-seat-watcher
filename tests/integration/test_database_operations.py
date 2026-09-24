from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from theater_tickets.adapters.persistence.database import create_session_factory
from theater_tickets.adapters.persistence.models import (
    BudgetAllocationModel,
    BuyerModel,
    CandidateModel,
    CatalogueSnapshotModel,
    CheckoutIntentModel,
    DiscoveryBatchModel,
    OutboxMessageModel,
    RenewalCycleModel,
    SessionModel,
    SubscriptionModel,
)
from theater_tickets.operations.database import (
    backup_database,
    health_database,
    migrate_database,
    restore_database,
    smoke_database,
)


def test_online_backup_restore_preserves_dedup_and_budget_keys(tmp_path: Path) -> None:
    async def seed_and_restart(database_url: str) -> None:
        engine, factory = create_session_factory(database_url)
        now = datetime(2026, 9, 21, 10, tzinfo=UTC)
        async with factory() as database, database.begin():
            database.add_all(
                [
                    BuyerModel(
                        id="buyer",
                        telegram_user_id="10",
                        telegram_chat_id="10",
                        created_at=now,
                    ),
                    CatalogueSnapshotModel(
                        id="snapshot",
                        theatre_alias="theatre",
                        fetched_at=now,
                        complete=True,
                        fingerprint="fingerprint",
                    ),
                    SessionModel(
                        id="session",
                        provider="quicktickets",
                        theatre_alias="theatre",
                        provider_session_id="3159",
                        event_id="386",
                        hall_id="16",
                        title="Title",
                        starts_at=now + timedelta(days=1),
                        first_seen_at=now,
                        last_seen_at=now,
                    ),
                ]
            )
            await database.flush()
            database.add(
                SubscriptionModel(
                    id="subscription",
                    buyer_id="buyer",
                    theatre_alias="theatre",
                    ticket_count=2,
                    seat_profile_id="profile",
                    enabled=True,
                    version=1,
                    config={"booking_mode": "dry_run"},
                )
            )
            database.add(
                DiscoveryBatchModel(
                    id="batch",
                    snapshot_id="snapshot",
                    discovered_session_ids=["session"],
                    created_at=now,
                )
            )
            await database.flush()
            database.add(
                CandidateModel(
                    id="candidate",
                    buyer_id="buyer",
                    subscription_id="subscription",
                    session_id="session",
                    discovery_batch_id="batch",
                    subscription_version=1,
                    booking_mode="dry_run",
                    tracking_state="dry_run_completed",
                    current_cycle_no=1,
                    watch_until=now + timedelta(days=1),
                )
            )
            await database.flush()
            database.add(
                RenewalCycleModel(
                    id="cycle",
                    candidate_id="candidate",
                    cycle_no=1,
                    state="submitting",
                    due_at=now,
                    started_at=now,
                    created_at=now,
                )
            )
            await database.flush()
            database.add(
                CheckoutIntentModel(
                    id="intent",
                    renewal_cycle_id="cycle",
                    attempt_no=1,
                    state="pending",
                    selected_seat_ids=["1", "2"],
                    reserved_total_minor=5_000,
                    expected_total_minor=4_000,
                    currency="RUB",
                    expected_hold_ttl_seconds=1200,
                    remote_stage="planned",
                    created_at=now,
                )
            )
            await database.flush()
            database.add_all(
                [
                    BudgetAllocationModel(
                        id="allocation",
                        buyer_id="buyer",
                        discovery_batch_id="batch",
                        candidate_id="candidate",
                        checkout_intent_id="intent",
                        reserved_total_minor=5_000,
                        active=True,
                        created_at=now,
                    ),
                    OutboxMessageModel(
                        id="outbox",
                        dedup_key="batch:batch:buyer:summary",
                        kind="batch_summary",
                        discovery_batch_id="batch",
                        buyer_id="buyer",
                        destination_chat_id="10",
                        payload={"results": []},
                        state="pending",
                        attempts=0,
                        next_attempt_at=now,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
        await engine.dispose()

        restarted_engine, restarted_factory = create_session_factory(database_url)
        async with restarted_factory() as database:
            assert await database.scalar(select(BudgetAllocationModel.id)) == "allocation"
            assert await database.scalar(select(OutboxMessageModel.id)) == "outbox"
        await restarted_engine.dispose()

    database_path = tmp_path / "data" / "theater.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    migrate_database(database_url)
    asyncio.run(seed_and_restart(database_url))
    assert health_database(database_url).revision == "0014_telegram_per_session_limits"
    assert smoke_database(database_url).revision == "0014_telegram_per_session_limits"

    backup_path = tmp_path / "backups" / "backup.sqlite3"
    assert backup_database(database_url, backup_path) == backup_path.resolve()
    assert backup_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        backup_database(database_url, backup_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE budget_allocations SET reserved_total_minor = 1")
        connection.execute("DELETE FROM outbox_messages")
        connection.commit()

    safety = restore_database(database_url, backup_path)
    assert safety is not None and safety.is_file()
    with sqlite3.connect(database_path) as connection:
        allocation = connection.execute(
            "SELECT reserved_total_minor, active FROM budget_allocations "
            "WHERE checkout_intent_id = 'intent'"
        ).fetchone()
        dedup = connection.execute(
            "SELECT dedup_key FROM outbox_messages WHERE id = 'outbox'"
        ).fetchone()
    assert allocation == (5_000, 1)
    assert dedup == ("batch:batch:buyer:summary",)
    assert smoke_database(database_url).revision == "0014_telegram_per_session_limits"
