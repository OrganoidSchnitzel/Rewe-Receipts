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

from . import config, db, extraction, paperless

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
