#!/usr/bin/env bash
# Paperless-ngx post-consumption script.
#
# Notifies the Receipt Importer that a document was consumed, so Rewe receipts
# are imported immediately (the importer itself re-checks the Rewe tag and skips
# duplicates, so it's safe to call for every document).
#
# --- Install ---------------------------------------------------------------
# 1. Mount this script into the Paperless container, e.g. add to the paperless
#    service in docker-compose.yml:
#
#      volumes:
#        - ./scripts/paperless_post_consume.sh:/usr/src/paperless/scripts/post_consume.sh:ro
#      environment:
#        PAPERLESS_POST_CONSUME_SCRIPT: /usr/src/paperless/scripts/post_consume.sh
#        RECEIPT_IMPORTER_URL: http://receipt-importer:8000
#        RECEIPT_IMPORTER_WEBHOOK_TOKEN: <same value as PAPERLESS_WEBHOOK_TOKEN>
#
# 2. Make sure `curl` is available in the Paperless image (it usually is).
#
# Paperless passes the document id as $DOCUMENT_ID (and older versions as $1).
# Docs: https://docs.paperless-ngx.com/advanced_usage/#post-consumption-script
# ---------------------------------------------------------------------------
set -euo pipefail

IMPORTER_URL="${RECEIPT_IMPORTER_URL:-http://receipt-importer:8000}"
TOKEN="${RECEIPT_IMPORTER_WEBHOOK_TOKEN:-}"
DOC_ID="${DOCUMENT_ID:-${1:-}}"

if [ -z "${DOC_ID}" ]; then
  echo "post_consume: no document id provided" >&2
  exit 0
fi

curl -fsS -X POST "${IMPORTER_URL}/webhook/paperless" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Token: ${TOKEN}" \
  -d "{\"document_id\": ${DOC_ID}}" \
  >/dev/null 2>&1 || echo "post_consume: importer notification failed for doc ${DOC_ID}" >&2

exit 0
