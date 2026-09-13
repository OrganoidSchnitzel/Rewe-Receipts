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
from typing import Optional

from . import config, db, extraction, lidl, notifier, paperless

logger = logging.getLogger(__name__)


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

    total = round(sum(i.total_price for i in items), 2)
    notifier.notify_new_receipt(
        receipt_id, "REWE receipt", len(items), total,
        top_items=[i.name for i in items],
    )
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
    notifier.notify_new_receipt(
        receipt_id, receipt.store or "Lidl receipt",
        len(receipt.items), receipt.total_amount,
        top_items=[i.name for i in receipt.items],
    )
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
    Settled receipts and manual entries (no live source) are left untouched.
    Returns True if the receipt's items were refreshed.
    """
    receipt = db.get_receipt(receipt_id)
    if not receipt or receipt.status == "settled":
        return False

    source_ref = receipt.external_id.split(":", 1)
    if len(source_ref) != 2:
        return False
    ref = source_ref[1]

    if receipt.source == "rewe":
        text = paperless.get_document_text(int(ref))
        items = extraction.extract_rewe_items(text, known_items=db.get_known_items())
        db.replace_items(receipt_id, _items_to_rows(items))
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
        db.replace_items(receipt_id, _items_to_rows(parsed.items))
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
        r.source == "lidl" and r.status != "settled" for r in receipts
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
