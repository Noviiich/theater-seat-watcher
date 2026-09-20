"""QuickTickets reconciliation boundary without payment-status polling."""

from __future__ import annotations

from theater_tickets.application.checkout import CheckoutRequest
from theater_tickets.application.reconciliation import (
    CheckoutReconciliationTransport,
    ReconciliationObservation,
    ReconciliationState,
)


class UnsupportedQuickTicketsReconciliationTransport(CheckoutReconciliationTransport):
    """Safe default until a provider order-lookup contract is confirmed."""

    async def reconcile(self, request: CheckoutRequest) -> ReconciliationObservation:
        return ReconciliationObservation(ReconciliationState.UNSUPPORTED)
