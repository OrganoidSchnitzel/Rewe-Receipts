"""Ingestion orchestration.

Turns a Paperless-ngx Rewe document into a stored receipt with extracted items.
Duplicate detection is enforced up front (and again by the DB UNIQUE
constraint): a document already imported is skipped entirely — never
re-extracted, never re-shown as new.

Both trigger paths (webhook push, polling fallback) funnel through
``ingest_rewe_document``.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Optional

from . import config, db, extraction, lidl, notifier, paperless, spliit

logger = logging.getLogger(__name__)

# Serializes receipt state changes (settle / dismiss / reopen / delete). The web
# server is threaded and the Telegram bot runs in its own thread, so without
# this a double-clicked button — or an Approve tap while the web UI is settling
# — could both see "pending" and create two Spliit expenses.
_state_lock = threading.RLock()


def rewe_external_id(document_id: int) -> str:
    return f"rewe:{document_id}"


def ingest_rewe_document(document_id: int) -> Optional[str]:
    """Import a single Rewe document. Returns the new receipt id, or ``None``
    if it was a duplicate / had no extractable items."""
    external_id = rewe_external_id(document_id)

    # 1) Duplicate check before doing any work.
    if db.receipt_exists(external_id):
        logger.info("Skipping already-imported Rewe document %s", document_id)
        return None

    # 2) Fetch metadata, OCR text and the original PDF.
    doc = paperless.get_document(document_id)
    ocr_text = doc.get("content", "") or ""
    purchase_date = paperless.document_purchase_date(doc)

    file_path: Optional[str] = None
    try:
        config.RECEIPT_FILES_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.RECEIPT_FILES_DIR / f"rewe_{document_id}.pdf"
        paperless.download_document(document_id, str(dest))
        file_path = str(dest)
    except Exception as exc:  # download is best-effort; text still works
        logger.warning("Could not download PDF for document %s: %s", document_id, exc)

    # 3) Extract items (known-items -> regex -> Ollama fallback).
    known = db.get_known_items()
    items = extraction.extract_rewe_items(ocr_text, known_items=known)
    if not items:
        logger.info("No items extracted from Rewe document %s", document_id)

    # 4) Persist (create_receipt returns None on a UNIQUE race → treated as dup).
    receipt_id = db.create_receipt(
        source="rewe",
        external_id=external_id,
        items=items,
        purchase_date=purchase_date,
        store="REWE",
        file_path=file_path,
        # The printed SUMME, so the UI can flag items the parser missed; falls
        # back to the item sum when no total line was recognized.
        total_amount=extraction.extract_rewe_total(ocr_text),
    )
    if receipt_id is None:
        logger.info("Concurrent import of Rewe document %s; skipped", document_id)
        return None

    logger.info(
        "Imported Rewe document %s as receipt %s (%d items)",
        document_id, receipt_id, len(items),
    )

    # Non-destructive: tag the Paperless document as processed so handled and
    # unhandled receipts are distinguishable there (never delete the archive).
    try:
        paperless.mark_document_processed(doc)
    except Exception as exc:
        logger.warning("Could not tag Paperless document %s as processed: %s",
                       document_id, exc)

    notifier.notify_new_receipt(db.get_receipt(receipt_id))
    return receipt_id


def poll_rewe_documents() -> list[str]:
    """Poll Paperless for Rewe documents and import any not yet seen.

    Checks every fetched document against existing external_ids (not just a
    last-seen date), so receipts arriving out of order are never missed.
    """
    imported: list[str] = []
    try:
        documents = paperless.list_rewe_documents()
    except Exception as exc:
        logger.warning("Paperless poll failed: %s", exc)
        return imported

    for doc in documents:
        document_id = doc.get("id")
        if document_id is None:
            continue
        if db.receipt_exists(rewe_external_id(document_id)):
            continue
        try:
            receipt_id = ingest_rewe_document(document_id)
            if receipt_id:
                imported.append(receipt_id)
        except Exception as exc:
            logger.warning("Failed to ingest document %s: %s", document_id, exc)
    return imported


# --- Lidl --------------------------------------------------------------------

def ingest_lidl_receipt(receipt: "lidl.LidlReceipt") -> Optional[str]:
    """Store a parsed Lidl receipt, skipping duplicates by external_id."""
    if db.receipt_exists(receipt.external_id):
        return None
    if not receipt.items:
        logger.info("Lidl receipt %s has no items; skipping", receipt.external_id)
        return None

    if not lidl.reconciles(receipt):
        logger.warning(
            "Lidl receipt %s items sum (%.2f) != ticket total (%.2f); review it",
            receipt.external_id,
            sum(i.total_price for i in receipt.items),
            receipt.total_amount,
        )

    receipt_id = db.create_receipt(
        source="lidl",
        external_id=receipt.external_id,
        items=receipt.items,
        purchase_date=receipt.purchase_date,
        store=receipt.store,
        total_amount=receipt.total_amount,
    )
    if receipt_id is None:
        return None

    logger.info(
        "Imported Lidl receipt %s as %s (%d items)",
        receipt.external_id, receipt_id, len(receipt.items),
    )
    notifier.notify_new_receipt(db.get_receipt(receipt_id))
    return receipt_id


def poll_lidl_tickets() -> list[str]:
    """Poll the Lidl Plus API and import any tickets not yet seen.

    Every ticket id is checked against existing external_ids (not a last-seen
    date), so tickets arriving out of order are never missed.
    """
    imported: list[str] = []
    if not lidl.is_configured():
        return imported

    try:
        client = lidl.open_client()
    except Exception as exc:
        logger.warning("Lidl poll failed (auth): %s", exc)
        return imported

    with client:
        try:
            ticket_ids = lidl.list_ticket_ids(client)
        except Exception as exc:
            logger.warning("Lidl poll failed (ticket list): %s", exc)
            return imported

        for ticket_id in ticket_ids:
            if db.receipt_exists(f"lidl:{ticket_id}"):
                continue
            try:
                receipt = lidl.fetch_ticket(ticket_id, client)
                new_id = ingest_lidl_receipt(receipt)
                if new_id:
                    imported.append(new_id)
            except Exception as exc:
                logger.warning("Failed to ingest Lidl ticket %s: %s", ticket_id, exc)
    return imported


# --- Re-extraction -----------------------------------------------------------

def _items_to_rows(items) -> list[dict]:
    return [
        {
            "name": i.name,
            "quantity": i.quantity,
            "unit_price": i.unit_price,
            "total_price": i.total_price,
            "included": True,
            "source_method": i.source_method,
            "raw_line": i.raw_line,
        }
        for i in items
    ]


def reextract_receipt(receipt_id: str, lidl_client=None) -> bool:
    """Re-run extraction for a receipt from its source and replace its items.

    Used to apply improved parsing to receipts imported by an earlier build.
    Only pending receipts are touched — settled/dismissed ones are final until
    reopened — and manual entries have no live source to re-fetch.
    Returns True if the receipt's items were refreshed.
    """
    receipt = db.get_receipt(receipt_id)
    if not receipt or receipt.status != "pending":
        return False

    source_ref = receipt.external_id.split(":", 1)
    if len(source_ref) != 2:
        return False
    ref = source_ref[1]

    if receipt.source == "rewe":
        text = paperless.get_document_text(int(ref))
        items = extraction.extract_rewe_items(text, known_items=db.get_known_items())
        total = extraction.extract_rewe_total(text)
        if total is None:
            total = sum(i.total_price for i in items)
        db.replace_items(receipt_id, _items_to_rows(items), total_amount=total)
        return True

    if receipt.source == "lidl":
        if not lidl.is_configured():
            return False
        client = lidl_client or lidl.open_client()
        try:
            parsed = lidl.fetch_ticket(ref, client)
        finally:
            if lidl_client is None:
                client.close()
        db.replace_items(receipt_id, _items_to_rows(parsed.items),
                         total_amount=parsed.total_amount)
        return True

    return False  # manual receipts have no source to re-fetch


def reextract_all() -> tuple[int, int]:
    """Re-extract every pending receipt from source. Returns (updated, skipped).

    Opens a single Lidl client for all Lidl receipts so the token is renewed
    once rather than per receipt.
    """
    updated = skipped = 0
    receipts = db.list_receipts()
    lidl_client = None
    if lidl.is_configured() and any(
        r.source == "lidl" and r.status == "pending" for r in receipts
    ):
        try:
            lidl_client = lidl.open_client()
        except Exception as exc:
            logger.warning("Could not open Lidl client for re-extract: %s", exc)

    try:
        for receipt in receipts:
            try:
                if reextract_receipt(receipt.id, lidl_client=lidl_client):
                    updated += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning("Re-extract failed for %s: %s", receipt.id, exc)
                skipped += 1
    finally:
        if lidl_client is not None:
            lidl_client.close()
    return updated, skipped


# --- Settlement (shared by the web UI and the Telegram bot) ------------------

def _after_change(receipt_id: str, outcome: str = "") -> None:
    """Mirror a state change into the receipt's Telegram message (if any)."""
    notifier.update_receipt_message(db.get_receipt(receipt_id), outcome)


