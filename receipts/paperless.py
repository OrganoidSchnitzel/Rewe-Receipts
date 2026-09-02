"""Paperless-ngx REST API client (Rewe receipts).

Rewe receipts arrive by email and are OCR'd by Paperless-ngx. We identify them
by the configured tag (default "Rewe") and pull the OCR text + original PDF via
the REST API.

API reference: https://docs.paperless-ngx.com/api/
"""
from __future__ import annotations

from typing import Any, Optional

import requests

from . import config


class PaperlessError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    if not config.PAPERLESS_TOKEN:
        raise PaperlessError(
            "PAPERLESS_TOKEN is not configured; cannot call the Paperless API."
        )
    return {"Authorization": f"Token {config.PAPERLESS_TOKEN}"}


def _get(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    url = f"{config.PAPERLESS_URL}{path}"
    response = requests.get(
        url, headers=_headers(), params=params, timeout=config.HTTP_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def get_rewe_tag_id() -> Optional[int]:
    """Resolve the configured Rewe tag name to its Paperless tag id."""
    data = _get("/api/tags/", params={"name__iexact": config.PAPERLESS_REWE_TAG})
    results = data.get("results", [])
    if results:
        return results[0]["id"]
    # Fallback: some Paperless versions ignore name__iexact; scan the page.
    data = _get("/api/tags/", params={"page_size": 200})
    for tag in data.get("results", []):
        if tag.get("name", "").lower() == config.PAPERLESS_REWE_TAG.lower():
            return tag["id"]
    return None


def list_rewe_documents(page_size: int = 50) -> list[dict[str, Any]]:
    """Return Rewe-tagged documents, newest first.

    Falls back to a free-text tag query if the tag id can't be resolved.
    """
    tag_id = get_rewe_tag_id()
    params: dict[str, Any] = {"ordering": "-created", "page_size": page_size}
    if tag_id is not None:
        params["tags__id__all"] = tag_id
    else:
        params["tags__name__icontains"] = config.PAPERLESS_REWE_TAG
    data = _get("/api/documents/", params=params)
    return data.get("results", [])


def get_document(document_id: int) -> dict[str, Any]:
    return _get(f"/api/documents/{document_id}/")


def get_document_text(document_id: int) -> str:
    """Return the OCR'd text content of a document."""
    doc = get_document(document_id)
    return doc.get("content", "") or ""


def download_document(document_id: int, dest_path: str) -> str:
    """Download the original file (PDF) to ``dest_path``; returns the path."""
    url = f"{config.PAPERLESS_URL}/api/documents/{document_id}/download/"
    response = requests.get(
        url, headers=_headers(), timeout=config.HTTP_TIMEOUT, stream=True
    )
    response.raise_for_status()
    with open(dest_path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=8192):
            fh.write(chunk)
    return dest_path


def document_purchase_date(doc: dict[str, Any]) -> Optional[str]:
    """Best-effort purchase date: Paperless 'created' (document date)."""
    return doc.get("created") or doc.get("created_date")
