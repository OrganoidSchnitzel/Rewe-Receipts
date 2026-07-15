from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, abort, render_template, request
from pypdf import PdfReader

app = Flask(__name__)


@dataclass
class ReceiptItem:
    name: str
    price: float


IGNORED_LINE_PREFIXES = (
    "summe",
    "gesamt",
    "mwst",
    "ec-zahlung",
    "bar",
    "wechselgeld",
)



def parse_receipt_text(receipt_text: str) -> list[ReceiptItem]:
    items: list[ReceiptItem] = []
    price_regex = re.compile(r"-?\d{1,4}(?:[.,]\d{2})")

    for raw_line in receipt_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lowered = line.lower()
        if lowered.startswith(IGNORED_LINE_PREFIXES):
            continue

        price_matches = list(price_regex.finditer(line))
        if not price_matches:
            continue

        match = price_matches[-1]
        name = line[: match.start()].strip("- ")
        price = float(match.group(0).replace(",", "."))
        if name and price > 0:
            items.append(ReceiptItem(name=name, price=price))

    return items



def extract_text_from_pdf(file_obj: Any) -> str:
    reader = PdfReader(file_obj)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)



def calculate_total(items: list[ReceiptItem]) -> float:
    return round(sum(item.price for item in items), 2)



def receipts_db_path() -> Path:
    configured = os.getenv("RECEIPTS_DB_PATH", "/app/data/processed_receipts.json")
    return Path(configured)



def load_processed_receipts() -> list[dict[str, Any]]:
    path = receipts_db_path()
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return data



def save_processed_receipts(receipts: list[dict[str, Any]]) -> None:
    path = receipts_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")



def upsert_processed_receipt(
    all_items: list[ReceiptItem], selected_indices: list[int], receipt_id: str | None = None
) -> str:
    receipts = load_processed_receipts()
    record_id = receipt_id or uuid.uuid4().hex
    selected_items = [item for index, item in enumerate(all_items) if index in set(selected_indices)]

    record = {
        "id": record_id,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "all_items": [{"name": item.name, "price": item.price} for item in all_items],
        "selected_indices": selected_indices,
        "selected_total": calculate_total(selected_items),
    }

    for index, existing in enumerate(receipts):
        if existing.get("id") == record_id:
            receipts[index] = record
            break
    else:
        receipts.insert(0, record)

    save_processed_receipts(receipts)
    return record_id



def get_processed_receipt(receipt_id: str) -> dict[str, Any] | None:
    for receipt in load_processed_receipts():
        if receipt.get("id") == receipt_id:
            return receipt
    return None



def send_amount_to_spliit(total: float, selected_items: list[ReceiptItem]) -> dict[str, Any]:
    spliit_url = os.getenv("SPLIIT_API_URL")
    spliit_api_key = os.getenv("SPLIIT_API_KEY")

    payload = {
        "amount": total,
        "currency": "EUR",
        "description": f"REWE receipt split ({len(selected_items)} items)",
        "items": [{"name": item.name, "price": item.price} for item in selected_items],
    }

    if not spliit_url:
        return {
            "sent": False,
            "message": "SPLIIT_API_URL is not configured. No transfer was made.",
            "payload": payload,
        }

    headers = {"Content-Type": "application/json"}
    if spliit_api_key:
        headers["X-API-Key"] = spliit_api_key

    response = requests.post(spliit_url, json=payload, headers=headers, timeout=10)
    response.raise_for_status()

    return {"sent": True, "message": "Amount transferred to Spliit.", "payload": payload}



def parse_items_from_form() -> tuple[list[ReceiptItem], list[int]]:
    selected_indices = [int(i) for i in request.form.getlist("selected_indices") if i.isdigit()]
    selected_index_set = set(selected_indices)
    item_count = int(request.form.get("item_count", "0"))

    items: list[ReceiptItem] = []
    kept_selected: list[int] = []
    for index in range(item_count):
        name = request.form.get(f"item_name_{index}", "").strip()
        price_value = request.form.get(f"item_price_{index}", "0")

        if not name:
            continue

        try:
            price = float(price_value)
        except ValueError:
            continue

        items.append(ReceiptItem(name=name, price=price))
        actual_index = len(items) - 1
        if index in selected_index_set:
            kept_selected.append(actual_index)

    return items, kept_selected


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.get("/receipts")
def list_receipts() -> str:
    receipts = load_processed_receipts()
    return render_template("receipts.html", receipts=receipts)


@app.get("/receipts/<receipt_id>/edit")
def edit_receipt(receipt_id: str) -> str:
    receipt = get_processed_receipt(receipt_id)
    if not receipt:
        abort(404)

    items = [
        ReceiptItem(name=item.get("name", ""), price=float(item.get("price", 0.0)))
        for item in receipt.get("all_items", [])
        if item.get("name")
    ]
    selected_indices = [
        index
        for index in receipt.get("selected_indices", [])
        if isinstance(index, int) and 0 <= index < len(items)
    ]

    return render_template(
        "select_items.html",
        items=items,
        original_text="",
        preselected_indices=set(selected_indices),
        receipt_id=receipt_id,
    )


@app.post("/parse")
def parse_receipt() -> str:
    receipt_text = request.form.get("receipt_text", "")
    receipt_pdf = request.files.get("receipt_pdf")

    if receipt_pdf and receipt_pdf.filename:
        receipt_text = extract_text_from_pdf(receipt_pdf.stream)

    items = parse_receipt_text(receipt_text)
    return render_template(
        "select_items.html",
        items=items,
        original_text=receipt_text,
        preselected_indices=set(),
        receipt_id="",
    )


@app.post("/send-to-spliit")
def send_to_spliit() -> str:
    all_items, selected_indices = parse_items_from_form()
    selected_set = set(selected_indices)
    selected_items = [item for idx, item in enumerate(all_items) if idx in selected_set]
    total = calculate_total(selected_items)

    try:
        result = send_amount_to_spliit(total=total, selected_items=selected_items)
    except requests.RequestException as exc:
        result = {
            "sent": False,
            "message": f"Spliit transfer failed: {exc}",
            "payload": {
                "amount": total,
                "currency": "EUR",
                "description": f"REWE receipt split ({len(selected_items)} items)",
                "items": [{"name": item.name, "price": item.price} for item in selected_items],
            },
        }

    receipt_id = upsert_processed_receipt(
        all_items=all_items,
        selected_indices=selected_indices,
        receipt_id=request.form.get("receipt_id") or None,
    )

    return render_template(
        "result.html",
        selected_items=selected_items,
        total=total,
        result=result,
        receipt_id=receipt_id,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
