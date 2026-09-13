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
from html.parser import HTMLParser
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

def _store_name(store: Any) -> str:
    if isinstance(store, dict):
        for key in ("name", "locality", "city", "address"):
            if store.get(key):
                return f"Lidl {store[key]}"
    return "Lidl"


class _LidlArticleParser(HTMLParser):
    """Collect the article <span> data attributes from a Lidl HTML receipt."""

    def __init__(self) -> None:
        super().__init__()
        self.articles: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag != "span":
            return
        attr = {k: (v or "") for k, v in attrs}
        css = attr.get("class", "")
        # Only real purchase lines (not summary/return copies of the same span).
        if "article" in css and attr.get("data-art-description") \
                and attr.get("id", "").startswith("purchase_list_line"):
            self.articles.append(attr)


def parse_lidl_html(html: str) -> list[ExtractedItem]:
    """Parse item lines from a Lidl v3 ``htmlPrintedReceipt`` (DE format).

    Each article span carries ``data-art-description``, ``data-art-quantity``
    and ``data-unit-price`` (comma decimals); the line total is quantity ×
    unit price (weight lines have a fractional quantity, e.g. 0,638 kg).
    """
    parser = _LidlArticleParser()
    parser.feed(html)

    items: list[ExtractedItem] = []
    seen: set[tuple[str, str, str, str]] = set()
    for art in parser.articles:
        # The HTML repeats each purchase line (multiple render copies); collapse
        # identical spans. Lidl merges genuine repeat purchases into one line
        # with a higher quantity, so identical lines are always render dupes.
        key = (
            art.get("data-art-id", ""),
            art.get("data-art-description", ""),
            art.get("data-art-quantity", ""),
            art.get("data-unit-price", ""),
        )
        if key in seen:
            continue
        seen.add(key)

        name = art.get("data-art-description", "").strip()
        if not name:
            continue
        unit_price = parse_amount(art.get("data-unit-price"))
        qty_raw = art.get("data-art-quantity")
        if qty_raw:
            quantity = parse_amount(qty_raw) or 1.0
            total_price = round(quantity * unit_price, 2)
        else:
            quantity = 1.0
            total_price = unit_price
        if total_price <= 0:
            continue
        items.append(
            ExtractedItem(
                name=name,
                quantity=quantity,
                unit_price=unit_price,
                total_price=total_price,
                category=art.get("data-tax-type") or None,
                source_method="lidl",
            )
        )
    return items


def parse_lidl_ticket(ticket: dict[str, Any]) -> LidlReceipt:
    """Map a Lidl ticket detail dict onto the internal model.

    Handles both the German v3 ``htmlPrintedReceipt`` format and the legacy
    structured ``itemsLine`` format. ``totalAmount`` from the ticket is the
    authoritative receipt total; coupons/loyalty that reduced the paid total are
    represented as a single reducing line so the items reconcile to it.
    """
    ticket_id = str(ticket.get("id") or ticket.get("sequenceNumber") or "")
    purchase_date = ticket.get("date") or ticket.get("isoDate")
    total_amount = parse_amount(ticket.get("totalAmount"))
    store = _store_name(ticket.get("store"))

    html = ticket.get("htmlPrintedReceipt") or ticket.get("html")
    if isinstance(html, str) and html.strip():
        items = parse_lidl_html(html)
        # Coupons/loyalty reduce the paid total below the gross line sum; add one
        # reducing line so the receipt reconciles (itemized coupons: follow-up).
        if total_amount > 0 and items:
            delta = round(sum(i.total_price for i in items) - total_amount, 2)
            if delta > 0.02:
                items.append(
                    ExtractedItem(
                        name="Rabatt / Coupons",
                        quantity=1.0,
                        unit_price=-delta,
                        total_price=-delta,
                        source_method="lidl",
                    )
                )
    else:
        items = _parse_itemsline(ticket)

    if total_amount <= 0 and items:
        total_amount = round(sum(i.total_price for i in items), 2)

    return LidlReceipt(
        external_id=f"lidl:{ticket_id}",
        purchase_date=purchase_date,
        total_amount=total_amount,
        store=store,
        items=items,
    )


def _parse_itemsline(ticket: dict[str, Any]) -> list[ExtractedItem]:
    """Legacy structured item list (non-HTML tickets / other countries)."""
    items: list[ExtractedItem] = []
    for line in ticket.get("itemsLine") or ticket.get("items") or []:
        name = (line.get("name") or "").strip()
        if not name:
            continue

        quantity = parse_amount(line.get("quantity")) or 1.0
        gross = parse_amount(line.get("originalAmount"))
        if not gross:
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
    return items


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


# The ticket LIST is served by API v2; a single ticket's DETAIL by API v3.
_TICKET_DETAIL_API = "https://tickets.lidlplus.com/api/v3"


def fetch_ticket_raw(ticket_id: str, client) -> dict[str, Any]:
    url = f"{_TICKET_DETAIL_API}/{config.LIDL_COUNTRY}/tickets/{ticket_id}"
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

            summary = summaries[0]
            print("\n-- Newest ticket SUMMARY object --")
            print("keys:", sorted(summary.keys()))
            print("values:", summary)

            tid = str(summary.get("id") or summary.get("sequenceNumber"))
            detail = fetch_ticket_raw(tid, client)  # API v3
    except Exception as exc:
        print(f"✗ Ticket detail (v3) failed: {type(exc).__name__}: {exc}")
        return

    print(f"\n-- Newest ticket DETAIL (v3, id={tid}) --")
    print("top-level keys:", sorted(detail.keys()))

    items_key = next((k for k in _ITEM_LIST_KEYS if isinstance(detail.get(k), list)), None)
    print("structured items key:", items_key)
    if items_key and detail[items_key]:
        print("first item keys:", sorted(detail[items_key][0].keys()))
        print("first item:", detail[items_key][0])

    html = detail.get("htmlPrintedReceipt") or detail.get("html") or ""
    if isinstance(html, str) and html:
        import re
        attrs = sorted(set(re.findall(r'(data-[\w-]+)=', html)))
        classes = sorted(set(re.findall(r'class="([\w ]+)"', html)))[:20]
        print(f"\nhtmlPrintedReceipt present ({len(html)} chars)")
        print("data-* attributes used:", attrs)
        print("span classes used:", classes)
        # One article span verbatim (item name/price — from your own receipt).
        m = re.search(r'<span[^>]*class="[^"]*article[^"]*"[^>]*>', html)
        if m:
            print("sample article span:", m.group(0))

    for key in ("couponsUsed", "offersUsed"):
        val = detail.get(key)
        if val:
            print(f"\n{key}: {val}")

    parsed = parse_lidl_ticket(detail)
    print(f"\nparsed -> {len(parsed.items)} item(s), store={parsed.store!r}, "
          f"total €{parsed.total_amount:.2f}, reconciles={reconciles(parsed)}")
    for it in parsed.items[:20]:
        print(f"   {it.name:28.28} qty={it.quantity:g} unit={it.unit_price:.2f} "
              f"total={it.total_price:.2f}")
    if not parsed.items:
        print("⚠ 0 items parsed — mapping needs updating for the shape above.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    diagnose()
