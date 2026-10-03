"""Two-way Telegram: act on inline-button presses via long-polling.

A daemon thread calls ``getUpdates`` (long poll) — no inbound webhook or exposed
port needed, so it works behind a home NAT. Only callback presses coming from
the configured ``TELEGRAM_CHAT_ID`` are honored. "Approve & split" settles the
receipt (creates the Spliit expense for its included items) and "Dismiss" marks
it handled with nothing shared; both are idempotent, so a stale press on an
already-handled receipt changes nothing and just refreshes the message.
"""
from __future__ import annotations

import logging
import threading

from . import config, notifier

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled() -> bool:
    return config.TELEGRAM_TWO_WAY and notifier.is_configured()


def start() -> None:
    global _thread
    if not enabled():
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="telegram-bot", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def _loop() -> None:
    logger.info("Telegram two-way bot started (long-polling)")
    offset = None
    while not _stop.is_set():
        try:
            updates = notifier.get_updates(offset=offset, timeout=30)
        except Exception as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            _stop.wait(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                _handle_update(update)
            except Exception as exc:
                logger.warning("Telegram update handling failed: %s", exc)


def _handle_update(update: dict) -> None:
    callback = update.get("callback_query")
    if not callback:
        return

    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    # Only honor presses from the configured chat.
    if str(chat.get("id")) != str(config.TELEGRAM_CHAT_ID):
        notifier.answer_callback(callback["id"], "Not authorized.")
        return

    data = callback.get("data") or ""
    if ":" not in data:
        notifier.answer_callback(callback["id"], "")
        return
    action, receipt_id = data.split(":", 1)
    if action not in ("approve", "dismiss"):
        notifier.answer_callback(callback["id"], "")
        return

    from . import db, ingest  # lazy import avoids a circular import at module load

    # Notifications sent before message ids were stored: link this message to
    # its receipt now, so it is updated like any other from here on.
    receipt = db.get_receipt(receipt_id)
    if receipt and not receipt.telegram_message_id and message.get("message_id"):
        db.set_telegram_message(receipt_id, message["message_id"])

    if action == "approve":
        ok, result = ingest.settle_receipt(receipt_id)
    else:  # dismiss
        ok, result = ingest.dismiss_receipt(receipt_id)

    # On success ingest has already re-rendered the message. On a stale press
    # (e.g. settled in the web UI meanwhile) refresh it to the current state.
    notifier.answer_callback(callback["id"], result)
    if not ok:
        notifier.update_receipt_message(db.get_receipt(receipt_id))
