"""Lightweight background polling.

Daemon threads poll Paperless-ngx (Rewe) and, when configured, the Lidl Plus
API on their own intervals — a fallback in case a webhook event is missed, and
the sole trigger for Lidl. No external scheduler dependency; this is a
single-user home service.
"""
from __future__ import annotations

import logging
import threading

from . import config, ingest, lidl

logger = logging.getLogger(__name__)

_threads: list[threading.Thread] = []
_stop = threading.Event()


def _run_periodically(name: str, interval: int, job) -> None:
    logger.info("%s polling started (every %ss)", name, interval)
    # Initial delay so the web server is up before the first poll.
    while not _stop.wait(min(15, interval)):
        try:
            imported = job()
            if imported:
                logger.info("%s poll imported %d new receipt(s)", name, len(imported))
        except Exception as exc:
            logger.warning("%s poll iteration error: %s", name, exc)
        if _stop.wait(interval):
            break


def _spawn(name: str, interval: int, job) -> None:
    thread = threading.Thread(
        target=_run_periodically, args=(name, interval, job),
        name=f"{name.lower()}-poll", daemon=True,
    )
    thread.start()
    _threads.append(thread)


def start() -> None:
    if _threads:
        return
    _stop.clear()

    if config.POLL_ENABLED:
        _spawn("Rewe", config.POLL_INTERVAL_SECONDS, ingest.poll_rewe_documents)
    else:
        logger.info("Rewe polling disabled (POLL_ENABLED=false)")

    if lidl.is_configured():
        _spawn("Lidl", config.LIDL_POLL_INTERVAL_SECONDS, ingest.poll_lidl_tickets)
    else:
        logger.info("Lidl polling disabled (LIDL_ENABLED / LIDL_REFRESH_TOKEN unset)")


def stop() -> None:
    _stop.set()
