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

import logging

import requests

from . import config

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(
        config.TELEGRAM_ENABLED
        and config.TELEGRAM_BOT_TOKEN
        and config.TELEGRAM_CHAT_ID
    )


def send_message(text: str) -> bool:
    """Send a Markdown message to the configured chat. Returns success."""
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=config.HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return False


def notify_new_receipt(
    receipt_id: str, store: str, item_count: int, total_amount: float
) -> None:
    """Notify that a freshly imported receipt is ready to review."""
    if not is_configured():
        return

    lines = [
        f"🧾 *New {store or 'receipt'} ready to review*",
        f"{item_count} item(s) · €{total_amount:.2f}",
    ]
    if config.APP_PUBLIC_URL:
        link = f"{config.APP_PUBLIC_URL}/receipts/{receipt_id}"
        lines.append(f"[Open receipt]({link})")
    send_message("\n".join(lines))