def settle_receipt(receipt_id: str) -> tuple[bool, str]:
    """Create a Spliit expense for a receipt's currently-included items.

    Returns (ok, message). Idempotent: a receipt already settled is not settled
    again, so a repeated trigger (e.g. a stale Telegram button press or a
    double-clicked button) never creates a duplicate expense.
    """
    with _state_lock:
        ok, message = _settle_receipt(receipt_id)
    if ok:
        _after_change(receipt_id, message)
    return ok, message


def _settle_receipt(receipt_id: str) -> tuple[bool, str]:
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        return False, "Receipt not found."
    if receipt.status != "pending":
        return False, f"Receipt is already {receipt.status}."

    included = [i for i in receipt.items if i.included]
    if not included:
        return False, "No items selected."
    total = round(sum(i.total_price for i in included), 2)
    if total <= 0:
        return False, "Selected total must be positive."

    date_part = (receipt.purchase_date or "")[:10]
    title = f"{receipt.store or receipt.source.upper()} {date_part}".strip()
    try:
        expense_id = spliit.create_expense(
            title=title or "Receipt",
            amount_eur=total,
            notes=f"{len(included)} items imported from {receipt.source} receipt",
            expense_date=receipt.purchase_date,
        )
    except Exception as exc:
        logger.exception("Spliit expense creation failed")
        return False, f"Spliit error: {exc}"

    db.mark_settled(receipt_id, expense_id)
    return True, f"Created Spliit expense for €{total:.2f}."


