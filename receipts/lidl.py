"""Lidl Plus ingestion.

Uses the community ``lidl-plus`` library (unofficial, reverse-engineered) as the
API client. Auth is bootstrapped once, outside the container, with its CLI:

    pipx run "lidl-plus[auth]" auth        # or: pip install "lidl-plus[auth]"

That walks through email/password + the OTP and prints a **refresh token**.
Paste it into ``LIDL_REFRESH_TOKEN``; the service then runs unattended — the
library exchanges the refresh token for short-lived access tokens itself.

The Lidl API returns itemized receipts directly (no OCR). Prices come as German
decimal strings ("2,19") with optional per-line ``discounts``. The mapping
below is kept as a pure, unit-tested function since a bug would feed wrong
amounts into a real Spliit expense.

NOTE: the exact ticket schema is undocumented and could change. Verify the
mapping against one real ticket after first auth (the poller logs a warning if
the summed item lines don't reconcile with the ticket's own total).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config
from .models import ExtractedItem

logger = logging.getLogger(__name__)


@dataclass
class LidlReceipt:
    external_id: str
    purchase_date: Optional[str]
    total_amount: float
    store: str
    items: list[ExtractedItem] = field(default_factory=list)


def is_configured() -> bool:
    return bool(config.LIDL_ENABLED and config.LIDL_REFRESH_TOKEN)


# --- Amount parsing ----------------------------------------------------------

def parse_amount(value: Any) -> float:
    """Parse a Lidl amount ("2,19", "-1,20", 2.19) to a float. None -> 0.0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


# --- Ticket mapping (pure) ---------------------------------------------------

def parse_lidl_ticket(ticket: dict[str, Any]) -> LidlReceipt:
    """Map a lidl-plus ``ticket()`` detail dict onto the internal model.

    * Each line's total = ``originalAmount`` (extended line amount) minus its
      discounts. Deposits (Pfand) are left out of item totals; the receipt-level
      ``totalAmount`` from Lidl remains the authoritative figure shown.
    """
    ticket_id = str(ticket.get("id") or ticket.get("sequenceNumber") or "")
    purchase_date = ticket.get("date") or ticket.get("isoDate")
    total_amount = parse_amount(ticket.get("totalAmount"))

    store = "Lidl"
    store_info = ticket.get("store")
    if isinstance(store_info, dict) and store_info.get("name"):
        store = f"Lidl {store_info['name']}"

    items: list[ExtractedItem] = []
    for line in ticket.get("itemsLine") or ticket.get("items") or []:
        name = (line.get("name") or "").strip()
        if not name:
            continue

        quantity = parse_amount(line.get("quantity")) or 1.0
        gross = parse_amount(line.get("originalAmount"))
        if not gross:
            # Fall back to unit price * quantity when no line amount is given.
            gross = round(parse_amount(line.get("currentUnitPrice")) * quantity, 2)

        discount_total = sum(
            abs(parse_amount(d.get("amount")))
            for d in (line.get("discounts") or [])
            if isinstance(d, dict)
        )
        total_price = round(gross - discount_total, 2)
        unit_price = parse_amount(line.get("currentUnitPrice"))
        if not unit_price and quantity:
            unit_price = round(total_price / quantity, 2)

        if total_price <= 0:
            continue

        items.append(
            ExtractedItem(
                name=name,
                quantity=quantity,
                unit_price=unit_price,
                total_price=total_price,
                category=line.get("taxGroupName"),
                source_method="lidl",
            )
        )

    if total_amount <= 0 and items:
        total_amount = round(sum(i.total_price for i in items), 2)

    return LidlReceipt(
        external_id=f"lidl:{ticket_id}",
        purchase_date=purchase_date,
        total_amount=total_amount,
        store=store,
        items=items,
    )


def reconciles(receipt: LidlReceipt) -> bool:
    """True if the summed item lines match the ticket total (within 2 cents)."""
    return abs(sum(i.total_price for i in receipt.items) - receipt.total_amount) <= 0.02


# --- API client (lazy import so the lib is only needed at runtime) -----------

def _api():
    from lidlplus import LidlPlusApi  # noqa: PLC0415  (lazy: runtime-only dep)

    return LidlPlusApi(
        config.LIDL_LANGUAGE,
        config.LIDL_COUNTRY,
        refresh_token=config.LIDL_REFRESH_TOKEN,
    )


def list_ticket_ids(api: Optional[Any] = None) -> list[str]:
    """Return the ids of all tickets (newest first, as Lidl returns them)."""
    api = api or _api()
    ids = []
    for summary in api.tickets():
        tid = summary.get("id") or summary.get("sequenceNumber")
        if tid is not None:
            ids.append(str(tid))
    return ids


def fetch_ticket(ticket_id: str, api: Optional[Any] = None) -> LidlReceipt:
    api = api or _api()
    return parse_lidl_ticket(api.ticket(ticket_id))
