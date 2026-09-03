"""Receipt item extraction.

Two sources feed the same ``ExtractedItem`` model:

* **Lidl** — the Plus API returns structured line items; no OCR needed.
* **Rewe** — OCR text from Paperless-ngx. This uses a hybrid strategy:

    1. *Deterministic known-items*: a learned line -> canonical name table is
       checked first (a normalized OCR line matched exactly).
    2. *Deterministic regex*: the general "NAME ... 1,23 A" line parser.
    3. *AI fallback*: lines the regex can't turn into an item are handed to a
       local LLM via Ollama (only if enabled).

Everything is kept behind small functions so the matching rules or the LLM
prompt can be improved independently later.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from . import config
from .models import ExtractedItem

# --- Line filtering ----------------------------------------------------------

# Reaching one of these lines means the item list is over: everything below is
# the total, payment, tax, and cashback/loyalty section. Parsing stops here.
END_OF_ITEMS_PREFIXES = (
    "summe",
    "gesamt",
    "zu zahlen",
    "zu zahlender betrag",
    "total",
)

IGNORED_LINE_PREFIXES = (
    "summe",
    "gesamt",
    "mwst",
    "ec-zahlung",
    "bar",
    "wechselgeld",
)

IGNORED_LINE_KEYWORDS = (
    "guthaben",
    "bonus",
    "visa",
    "mastercard",
    "karte",
    "zahlung",
    "eingesetztes",
    "tse-",
    "start",
    "stop",
    "aktion",
    "rabatt",
    "mit diesem",
    "hast du",
    # REWE Beste Wahl / PAYBACK cashback lines (belt-and-suspenders; these
    # normally sit below SUMME and are already cut off there).
    "auf rewe",
    "beste wahl",
    "% auf",
    "payback",
    "coupon",
    "ersparnis",
)

# Matches a price like 1,23 / -1,23 / 12.34 (with a mandatory 2-decimal part).
PRICE_REGEX = re.compile(r"-?\d{1,4}(?:[.,]\d{2})")
# Quantity prefix like "2 x" / "3x" at the start of a line.
QUANTITY_REGEX = re.compile(r"^(\d{1,3})\s*[xX*]\s+")
# A quantity/weight breakdown sub-line that belongs to the item above it, e.g.
# "3 Stk x 1,29" or "0,234 kg x 5,99 EUR/kg" — NOT a separate article.
QUANTITY_BREAKDOWN_REGEX = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(?:stk|st|stück|kg|g|l|ml)?\.?\s*[x×*]\s*(\d+(?:[.,]\d{2}))",
    re.IGNORECASE,
)


def parse_quantity_breakdown(line: str) -> Optional[tuple[float, float]]:
    """Parse a quantity/unit-price breakdown sub-line.

    Returns ``(quantity, unit_price)`` when the line is a breakdown descriptor
    (e.g. ``3 Stk x 1,29``), otherwise ``None``.
    """
    match = QUANTITY_BREAKDOWN_REGEX.match(line)
    if not match:
        return None
    quantity = float(match.group(1).replace(",", "."))
    unit_price = float(match.group(2).replace(",", "."))
    if quantity <= 0:
        return None
    return quantity, unit_price


def is_probable_item_name(name: str, lowered_line: str) -> bool:
    lowered_name = name.lower()

    if any(keyword in lowered_line for keyword in IGNORED_LINE_KEYWORDS):
        return False

    if re.search(r"\d{4}[-/.]\d{2}[-/.]\d{2}", name) or re.search(r"\d{1,2}:\d{2}", name):
        return False

    if re.match(r"^[a-z]=", lowered_name):
        return False

    letters = sum(char.isalpha() for char in name)
    if letters < 3:
        return False

    return True


def normalize_line(line: str) -> str:
    """Canonicalize an OCR line for the known-items table.

    Strips the trailing price and tax marker, uppercases, and collapses
    whitespace, so cosmetic OCR variance in the price column doesn't prevent a
    match on the item text.
    """
    text = line.strip()
    # Drop everything from the last price match onward (price + tax marker).
    matches = list(PRICE_REGEX.finditer(text))
    if matches:
        text = text[: matches[-1].start()]
    text = re.sub(r"\s+", " ", text).strip(" -\t")
    return text.upper()


# --- Lidl (structured) -------------------------------------------------------

def parse_lidl_receipt(receipt_data: dict[str, Any]) -> list[ExtractedItem]:
    """Map the Lidl Plus API response into internal items.

    Lidl returns prices in cents. Handles both ``totalPrice.value`` (cents) and
    a plain string amount, plus an optional ``quantity``/``currentUnitPrice``.
    """
    items: list[ExtractedItem] = []

    for line_item in receipt_data.get("lineItems") or receipt_data.get("items") or []:
        name = (line_item.get("name") or "").strip()
        if not name:
            continue

        total_price = _lidl_amount(line_item.get("totalPrice"))
        if total_price is None:
            total_price = _lidl_amount(line_item.get("currentUnitPrice"))
        if total_price is None:
            continue

        quantity = _to_float(line_item.get("quantity"), default=1.0) or 1.0
        unit_price = _lidl_amount(line_item.get("currentUnitPrice"))
        if unit_price is None and quantity:
            unit_price = round(total_price / quantity, 2)

        if total_price > 0 and is_probable_item_name(name, name.lower()):
            items.append(
                ExtractedItem(
                    name=name,
                    quantity=quantity,
                    unit_price=unit_price or total_price,
                    total_price=round(total_price, 2),
                    source_method="lidl",
                )
            )
    return items


def _lidl_amount(value: Any) -> Optional[float]:
    """Interpret a Lidl price field (cents int, {'value': cents}, or string)."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 100.0
    if isinstance(value, str):
        cleaned = value.replace(",", ".").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


