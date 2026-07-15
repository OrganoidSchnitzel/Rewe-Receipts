from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import requests
from flask import Flask, render_template, request
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


@app.get("/")
def index() -> str:
    return render_template("index.html")


@app.post("/parse")
def parse_receipt() -> str:
    receipt_text = request.form.get("receipt_text", "")
    receipt_pdf = request.files.get("receipt_pdf")

    if receipt_pdf and receipt_pdf.filename:
        receipt_text = extract_text_from_pdf(receipt_pdf.stream)

    items = parse_receipt_text(receipt_text)
    return render_template("select_items.html", items=items, original_text=receipt_text)


@app.post("/send-to-spliit")
def send_to_spliit() -> str:
    selected_indices = {
        int(i) for i in request.form.getlist("selected_indices") if i.isdigit()
    }
    items: list[ReceiptItem] = []

    item_count = int(request.form.get("item_count", "0"))
    for index in range(item_count):
        name = request.form.get(f"item_name_{index}", "")
        price_value = request.form.get(f"item_price_{index}", "0")

        if not name:
            continue

        item = ReceiptItem(name=name, price=float(price_value))
        if index in selected_indices:
            items.append(item)

    total = calculate_total(items)
    try:
        result = send_amount_to_spliit(total=total, selected_items=items)
    except requests.RequestException as exc:
        result = {
            "sent": False,
            "message": f"Spliit transfer failed: {exc}",
            "payload": {
                "amount": total,
                "currency": "EUR",
                "description": f"REWE receipt split ({len(items)} items)",
                "items": [{"name": item.name, "price": item.price} for item in items],
            },
        }
    return render_template("result.html", selected_items=items, total=total, result=result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
