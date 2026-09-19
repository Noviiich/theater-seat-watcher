"""Load a private buyer profile without leaking its values into application state."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from theater_tickets.application.checkout import CheckoutBuyer
from theater_tickets.domain.errors import DomainValidationError

_REQUIRED_FIELDS = frozenset(
    {"lastname", "firstname", "middlename", "email", "phone", "personal_data_consent"}
)
_PHONE_PATTERN = re.compile(r"^\+?[0-9]{10,15}$")


@dataclass(frozen=True, slots=True, repr=False)
class BuyerProfile:
    """Validated values intended only for an explicitly authorized checkout submit."""

    lastname: str
    firstname: str
    middlename: str
    email: str
    phone: str
    personal_data_consent: bool


def load_buyer_profile(path: Path) -> BuyerProfile:
    """Read one owner-only JSON profile; errors deliberately omit its contents."""
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DomainValidationError(f"cannot read buyer profile: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise DomainValidationError("buyer profile must be a regular file")
    if metadata.st_mode & (stat.S_IRGRP | stat.S_IROTH):
        raise DomainValidationError("buyer profile must not be readable by group or others")
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DomainValidationError(f"cannot parse buyer profile: {path.name}") from exc
    if not isinstance(raw, dict) or set(raw) != _REQUIRED_FIELDS:
        raise DomainValidationError("buyer profile must contain exactly the required fields")

    values = {
        name: _required_text(raw, name) for name in _REQUIRED_FIELDS - {"personal_data_consent"}
    }
    if "@" not in values["email"] or values["email"].startswith("@"):
        raise DomainValidationError("buyer profile email is invalid")
    normalized_phone = re.sub(r"[\s()\-]", "", values["phone"])
    if not _PHONE_PATTERN.fullmatch(normalized_phone):
        raise DomainValidationError("buyer profile phone is invalid")
    values["phone"] = normalized_phone
    if raw["personal_data_consent"] is not True:
        raise DomainValidationError("buyer profile requires personal data consent")
    return BuyerProfile(personal_data_consent=True, **values)


def to_checkout_buyer(profile: BuyerProfile) -> CheckoutBuyer:
    """Copy validated secrets into the provider-neutral checkout contract."""
    return CheckoutBuyer(
        lastname=profile.lastname,
        firstname=profile.firstname,
        middlename=profile.middlename,
        email=profile.email,
        phone=profile.phone,
        personal_data_consent=profile.personal_data_consent,
    )


def _required_text(raw: dict[str, Any], field: str) -> str:
    value = raw[field]
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise DomainValidationError(f"buyer profile {field} must be non-empty text")
    return normalized
