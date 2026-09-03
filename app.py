"""Flask web app: entrypoint.

Ties together ingestion (Paperless webhook + polling), the extraction pipeline,
the review UI, and Spliit expense creation. Kept as the container's single
process; the poller runs as a daemon thread.
"""
from __future__ import annotations

import json
import logging

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from receipts import config, db, extraction, ingest, scheduler, spliit
from receipts.extraction import normalize_line

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "receipt-importer"  # only used for flash messages (LAN-only)


# --- helpers -----------------------------------------------------------------

def _parse_items_from_form() -> list[dict]:
    """Read the edited item rows submitted from the detail view."""
    count = int(request.form.get("item_count", "0") or 0)
    selected = set(request.form.getlist("included"))
    items: list[dict] = []
    for index in range(count):
        name = request.form.get(f"name_{index}", "").strip()
        if not name:
            continue
        try:
            quantity = float(request.form.get(f"quantity_{index}", "1") or 1)
        except ValueError:
            quantity = 1.0
        try:
            total_price = float(request.form.get(f"total_price_{index}", "0") or 0)
        except ValueError:
            total_price = 0.0
        unit_price = round(total_price / quantity, 2) if quantity else total_price
        items.append(
            {
                "name": name,
                "quantity": quantity,
                "unit_price": unit_price,
                "total_price": total_price,
                "included": str(index) in selected,
                "raw_line": request.form.get(f"raw_line_{index}", "") or None,
                "source_method": request.form.get(f"source_method_{index}", "") or "",
            }
        )
    return items


# --- receipt list / detail ---------------------------------------------------

@app.get("/")
def index() -> str:
    receipts = db.list_receipts()
    return render_template("index.html", receipts=receipts)


@app.get("/receipts/<receipt_id>")
def receipt_detail(receipt_id: str):
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        abort(404)
    included_total = round(
        sum(i.total_price for i in receipt.items if i.included), 2
    )
    return render_template(
        "detail.html", receipt=receipt, included_total=included_total
    )


@app.get("/receipts/<receipt_id>/file")
def receipt_file(receipt_id: str):
    receipt = db.get_receipt(receipt_id)
    if not receipt or not receipt.file_path:
        abort(404)
    return send_file(receipt.file_path)


@app.post("/receipts/<receipt_id>/delete")
def delete_receipt(receipt_id: str):
    external_id = db.delete_receipt(receipt_id)
    if external_id is None:
        abort(404)
    flash(
        "Receipt deleted. If it was a Rewe document, the next Paperless poll "
        "(or a manual poll) will re-import it with the current extraction."
    )
    return redirect(url_for("index"))


@app.post("/receipts/<receipt_id>/save")
def save_receipt(receipt_id: str):
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        abort(404)
    items = _parse_items_from_form()
    db.replace_items(receipt_id, items)
    flash("Saved.")
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


@app.post("/receipts/<receipt_id>/spliit")
def create_spliit_expense(receipt_id: str):
    receipt = db.get_receipt(receipt_id)
    if not receipt:
        abort(404)
    if receipt.status == "settled":
        flash("Receipt is already settled; not creating a duplicate expense.")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    # Persist any last edits/selection from the form first.
    items = _parse_items_from_form()
    if items:
        db.replace_items(receipt_id, items)
        receipt = db.get_receipt(receipt_id)

    included = [i for i in receipt.items if i.included]
    if not included:
        flash("Select at least one item before creating an expense.")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    # Learning loop: remember confirmed AI-parsed lines so they are matched
    # deterministically next time.
    _learn_from_form(items)

    total = round(sum(i.total_price for i in included), 2)
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
        flash(f"Spliit expense creation failed: {exc}")
        return redirect(url_for("receipt_detail", receipt_id=receipt_id))

    db.mark_settled(receipt_id, expense_id)
    flash(f"Created Spliit expense {expense_id} for €{total:.2f}.")
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


def _learn_from_form(items: list[dict]) -> None:
    for item in items:
        if not item.get("included"):
            continue
        if item.get("source_method") != "ollama":
            continue
        raw_line = item.get("raw_line") or normalize_line(item["name"])
        db.upsert_known_item(raw_line, item["name"])


# --- manual entry (paste OCR / upload PDF / paste Lidl JSON) ------------------

@app.get("/manual")
def manual_form() -> str:
    return render_template("manual.html")


@app.post("/manual")
def manual_submit():
    receipt_text = request.form.get("receipt_text", "")
    receipt_pdf = request.files.get("receipt_pdf")
    store_source = (request.form.get("store_source") or "rewe").strip().lower()
    external_ref = (request.form.get("external_id") or "").strip()

    if receipt_pdf and receipt_pdf.filename:
        from pypdf import PdfReader  # imported lazily; only needed for PDF uploads

        reader = PdfReader(receipt_pdf.stream)
        receipt_text = "\n".join(p.extract_text() or "" for p in reader.pages)

    if store_source == "lidl":
        try:
            items = extraction.parse_lidl_receipt(json.loads(receipt_text))
        except (json.JSONDecodeError, TypeError):
            items = extraction.extract_rewe_items(receipt_text, db.get_known_items())
            store_source = "rewe"
    else:
        items = extraction.extract_rewe_items(receipt_text, db.get_known_items())

    import uuid
    external_id = external_ref or f"manual:{store_source}:{uuid.uuid4().hex[:12]}"
    receipt_id = db.create_receipt(
        source=store_source,
        external_id=external_id,
        items=items,
        store=store_source.upper(),
    )
    if receipt_id is None:
        flash("A receipt with that external id was already imported.")
        return redirect(url_for("index"))
    return redirect(url_for("receipt_detail", receipt_id=receipt_id))


# --- Paperless webhook + manual poll ----------------------------------------

@app.post("/webhook/paperless")
def paperless_webhook():
    """Post-consumption webhook from Paperless-ngx.

    Auth: a shared token via the ``X-Webhook-Token`` header (or ``token`` in the
    body), compared to ``PAPERLESS_WEBHOOK_TOKEN``. Document id from JSON or form
    (``document_id``).
    """
    if config.PAPERLESS_WEBHOOK_TOKEN:
        supplied = request.headers.get("X-Webhook-Token") or request.values.get("token")
        if supplied != config.PAPERLESS_WEBHOOK_TOKEN:
            abort(401)

    payload = request.get_json(silent=True) or request.form
    document_id = payload.get("document_id") or payload.get("id")
    if document_id is None:
        return jsonify({"error": "document_id is required"}), 400
    try:
        document_id = int(document_id)
    except (TypeError, ValueError):
        return jsonify({"error": "document_id must be an integer"}), 400

    try:
        receipt_id = ingest.ingest_rewe_document(document_id)
    except Exception as exc:
        logger.exception("Webhook ingest failed")
        return jsonify({"error": str(exc)}), 500

    if receipt_id is None:
        return jsonify({"status": "skipped", "reason": "duplicate or no items"}), 200
    return jsonify({"status": "imported", "receipt_id": receipt_id}), 201


@app.post("/poll")
def manual_poll():
    imported = ingest.poll_rewe_documents()
    flash(f"Poll complete: imported {len(imported)} new receipt(s).")
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


def create_app() -> Flask:
    db.init_db()
    scheduler.start()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host=config.APP_HOST, port=config.APP_PORT)
