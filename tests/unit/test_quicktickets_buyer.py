from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from theater_tickets.adapters.quicktickets.buyer import load_buyer_profile, to_checkout_buyer
from theater_tickets.domain.errors import DomainValidationError


def _write_profile(path: Path, **overrides: object) -> None:
    profile: dict[str, object] = {
        "lastname": "Tester",
        "firstname": "Test",
        "middlename": "Example",
        "email": "test@example.invalid",
        "phone": "+79990000000",
        "personal_data_consent": True,
    }
    profile.update(overrides)
    path.write_text(json.dumps(profile), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_buyer_profile_is_validated_and_never_reveals_values(tmp_path: Path) -> None:
    path = tmp_path / "buyer.json"
    _write_profile(path)

    profile = load_buyer_profile(path)

    assert profile.lastname == "Tester"
    assert "Tester" not in repr(profile)
    checkout_buyer = to_checkout_buyer(profile)
    assert checkout_buyer.email == "test@example.invalid"
    assert "test@example.invalid" not in repr(checkout_buyer)


def test_buyer_profile_normalizes_common_phone_separators(tmp_path: Path) -> None:
    path = tmp_path / "buyer.json"
    _write_profile(path, phone="+7 (999) 000-00-00")

    assert load_buyer_profile(path).phone == "+79990000000"


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"phone": "invalid"},
        {"personal_data_consent": False},
        {"firstname": ""},
    ],
)
def test_buyer_profile_rejects_invalid_values(tmp_path: Path, overrides: dict[str, object]) -> None:
    path = tmp_path / "buyer.json"
    _write_profile(path, **overrides)

    with pytest.raises(DomainValidationError):
        load_buyer_profile(path)


def test_buyer_profile_rejects_extra_fields_and_permissive_mode(tmp_path: Path) -> None:
    path = tmp_path / "buyer.json"
    _write_profile(path, extra="forbidden")
    with pytest.raises(DomainValidationError, match="exactly"):
        load_buyer_profile(path)

    _write_profile(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(DomainValidationError, match="group"):
        load_buyer_profile(path)
