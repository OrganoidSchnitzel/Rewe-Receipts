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

# --- Lidl Plus (ingestion added in a follow-up branch) -----------------------
LIDL_ENABLED = _get_bool("LIDL_ENABLED", False)
LIDL_EMAIL = _get("LIDL_EMAIL")
LIDL_PASSWORD = _get("LIDL_PASSWORD")
LIDL_COUNTRY = _get("LIDL_COUNTRY", "DE")
LIDL_LANGUAGE = _get("LIDL_LANGUAGE", "de")
LIDL_TOKEN_PATH = Path(_get("LIDL_TOKEN_PATH", "/app/data/lidl_token.json"))
LIDL_POLL_INTERVAL_SECONDS = _get_int("LIDL_POLL_INTERVAL_SECONDS", 3600)

# --- Web server --------------------------------------------------------------
APP_HOST = _get("APP_HOST", "0.0.0.0")
APP_PORT = _get_int("APP_PORT", 8000)

# HTTP timeout for outbound calls (Paperless / Spliit / Ollama), seconds.
HTTP_TIMEOUT = _get_int("HTTP_TIMEOUT", 30)
