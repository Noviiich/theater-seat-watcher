from __future__ import annotations

import pytest

from theater_tickets.adapters.quicktickets.buyer import validate_buyer_profile
from theater_tickets.domain.errors import DomainValidationError


def _profile(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "lastname": "Tester",
        "firstname": "Test",
        "middlename": "Example",
        "email": "test@example.invalid",
        "phone": "+79990000000",
        "personal_data_consent": True,
    }
    values.update(overrides)
    return values


def test_database_buyer_profile_is_validated_and_never_reveals_values() -> None:
    profile = validate_buyer_profile(**_profile())  # type: ignore[arg-type]

    assert profile.lastname == "Tester"
    assert profile.phone == "+79990000000"
    assert "Tester" not in repr(profile)
    assert "test@example.invalid" not in repr(profile)


def test_buyer_profile_normalizes_common_phone_separators() -> None:
    assert (
        validate_buyer_profile(**_profile(phone="+7 (999) 000-00-00")).phone  # type: ignore[arg-type]
        == "+79990000000"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"phone": "invalid"},
        {"personal_data_consent": False},
        {"firstname": ""},
    ],
)
def test_buyer_profile_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(DomainValidationError):
        validate_buyer_profile(**_profile(**overrides))  # type: ignore[arg-type]
