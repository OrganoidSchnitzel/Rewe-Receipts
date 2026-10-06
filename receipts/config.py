"""Central configuration read from environment variables.

Every value has a sensible default so the app can boot for local development,
but production secrets (tokens, credentials) must be supplied via the
environment. See ``.env.example`` for the full list.
"""
from __future__ import annotations

import os
from pathlib import Path


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- Storage -----------------------------------------------------------------
DB_PATH = Path(_get("DB_PATH", "/app/data/receipts.db"))
RECEIPT_FILES_DIR = Path(_get("RECEIPT_FILES_DIR", "/app/data/receipts"))

# --- Paperless-ngx (Rewe ingestion) ------------------------------------------
PAPERLESS_URL = _get("PAPERLESS_URL", "http://192.168.178.100:8000").rstrip("/")
PAPERLESS_TOKEN = _get("PAPERLESS_TOKEN")
PAPERLESS_REWE_TAG = _get("PAPERLESS_REWE_TAG", "Rewe")
# Shared secret the Paperless post-consume script must present to the webhook.
PAPERLESS_WEBHOOK_TOKEN = _get("PAPERLESS_WEBHOOK_TOKEN")
# After a successful import, tag the Paperless document (non-destructive) so you
# can tell handled from unhandled receipts in Paperless. Empty tag disables it.
PAPERLESS_TAG_ON_IMPORT = _get_bool("PAPERLESS_TAG_ON_IMPORT", True)
PAPERLESS_PROCESSED_TAG = _get("PAPERLESS_PROCESSED_TAG", "ReceiptImported")

# --- Polling scheduler -------------------------------------------------------
POLL_ENABLED = _get_bool("POLL_ENABLED", True)
POLL_INTERVAL_SECONDS = _get_int("POLL_INTERVAL_SECONDS", 3600)

# --- Spliit ------------------------------------------------------------------
SPLIIT_URL = _get("SPLIIT_URL", "http://192.168.178.100:3333").rstrip("/")
SPLIIT_GROUP_ID = _get("SPLIIT_GROUP_ID", "4rYxmoWLtDtiRmLLY3vZC")
# Self-hosted Spliit tRPC has no auth by default; leave blank unless a reverse
# proxy in front of it requires a bearer token / shared header.
SPLIIT_API_KEY = _get("SPLIIT_API_KEY")
# Who pays. Prefer the participant id (stable); name is a convenience fallback
# matched case-insensitively against the group's participants. If neither is
# set, the first participant returned by the group is used as payer.
SPLIIT_PAYER_PARTICIPANT_ID = _get("SPLIIT_PAYER_PARTICIPANT_ID")
SPLIIT_PAYER_NAME = _get("SPLIIT_PAYER_NAME")
SPLIIT_CURRENCY = _get("SPLIIT_CURRENCY", "EUR")

# --- Ollama (AI extraction fallback for Rewe OCR) ----------------------------
OLLAMA_URL = _get("OLLAMA_URL", "http://192.168.178.100:11434").rstrip("/")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "llama3.1")
# When false, extraction stays purely deterministic (known-items + regex).
OLLAMA_ENABLED = _get_bool("OLLAMA_ENABLED", False)

# --- Lidl Plus ---------------------------------------------------------------
# Auth is bootstrapped once, outside the container, with the lidl-plus CLI
# (`lidl-plus auth`), which prints a refresh token after the OTP step. Paste it
# here; the service then runs unattended, refreshing access tokens itself.
LIDL_ENABLED = _get_bool("LIDL_ENABLED", False)
LIDL_REFRESH_TOKEN = _get("LIDL_REFRESH_TOKEN")
LIDL_COUNTRY = _get("LIDL_COUNTRY", "DE")
LIDL_LANGUAGE = _get("LIDL_LANGUAGE", "de")
LIDL_POLL_INTERVAL_SECONDS = _get_int("LIDL_POLL_INTERVAL_SECONDS", 3600)
# The lidl-plus library hard-codes App-Version "999.99.9"; Lidl's WAF now resets
# requests carrying an impossible version, so we send a realistic one. Bump this
# when Lidl ships a new app release if calls start failing again.
LIDL_APP_VERSION = _get("LIDL_APP_VERSION", "15.30.7")
LIDL_OPERATING_SYSTEM = _get("LIDL_OPERATING_SYSTEM", "iOs")
LIDL_USER_AGENT = _get("LIDL_USER_AGENT")

# --- Telegram notifications --------------------------------------------------
TELEGRAM_ENABLED = _get_bool("TELEGRAM_ENABLED", False)
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")
# Two-way: show an "Approve & split" button on notifications and act on presses
# via long-polling (no inbound webhook / exposed port needed). Only presses from
# TELEGRAM_CHAT_ID are honored.
TELEGRAM_TWO_WAY = _get_bool("TELEGRAM_TWO_WAY", True)

# --- Web server --------------------------------------------------------------
APP_HOST = _get("APP_HOST", "0.0.0.0")
APP_PORT = _get_int("APP_PORT", 8000)
# Base URL the app is reached at, used to build links in notifications
# (e.g. http://192.168.178.100:8881). No trailing slash.
APP_PUBLIC_URL = _get("APP_PUBLIC_URL").rstrip("/")

# HTTP timeout for outbound calls (Paperless / Spliit / Ollama), seconds.
HTTP_TIMEOUT = _get_int("HTTP_TIMEOUT", 30)
