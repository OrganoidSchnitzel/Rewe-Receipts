"""Lightweight background polling.

A daemon thread polls Paperless-ngx for new Rewe documents on an interval, as a
fallback in case a webhook event is missed. No external scheduler dependency —
this is a single-user home service.
"""
from __future__ import annotations

import logging
import threading

from . import config, ingest

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    logger.info("Rewe polling started (every %ss)", config.POLL_INTERVAL_SECONDS)
    # Initial delay so the web server is up before the first poll.
    while not _stop.wait(min(15, config.POLL_INTERVAL_SECONDS)):
        try:
            imported = ingest.poll_rewe_documents()
            if imported:
                logger.info("Poll imported %d new receipt(s)", len(imported))
        except Exception as exc:
            logger.warning("Poll iteration error: %s", exc)
        if _stop.wait(config.POLL_INTERVAL_SECONDS):
            break


def start() -> None:
    global _thread
    if not config.POLL_ENABLED:
        logger.info("Polling disabled (POLL_ENABLED=false)")
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="rewe-poll", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
