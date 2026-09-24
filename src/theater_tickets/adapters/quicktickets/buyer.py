"""Validate buyer data without exposing it in representations or logs."""

from __future__ import annotations

import re

from theater_tickets.application.checkout import CheckoutBuyer
from theater_tickets.domain.errors import DomainValidationError

_PHONE_PATTERN = re.compile(r"^\+?[0-9]{10,15}$")


def validate_buyer_profile(
    *,
    lastname: str,
    firstname: str,
    middlename: str,
    email: str,
    phone: str,
    personal_data_consent: bool,
) -> CheckoutBuyer:
    """Normalize a database-backed buyer profile for a future checkout."""
    values = {
        "lastname": _required_text(lastname, "lastname"),
        "firstname": _required_text(firstname, "firstname"),
        "middlename": _required_text(middlename, "middlename"),
        "email": _required_text(email, "email"),
        "phone": _required_text(phone, "phone"),
    }
    if "@" not in values["email"] or values["email"].startswith("@"):
        raise DomainValidationError("buyer profile email is invalid")
    normalized_phone = re.sub(r"[\s()\-]", "", values["phone"])
    if not _PHONE_PATTERN.fullmatch(normalized_phone):
        raise DomainValidationError("buyer profile phone is invalid")
    values["phone"] = normalized_phone
    if not personal_data_consent:
        raise DomainValidationError("buyer profile requires personal data consent")
    return CheckoutBuyer(personal_data_consent=True, **values)


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise DomainValidationError(f"buyer profile {field} must be non-empty text")
    return normalized
