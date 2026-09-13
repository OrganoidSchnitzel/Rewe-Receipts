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


# --- API client --------------------------------------------------------------
#
# We use the lidl-plus library ONLY for OAuth token management (its
# ``_default_headers()`` renews the access token from the refresh token and
# returns the Bearer + app headers). The actual ticket calls are made ourselves
# over HTTP/2, because Lidl's API gateway (Istio/Envoy) hangs plain HTTP/1.1
# requests (which is all `requests`/urllib3 speaks) — it only answers over the
# HTTP/2 that a real client negotiates. `requests` timing out at exactly the
# timeout on the authenticated ticket call, while curl (ALPN h2) responds
# instantly, is the tell.

_TICKET_API = "https://tickets.lidlplus.com/api/v2"


def _auth_headers() -> dict[str, str]:
    """Build the authenticated request headers via lidl-plus's token logic.

    We reuse the library only for the Bearer token, then overwrite its
    hard-coded ``App-Version: 999.99.9`` with a realistic one — Lidl's WAF
    resets requests carrying that impossible version (verified: fake token +
    999.99.9 → RST_STREAM; realistic version → normal 401).
    """
    from lidlplus import LidlPlusApi  # noqa: PLC0415  (lazy: runtime-only dep)

    api = LidlPlusApi(
        config.LIDL_LANGUAGE,
        config.LIDL_COUNTRY,
        refresh_token=config.LIDL_REFRESH_TOKEN,
    )
    headers = dict(api._default_headers())  # carries the renewed Bearer token
    headers["App-Version"] = config.LIDL_APP_VERSION
    headers["Operating-System"] = config.LIDL_OPERATING_SYSTEM
    headers["App"] = "com.lidl.eci.lidl.plus"
    headers["Accept-Language"] = config.LIDL_LANGUAGE
    if config.LIDL_USER_AGENT:
        headers["User-Agent"] = config.LIDL_USER_AGENT
    return headers


def open_client():
    """Open an HTTP/2 client carrying the authenticated headers."""
    import httpx  # noqa: PLC0415

    return httpx.Client(
        http2=True,
        timeout=config.HTTP_TIMEOUT,
        headers=_auth_headers(),
        follow_redirects=True,
    )


def _tickets_page(client, page: int) -> dict[str, Any]:
    url = f"{_TICKET_API}/{config.LIDL_COUNTRY}/tickets"
    response = client.get(url, params={"pageNumber": page, "onlyFavorite": "false"})
    response.raise_for_status()
    return response.json()


def list_tickets(client) -> list[dict[str, Any]]:
    """Return all ticket summaries, following pagination."""
    first = _tickets_page(client, 1)
    tickets = list(first.get("tickets") or [])
    try:
        size = int(first.get("size") or 0)
        total = int(first.get("totalCount") or 0)
    except (TypeError, ValueError):
        size = total = 0
    if size > 0 and total > size:
        last_page = -(-total // size)  # ceil
        for page in range(2, last_page + 1):
            tickets += list(_tickets_page(client, page).get("tickets") or [])
    return tickets


def list_ticket_ids(client) -> list[str]:
    """Return the ids of all tickets (newest first, as Lidl returns them)."""
    ids = []
    for summary in list_tickets(client):
        tid = summary.get("id") or summary.get("sequenceNumber")
        if tid is not None:
            ids.append(str(tid))
    return ids


def fetch_ticket_raw(ticket_id: str, client) -> dict[str, Any]:
    url = f"{_TICKET_API}/{config.LIDL_COUNTRY}/tickets/{ticket_id}"
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def fetch_ticket(ticket_id: str, client) -> LidlReceipt:
    return parse_lidl_ticket(fetch_ticket_raw(ticket_id, client))


# Candidate keys the item list might live under, across Lidl API variants.
_ITEM_LIST_KEYS = ("itemsLine", "items", "articlesList", "articles", "lineItems")


def diagnose() -> None:
    """Connectivity + shape check (run: python -m receipts.lidl)."""
    import time

    if not config.LIDL_REFRESH_TOKEN:
        print("LIDL_REFRESH_TOKEN is not set.")
        return
    print(f"Country={config.LIDL_COUNTRY} Language={config.LIDL_LANGUAGE} "
          f"App-Version={config.LIDL_APP_VERSION}")
    try:
        _auth_headers()
        print("✓ Access token obtained (Authorization header built).")
    except Exception as exc:
        print(f"✗ Token step failed: {exc!r}")
        return

    try:
        started = time.monotonic()
        with open_client() as client:
            summaries = list_tickets(client)
            elapsed = time.monotonic() - started
            print(f"✓ HTTP/2 ticket list OK in {elapsed:.1f}s — "
                  f"{len(summaries)} ticket(s).")
            if not summaries:
                print("  (No tickets on this account yet — nothing to import.)")
                return

            tid = str(summaries[0].get("id") or summaries[0].get("sequenceNumber"))
            detail = fetch_ticket_raw(tid, client)
    except Exception as exc:
        print(f"✗ Ticket call failed: {type(exc).__name__}: {exc}")
        return

    print(f"\n-- Newest ticket detail (id={tid}) --")
    print("top-level keys:", sorted(detail.keys()))
    items_key = next((k for k in _ITEM_LIST_KEYS if isinstance(detail.get(k), list)), None)
    print("items key found:", items_key)
    if items_key:
        items = detail[items_key]
        print(f"item count: {len(items)}")
        if items:
            print("first item keys:", sorted(items[0].keys()))
            print("first item:", items[0])

    parsed = parse_lidl_ticket(detail)
    print(f"\nparsed by current mapping -> {len(parsed.items)} item(s), "
          f"total €{parsed.total_amount:.2f}, reconciles={reconciles(parsed)}")
    if not parsed.items:
        print("⚠ 0 items parsed — the item mapping needs the real keys above.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    diagnose()