# --- Rewe (OCR, hybrid) ------------------------------------------------------

def parse_rewe_line(line: str) -> Optional[ExtractedItem]:
    """Deterministic regex parse of a single Rewe OCR line.

    Returns an ``ExtractedItem`` or ``None`` when the line isn't a priced item.
    """
    stripped = line.strip()
    if not stripped:
        return None

    lowered = stripped.lower()
    if lowered.startswith(IGNORED_LINE_PREFIXES):
        return None

    # A quantity/weight breakdown ("3 Stk x 1,29") is not an item of its own.
    if parse_quantity_breakdown(stripped) is not None:
        return None

    price_matches = list(PRICE_REGEX.finditer(stripped))
    if not price_matches:
        return None

    match = price_matches[-1]
    name = stripped[: match.start()].strip("- ")
    price = float(match.group(0).replace(",", "."))

    quantity = 1.0
    qty_match = QUANTITY_REGEX.match(name)
    if qty_match:
        quantity = float(qty_match.group(1))
        name = name[qty_match.end():].strip()

    if not name or price <= 0 or not is_probable_item_name(name, lowered):
        return None

    unit_price = round(price / quantity, 2) if quantity else price
    return ExtractedItem(
        name=name,
        quantity=quantity,
        unit_price=unit_price,
        total_price=price,
        source_method="regex",
        raw_line=normalize_line(stripped),
    )


