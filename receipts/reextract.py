"""Bulk re-extract every pending receipt from its source.

Run inside the container to apply improved parsing to receipts that were
imported by an earlier build:

    docker compose exec receipt-importer python -m receipts.reextract

Settled receipts and manual entries are left untouched.
"""
from __future__ import annotations

import logging

from . import db, ingest


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    updated, skipped = ingest.reextract_all()
    print(f"Re-extracted {updated} receipt(s); skipped {skipped} "
          f"(settled / manual / no source).")


if __name__ == "__main__":
    main()
