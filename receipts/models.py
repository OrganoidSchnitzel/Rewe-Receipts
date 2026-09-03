"""Plain data structures shared across the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractedItem:
    """A single line item extracted from a receipt (Lidl API or Rewe OCR)."""

    name: str
    quantity: float = 1.0
    unit_price: float = 0.0
    total_price: float = 0.0
    category: Optional[str] = None
    # How this item was produced: "known" (deterministic table), "regex",
    # "lidl" (structured API) or "ollama" (LLM fallback). Drives the learning
    # loop: only "ollama"-sourced lines are worth persisting on confirmation.
    source_method: str = "regex"
    # The normalized OCR line this item came from, if any. Used to upsert into
    # the known-items table when the user confirms an AI-parsed item.
    raw_line: Optional[str] = None

    def __post_init__(self) -> None:
        # Keep unit/total consistent when only one is provided.
        if not self.total_price and self.unit_price:
            self.total_price = round(self.unit_price * (self.quantity or 1), 2)
        if not self.unit_price and self.total_price:
            qty = self.quantity or 1
            self.unit_price = round(self.total_price / qty, 2) if qty else self.total_price


@dataclass
class Receipt:
    id: str
    source: str  # "lidl" | "rewe"
    external_id: str
    purchase_date: Optional[str]
    store: str
    file_path: Optional[str]
    total_amount: float
    status: str  # "pending" | "settled"
    spliit_expense_id: Optional[str]
    created_at: str
    item_count: int = 0
    items: list["ReceiptItemRow"] = field(default_factory=list)


@dataclass
class ReceiptItemRow:
    id: int
    receipt_id: str
    name: str
    quantity: float
    unit_price: float
    total_price: float
    category: Optional[str]
    included: bool
    position: int
    source_method: str = ""
    raw_line: Optional[str] = None
