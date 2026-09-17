from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import (
    Money,
    RenewalPolicy,
    Seat,
    SeatAvailability,
    SeatGroup,
    Session,
    SessionKey,
    Subscription,
)


def test_money_converts_exact_decimal_rubles_to_kopecks() -> None:
    assert Money.from_rubles(Decimal("700.10")) == Money(70_010)


def test_renewal_policy_defaults_to_twenty_minute_payment_window() -> None:
    policy = RenewalPolicy()

    assert policy.renewal_interval_seconds == 1200
    assert policy.expected_hold_ttl_seconds == 1200
    assert policy.availability_retry_seconds == 180


@pytest.mark.parametrize("value", [700.1, "1.001"])
def test_money_rejects_imprecise_values(value: float | str) -> None:
    with pytest.raises(DomainValidationError):
        Money.from_rubles(value)


def test_session_normalizes_provider_ids_and_utc_time() -> None:
    key = SessionKey(provider=" quicktickets ", theatre_alias=" theatre ", session_id=3159)
    session = Session(
        key=key,
        event_id=386,
        hall_id=16,
        title="Спектакль",
        starts_at=datetime(2026, 10, 24, 16, 0, tzinfo=UTC),
    )

    assert session.key.session_id == "3159"
    assert session.event_id == "386"
    assert session.starts_at.tzinfo is UTC


def test_seat_group_rejects_non_free_and_duplicate_seats() -> None:
    free = Seat("1", "hall", "block", "1", "1", Money(70000), SeatAvailability.FREE)
    held = Seat("2", "hall", "block", "1", "2", Money(70000), SeatAvailability.HELD)

    with pytest.raises(DomainValidationError, match="only free"):
        SeatGroup("row-1", (free, held), Decimal("0.8"))
    with pytest.raises(DomainValidationError, match="duplicate"):
        SeatGroup("row-1", (free, free), Decimal("0.8"))


def test_subscription_validates_and_matches_local_time_filters() -> None:
    subscription = Subscription(
        subscription_id="sub-1",
        buyer_id="buyer-1",
        theatre_alias="theatre",
        ticket_count=2,
        seat_profile_id="hall-1",
        max_sessions_per_batch=3,
        event_ids=frozenset({"386"}),
        date_from=date(2026, 10, 24),
        weekdays=frozenset({5}),
        time_from=time(15, 0),
        time_to=time(17, 0),
        title_filter="спект",
    )
    session = Session(
        key=SessionKey("quicktickets", "theatre", "3159"),
        event_id="386",
        hall_id="16",
        title="Спектакль",
        starts_at=datetime(2026, 10, 24, 13, 0, tzinfo=UTC),
    )

    assert subscription.matches(session, local_starts_at=datetime(2026, 10, 24, 16, 0, tzinfo=UTC))
    assert not subscription.matches(
        session, local_starts_at=datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
    )


def test_subscription_rejects_invalid_filter_range() -> None:
    with pytest.raises(DomainValidationError, match="date_from"):
        Subscription(
            subscription_id="sub-1",
            buyer_id="buyer-1",
            theatre_alias="theatre",
            ticket_count=1,
            seat_profile_id="hall-1",
            max_sessions_per_batch=1,
            date_from=date(2026, 10, 25),
            date_to=date(2026, 10, 24),
        )


def test_subscription_applies_ticket_count_and_known_price_limits() -> None:
    subscription = Subscription(
        subscription_id="sub-1",
        buyer_id="buyer-1",
        theatre_alias="theatre",
        ticket_count=2,
        seat_profile_id="hall-1",
        max_sessions_per_batch=1,
        max_ticket_price=Money(70_000),
        max_order_total=Money(140_000),
    )
    eligible = SeatGroup(
        "row-1",
        (
            Seat("1", "hall", "block", "1", "1", Money(70_000), SeatAvailability.FREE),
            Seat("2", "hall", "block", "1", "2", Money(70_000), SeatAvailability.FREE),
        ),
        Decimal("0.8"),
    )
    expensive = SeatGroup(
        "row-1",
        (
            Seat("3", "hall", "block", "1", "3", Money(70_001), SeatAvailability.FREE),
            Seat("4", "hall", "block", "1", "4", Money(70_000), SeatAvailability.FREE),
        ),
        Decimal("0.8"),
    )

    assert subscription.allows_seat_group(eligible)
    assert not subscription.allows_seat_group(expensive)
