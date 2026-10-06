"""Telegram notifications.

Sends a message when a new receipt has been auto-ingested and is ready to
review. Notifications are outbound-only (a single HTTPS call to the Telegram
Bot API) — no inbound webhook, no extra ports.

Setup:
  1. Talk to @BotFather in Telegram, /newbot, copy the token -> TELEGRAM_BOT_TOKEN
  2. Send your new bot any message, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy the numeric
     "chat":{"id": ...} -> TELEGRAM_CHAT_ID
  3. Set TELEGRAM_ENABLED=true (and APP_PUBLIC_URL so the message links back).

All failures are swallowed and logged: a down/misconfigured Telegram must never
break ingestion.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

import requests

from . import config

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        config.TELEGRAM_ENABLED
        and config.TELEGRAM_BOT_TOKEN
        and config.TELEGRAM_CHAT_ID
    )


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


def send_message(text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    """Send an HTML message to the configured chat.

    Returns the new message's id (truthy) on success, ``None`` on failure.
    """
    if not is_configured():
        return None
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(_api("sendMessage"), json=payload, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        return response.json()["result"]["message_id"]
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return None


def get_updates(offset: Optional[int] = None, timeout: int = 30) -> list:
    """Long-poll for updates (callback button presses). Returns the result list."""
    if not is_configured():
        return []
    params = {"timeout": timeout, "allowed_updates": '["callback_query"]'}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(_api("getUpdates"), params=params, timeout=timeout + 10)
    response.raise_for_status()
    return response.json().get("result", [])


def answer_callback(callback_query_id: str, text: str = "") -> None:
    if not is_configured():
        return
    try:
        requests.post(
            _api("answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text[:200]},
            timeout=config.HTTP_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("Telegram answerCallback failed: %s", exc)


def edit_message(chat_id, message_id, text: str, reply_markup: Optional[dict] = None) -> None:
    """Replace a message's text. Without ``reply_markup`` its buttons are removed."""
    if not is_configured():
        return
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(
            _api("editMessageText"), json=payload, timeout=config.HTTP_TIMEOUT
        )
        body = response.json()
        # Re-rendering an unchanged state is harmless; anything else is worth a log.
        if not body.get("ok") and "not modified" not in str(body.get("description", "")):
            logger.warning("Telegram editMessage failed: %s", body.get("description"))
    except Exception as exc:
        logger.warning("Telegram editMessage failed: %s", exc)


def _esc(text: str) -> str:
    """Escape the characters Telegram's HTML parse mode cares about."""
    return html.escape(str(text), quote=False)


_HEADERS = {
    "pending": "🧾 <b>New {store}</b> · ready to review",
    "settled": "✅ <b>{store}</b> · shared to Spliit",
    "dismissed": "🚫 <b>{store}</b> · dismissed, nothing shared",
    "deleted": "🗑 <b>{store}</b> · deleted",
}


def render_receipt_message(receipt, outcome: str = "", deleted: bool = False):
    """Build ``(text, reply_markup)`` for a receipt's notification.

    The same renderer serves the first notification and every later edit, so
    the chat always mirrors the receipt's current state: buttons while it is
    pending, a plain status line once it has been handled (from anywhere).
    ``outcome`` is an optional result line (e.g. the Spliit split breakdown).
    """
    state = "deleted" if deleted else receipt.status
    store = _esc(receipt.store or receipt.source.upper() or "receipt")
    items = receipt.items
    count = len(items)
    date = (receipt.purchase_date or "")[:10]

    lines = [_HEADERS.get(state, _HEADERS["pending"]).format(store=store)]
    summary = f"{count} item{'' if count == 1 else 's'} · €{receipt.total_amount:.2f}"
    lines.append(f"{date} · {summary}" if date else summary)
    if items:
        names = [i.name for i in items]
        preview = ", ".join(_esc(name) for name in names[:5])
        if len(names) > 5:
            preview += f", +{len(names) - 5} more"
        lines.append(f"<i>{preview}</i>")
    if outcome:
        lines.append(_esc(outcome))

    link = (f"{config.APP_PUBLIC_URL}/receipts/{receipt.id}"
            if config.APP_PUBLIC_URL and not deleted else None)
    reply_markup = None
    if state == "pending" and config.TELEGRAM_TWO_WAY:
        # Approve creates the Spliit expense for all items straight from the chat;
        # Dismiss marks it handled with nothing shared; the URL button opens the
        # web UI to review/edit first.
        row = [{"text": "✅ Approve & split", "callback_data": f"approve:{receipt.id}"}]
        if link:
            row.append({"text": "✏️ Review", "url": link})
        reply_markup = {"inline_keyboard": [
            row,
            [{"text": "🚫 Dismiss (nothing to share)", "callback_data": f"dismiss:{receipt.id}"}],
        ]}
    elif link:
        lines.append(f'<a href="{html.escape(link, quote=True)}">Open receipt →</a>')

    return "\n".join(lines), reply_markup


def notify_new_receipt(receipt) -> None:
    """Notify that a freshly imported receipt is ready to review, and remember
    the message so it can be updated once the receipt is handled."""
    if not is_configured():
        return
    text, reply_markup = render_receipt_message(receipt)
    message_id = send_message(text, reply_markup=reply_markup)
    if message_id:
        from . import db  # lazy: keeps this module importable without the DB

        db.set_telegram_message(receipt.id, message_id)


def update_receipt_message(receipt, outcome: str = "", deleted: bool = False) -> None:
    """Re-render a receipt's notification to its current state (best-effort).

    Called after every state change — from the web UI or the chat — so the
    message never keeps offering buttons for a receipt that's already handled.
    """
    if not is_configured() or not receipt or not receipt.telegram_message_id:
        return
    try:
        text, reply_markup = render_receipt_message(receipt, outcome, deleted)
        edit_message(config.TELEGRAM_CHAT_ID, receipt.telegram_message_id, text, reply_markup)
    except Exception as exc:
        logger.warning("Could not update Telegram message for %s: %s", receipt.id, exc)
