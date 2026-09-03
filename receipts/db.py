"""SQLite persistence layer.

Tables
------
receipts     one row per imported receipt. ``external_id`` carries a DB-level
             UNIQUE constraint so a receipt can never be imported twice, even
             if the webhook and the polling fallback race on the same Paperless
             document.
items        line items belonging to a receipt (with per-item include flag so
             the user's selection survives a reload).
known_items  learned mapping from a normalized Rewe OCR line to a canonical
             name, so a line the LLM parsed once is matched deterministically
             next time.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import config
from .models import ExtractedItem, Receipt, ReceiptItemRow


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    return config.DB_PATH


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS receipts (
                id                TEXT PRIMARY KEY,
                source            TEXT NOT NULL,
                external_id       TEXT NOT NULL UNIQUE,
                purchase_date     TEXT,
                store             TEXT,
                file_path         TEXT,
                total_amount      REAL NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'pending',
                spliit_expense_id TEXT,
                created_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id    TEXT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                quantity      REAL NOT NULL DEFAULT 1,
                unit_price    REAL NOT NULL DEFAULT 0,
                total_price   REAL NOT NULL DEFAULT 0,
                category      TEXT,
                included      INTEGER NOT NULL DEFAULT 1,
                position      INTEGER NOT NULL DEFAULT 0,
                source_method TEXT NOT NULL DEFAULT '',
                raw_line      TEXT
            );

            CREATE TABLE IF NOT EXISTS known_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern        TEXT NOT NULL UNIQUE,
                canonical_name TEXT NOT NULL,
                price_rule     TEXT,
                hit_count      INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT NOT NULL,
                updated_at     TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_items_receipt ON items(receipt_id);
            """
        )


# --- Duplicate detection -----------------------------------------------------

def receipt_exists(external_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM receipts WHERE external_id = ? LIMIT 1", (external_id,)
        ).fetchone()
        return row is not None


# --- Receipt writes ----------------------------------------------------------

def create_receipt(
    *,
    source: str,
    external_id: str,
    items: list[ExtractedItem],
    purchase_date: Optional[str] = None,
    store: str = "",
    file_path: Optional[str] = None,
    total_amount: Optional[float] = None,
) -> Optional[str]:
    """Insert a receipt and its items.

    Returns the new receipt id, or ``None`` if a receipt with the same
    ``external_id`` already exists (the insert is skipped entirely — the caller
    should not re-extract or re-show it as new).
    """
    receipt_id = uuid.uuid4().hex
    if total_amount is None:
        total_amount = round(sum(i.total_price for i in items), 2)

    with connect() as conn:
        try:
            conn.execute(
                """
                INSERT INTO receipts
                    (id, source, external_id, purchase_date, store, file_path,
                     total_amount, status, spliit_expense_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                """,
                (
                    receipt_id,
                    source,
                    external_id,
                    purchase_date,
                    store,
                    file_path,
                    total_amount,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            # UNIQUE(external_id) violation: another path already imported it.
            return None

        for position, item in enumerate(items):
            conn.execute(
                """
                INSERT INTO items
                    (receipt_id, name, quantity, unit_price, total_price,
                     category, included, position, source_method, raw_line)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    receipt_id,
                    item.name,
                    item.quantity,
                    item.unit_price,
                    item.total_price,
                    item.category,
                    position,
                    item.source_method,
                    item.raw_line,
                ),
            )
    return receipt_id


def replace_items(receipt_id: str, items: list[dict]) -> None:
    """Replace a receipt's items with the edited set from the UI.

    ``items`` is a list of dicts: name, quantity, unit_price, total_price,
    included. Also refreshes the receipt total to the sum of *included* items.
    """
    with connect() as conn:
        conn.execute("DELETE FROM items WHERE receipt_id = ?", (receipt_id,))
        total = 0.0
        for position, item in enumerate(items):
            included = 1 if item.get("included") else 0
            total_price = float(item.get("total_price", 0) or 0)
            if included:
                total += total_price
            conn.execute(
                """
                INSERT INTO items
                    (receipt_id, name, quantity, unit_price, total_price,
                     category, included, position, source_method, raw_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    item.get("name", ""),
                    float(item.get("quantity", 1) or 1),
                    float(item.get("unit_price", 0) or 0),
                    total_price,
                    item.get("category"),
                    included,
                    position,
                    item.get("source_method", "") or "",
                    item.get("raw_line"),
                ),
            )
        conn.execute(
            "UPDATE receipts SET total_amount = ? WHERE id = ?",
            (round(total, 2), receipt_id),
        )


def mark_settled(receipt_id: str, spliit_expense_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE receipts SET status = 'settled', spliit_expense_id = ? WHERE id = ?",
            (spliit_expense_id, receipt_id),
        )


# --- Receipt reads -----------------------------------------------------------

def _row_to_receipt(row: sqlite3.Row) -> Receipt:
    return Receipt(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        purchase_date=row["purchase_date"],
        store=row["store"] or "",
        file_path=row["file_path"],
        total_amount=row["total_amount"],
        status=row["status"],
        spliit_expense_id=row["spliit_expense_id"],
        created_at=row["created_at"],
    )


def list_receipts() -> list[Receipt]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, (
                SELECT COUNT(*) FROM items i WHERE i.receipt_id = r.id
            ) AS item_count
            FROM receipts r
            ORDER BY COALESCE(r.purchase_date, r.created_at) DESC, r.created_at DESC
            """
        ).fetchall()
        result = []
        for r in rows:
            receipt = _row_to_receipt(r)
            receipt.item_count = r["item_count"]
            result.append(receipt)
        return result


def get_receipt(receipt_id: str) -> Optional[Receipt]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if not row:
            return None
        receipt = _row_to_receipt(row)
        item_rows = conn.execute(
            "SELECT * FROM items WHERE receipt_id = ? ORDER BY position", (receipt_id,)
        ).fetchall()
        receipt.items = [
            ReceiptItemRow(
                id=ir["id"],
                receipt_id=ir["receipt_id"],
                name=ir["name"],
                quantity=ir["quantity"],
                unit_price=ir["unit_price"],
                total_price=ir["total_price"],
                category=ir["category"],
                included=bool(ir["included"]),
                position=ir["position"],
                source_method=ir["source_method"],
                raw_line=ir["raw_line"],
            )
            for ir in item_rows
        ]
        return receipt


def delete_receipt(receipt_id: str) -> Optional[str]:
    """Delete a receipt (and its items, via ON DELETE CASCADE).

    Returns the receipt's ``external_id`` so the caller can decide whether a
    re-poll should re-import it, or ``None`` if it didn't exist.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT external_id FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
        return row["external_id"]


# --- Known-items (learning loop) --------------------------------------------

def get_known_items() -> dict[str, dict]:
    """Return the known-items table keyed by normalized pattern."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT pattern, canonical_name, price_rule FROM known_items"
        ).fetchall()
        return {
            r["pattern"]: {
                "canonical_name": r["canonical_name"],
                "price_rule": r["price_rule"],
            }
            for r in rows
        }


def upsert_known_item(
    pattern: str, canonical_name: str, price_rule: Optional[str] = None
) -> None:
    if not pattern or not canonical_name:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO known_items
                (pattern, canonical_name, price_rule, hit_count, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(pattern) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                price_rule = COALESCE(excluded.price_rule, known_items.price_rule),
                hit_count = known_items.hit_count + 1,
                updated_at = excluded.updated_at
            """,
            (pattern, canonical_name, price_rule, _now(), _now()),
        )