def extract_rewe_items(
    receipt_text: str,
    known_items: Optional[dict[str, dict]] = None,
    use_ollama: Optional[bool] = None,
) -> list[ExtractedItem]:
    """Hybrid extraction over Rewe OCR text.

    Order per line: stop at the total -> merge quantity breakdowns into the
    previous item -> known-items table -> regex -> collect residue. Residual
    lines that look like they *might* be items are sent to Ollama in one batch.
    """
    known_items = known_items or {}
    if use_ollama is None:
        use_ollama = config.OLLAMA_ENABLED

    items: list[ExtractedItem] = []
    unresolved: list[str] = []

    for raw_line in receipt_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lowered = line.lower()

        # 0) End of the item list: SUMME / total. Everything below (payment,
        #    tax, cashback, bonus) is not an article — stop parsing entirely.
        if lowered.startswith(END_OF_ITEMS_PREFIXES):
            break

        # 0b) Quantity/weight breakdown ("3 Stk x 1,29"): enrich the previous
        #     item with quantity + unit price instead of adding a phantom item.
        breakdown = parse_quantity_breakdown(line)
        if breakdown is not None:
            if items:
                quantity, unit_price = breakdown
                items[-1].quantity = quantity
                items[-1].unit_price = unit_price
            continue

        normalized = normalize_line(line)

        # 1) Known-items deterministic match.
        if normalized and normalized in known_items:
            regex_item = parse_rewe_line(line)
            canonical = known_items[normalized]["canonical_name"]
            if regex_item:
                regex_item.name = canonical
                regex_item.source_method = "known"
                items.append(regex_item)
            # No price on the line but a known pattern: still record the name.
            continue

        # 2) Deterministic regex.
        regex_item = parse_rewe_line(line)
        if regex_item:
            items.append(regex_item)
            continue

        # 3) Residue -> AI fallback candidate (skip obvious noise).
        if lowered.startswith(IGNORED_LINE_PREFIXES):
            continue
        if any(keyword in lowered for keyword in IGNORED_LINE_KEYWORDS):
            continue
        if sum(ch.isalpha() for ch in line) >= 3:
            unresolved.append(line)

    if use_ollama and unresolved:
        items.extend(ollama_extract("\n".join(unresolved)))

    return items


# --- Ollama fallback ---------------------------------------------------------

OLLAMA_PROMPT = (
    "You extract grocery line items from noisy German supermarket receipt OCR "
    "text. Return ONLY a JSON array. Each element must be an object with keys "
    '"name" (string), "quantity" (number), "unit_price" (number, euros), and '
    '"total_price" (number, euros). Ignore totals, tax lines, payment lines, '
    "loyalty/bonus lines, dates and store metadata. If no items are present, "
    "return []. Text:\n\n"
)


def ollama_extract(
    text_block: str,
    url: Optional[str] = None,
    model: Optional[str] = None,
) -> list[ExtractedItem]:
    """Ask a local Ollama model to extract items from residual OCR lines.

    Network failures and malformed responses degrade gracefully to an empty
    list, so a flaky/offline Ollama never breaks ingestion.
    """
    import requests  # local import: keeps the module import-light for tests

    url = (url or config.OLLAMA_URL).rstrip("/")
    model = model or config.OLLAMA_MODEL
    if not text_block.strip():
        return []

    try:
        response = requests.post(
            f"{url}/api/generate",
            json={
                "model": model,
                "prompt": OLLAMA_PROMPT + text_block,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=config.HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []

    return parse_ollama_response(payload.get("response", ""))


def parse_ollama_response(raw: str) -> list[ExtractedItem]:
    """Parse the LLM's JSON output into items (tolerant of wrapping objects)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Some models wrap the array in prose; grab the first [...] block.
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return []

    if isinstance(data, dict):
        # e.g. {"items": [...]}
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
        else:
            return []
    if not isinstance(data, list):
        return []

    items: list[ExtractedItem] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        quantity = _to_float(entry.get("quantity"), default=1.0) or 1.0
        total_price = _to_float(entry.get("total_price"), default=0.0)
        unit_price = _to_float(entry.get("unit_price"), default=0.0)
        if total_price <= 0 and unit_price > 0:
            total_price = round(unit_price * quantity, 2)
        if total_price <= 0:
            continue
        items.append(
            ExtractedItem(
                name=name,
                quantity=quantity,
                unit_price=unit_price or round(total_price / quantity, 2),
                total_price=round(total_price, 2),
                source_method="ollama",
                raw_line=normalize_line(name),
            )
        )
    return items