def compute_participant_cents(items, all_ids: list[str]) -> dict[str, int]:
    """Split each item's cents among its assignees (or everyone if unassigned).

    Exact and fair: every person's share is accumulated exactly (fractions of a
    cent included) across the whole receipt and only rounded once at the end.
    The leftover cents go to whoever lost the most to rounding (ties: group
    order), so totals sum exactly to the items' cents and an even split never
    drifts by more than one cent. (Rounding per item instead hands every odd
    cent to the same person — 17 cents on a 21-item receipt.)

    Assignees not in the current group are ignored; an item with no valid
    assignee falls back to an even split.
    """
    if not all_ids:
        return {}
    # Common denominator for any k-way split, so shares stay integers.
    scale = math.lcm(*range(1, len(all_ids) + 1))
    units = {pid: 0 for pid in all_ids}  # in 1/scale cents
    total = 0
    for item in items:
        cents = round(item.total_price * 100)
        total += cents
        assignees = list(dict.fromkeys(
            a for a in (item.assignees or []) if a in units
        )) or list(all_ids)
        share = cents * scale // len(assignees)  # exact: scale is divisible by k
        for pid in assignees:
            units[pid] += share

    totals = {pid: u // scale for pid, u in units.items()}
    leftover = total - sum(totals.values())  # 0 <= leftover < len(all_ids)
    by_rounding_loss = sorted(
        all_ids, key=lambda pid: (-(units[pid] % scale), all_ids.index(pid))
    )
    for pid in by_rounding_loss[:leftover]:
        totals[pid] += 1
    return totals


def settle_receipt_advanced(receipt_id: str) -> tuple[bool, str]:
    """Create a per-person (BY_AMOUNT) Spliit expense from item assignments.

    Each included item is split among its assigned participants (or everyone if
    unassigned); the per-person sums become the expense's amounts. Idempotent
    like :func:`settle_receipt`.
    """
    with _state_lock:
        ok, message = _settle_receipt_advanced(receipt_id)
    if ok:
        _after_change(receipt_id, message)
    return ok, message


def _settle_receipt_advanced(receipt_id: str) -> tuple[bool, str]:
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        return False, "Receipt not found."
    if receipt.status != "pending":
        return False, f"Receipt is already {receipt.status}."

    included = [i for i in receipt.items if i.included]
    if not included:
        return False, "No items selected."

    try:
        participants = spliit.get_participants()
    except Exception as exc:
        return False, f"Could not load Spliit participants: {exc}"
    if not participants:
        return False, "Spliit group has no participants."

    name_by_id = {p.id: p.name for p in participants}
    cents = compute_participant_cents(included, [p.id for p in participants])
    if sum(c for c in cents.values() if c > 0) <= 0:
        return False, "Selected total must be positive."

    date_part = (receipt.purchase_date or "")[:10]
    title = f"{receipt.store or receipt.source.upper()} {date_part}".strip()
    try:
        payer = spliit.resolve_payer(participants)
        expense_id = spliit.create_expense_by_amounts(
            title=title or "Receipt",
            participant_cents=cents,
            payer_id=payer.id,
            notes=f"{len(included)} items, per-person split",
            expense_date=receipt.purchase_date,
        )
    except Exception as exc:
        logger.exception("Spliit advanced expense creation failed")
        return False, f"Spliit error: {exc}"

    db.mark_settled(receipt_id, expense_id)
    breakdown = ", ".join(
        f"{name_by_id.get(pid, pid)} €{c / 100:.2f}"
        for pid, c in cents.items() if c > 0
    )
    return True, f"Created Spliit expense — {breakdown}."


def dismiss_receipt(receipt_id: str) -> tuple[bool, str]:
    """Mark a receipt handled with nothing shared (no Spliit expense created).

    Like deselecting every item: the receipt leaves 'pending' without creating
    an expense. Idempotent and only applies to pending receipts.
    """
    with _state_lock:
        receipt = db.get_receipt(receipt_id)
        if not receipt:
            return False, "Receipt not found."
        if receipt.status == "dismissed":
            return False, "Already dismissed."
        if receipt.status != "pending":
            return False, f"Receipt is already {receipt.status}."
        db.set_status(receipt_id, "dismissed")
    _after_change(receipt_id)
    return True, "Dismissed — nothing shared."


def reopen_receipt(receipt_id: str) -> tuple[bool, str]:
    """Return a dismissed receipt to 'pending' so it can be handled again.

    Its Telegram message gets its Approve / Review / Dismiss buttons back.
    """
    with _state_lock:
        receipt = db.get_receipt(receipt_id)
        if not receipt:
            return False, "Receipt not found."
        if receipt.status != "dismissed":
            return False, "Only dismissed receipts can be reopened."
        db.set_status(receipt_id, "pending")
    _after_change(receipt_id)
    return True, "Reopened."


def delete_receipt(receipt_id: str) -> Optional[str]:
    """Delete a receipt; its Telegram message is marked deleted (buttons gone).

    Returns the deleted receipt's external_id, or ``None`` if it didn't exist.
    """
    with _state_lock:
        receipt = db.get_receipt(receipt_id)
        external_id = db.delete_receipt(receipt_id)
    if external_id is not None:
        notifier.update_receipt_message(receipt, deleted=True)
    return external_id
