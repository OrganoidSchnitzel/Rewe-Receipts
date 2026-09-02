# Receipt Importer — Rewe (+ Lidl) → Spliit

A self-hosted service that imports grocery receipts, extracts the item list,
lets you review/correct it in a web UI, and creates a matching expense in a
[Spliit](https://spliit.app) group.

* **Rewe** — receipts arrive by email and are OCR'd by **Paperless-ngx**. This
  service is triggered by a Paperless post-consumption webhook (with a polling
  fallback), pulls the OCR text + PDF, and extracts line items.
* **Lidl** — the Lidl Plus API returns itemized receipts directly (no OCR). The
  data model and UI already support Lidl; automated Lidl Plus ingestion is a
  planned follow-up (config is reserved in `.env.example`). You can add a Lidl
  receipt today via **Manual add** by pasting the API JSON.

This branch delivers the **Rewe pipeline end-to-end** (Paperless → extraction →
review UI → Spliit), plus the shared data model, duplicate detection, and
deployment.

## How it works

```
Rewe email ─▶ Paperless-ngx (OCR, tag "Rewe")
                     │  post-consume webhook  (primary)
                     │  hourly poll           (fallback)
                     ▼
             Receipt Importer ──▶ extract items ──▶ SQLite
                     ▲                                  │
                     │            review + correct in web UI
                     │                                  ▼
                     └──────────── create expense ──▶ Spliit (tRPC API)
```

### Item extraction (Rewe OCR — hybrid)

1. **Known-items table** (deterministic): a learned map from a normalized OCR
   line to a canonical item name is checked first.
2. **Regex parser** (deterministic): the general `NAME … 1,23 A` line parser.
3. **Ollama fallback** (optional): lines neither of the above resolves are sent
   to a local LLM (`OLLAMA_URL` / `OLLAMA_MODEL`) for structured extraction.
4. **Learning loop**: when you confirm an AI-parsed item in the UI, it is
   upserted into the known-items table so the same line is matched
   deterministically next time (fewer Ollama calls over time).

The extraction logic lives behind small functions in `receipts/extraction.py`
so the matching rules and the LLM prompt can be improved independently.

### Duplicate detection

A receipt is never imported twice. `external_id` (Paperless document id for
Rewe, Lidl receipt id for Lidl) carries a **DB UNIQUE constraint**, and every
ingestion path checks it *before* doing any work — so a receipt is never
re-parsed, re-shown as new, or re-sent to Spliit, even when the webhook and the
poll race on the same document.

### Spliit expense creation

Spliit's backend is a Next.js app exposing a **tRPC** API. This service:

* fetches the group's participants via `groups.get`, and
* creates the expense via `groups.expenses.create`.

**Split behavior** (please confirm this matches your intent): the expense is an
**equal split among all current group members, with you as the payer**
(configure the payer with `SPLIIT_PAYER_PARTICIPANT_ID` or `SPLIIT_PAYER_NAME`;
otherwise the group's first participant is used). Verified against the Spliit
source: amounts are stored as **integer cents**, and in `EVENLY` mode Spliit
**ignores per-participant shares** and divides equally among the listed
participants — so the split is correct regardless of the `shares` value sent.
Spliit also supports `BY_SHARES` / `BY_AMOUNT` / `BY_PERCENTAGE`, so per-expense
custom splits can be added later if you want them.

On success the returned Spliit `expenseId` is stored against the receipt and it
is marked `settled`, so it can't create a duplicate expense.

## Configuration

All config is via environment variables — see [`.env.example`](.env.example)
for the full, documented list. Known values are pre-filled; you must supply:

* `PAPERLESS_TOKEN` — Paperless-ngx API token (Settings → My Profile → API Auth
  Token).
* `PAPERLESS_WEBHOOK_TOKEN` — a random shared secret for the webhook (recommended).
* `SPLIIT_PAYER_PARTICIPANT_ID` / `SPLIIT_PAYER_NAME` — who pays (optional).
* `OLLAMA_*` — only if you enable the AI extraction fallback (`OLLAMA_ENABLED=true`).

Self-hosted Spliit's tRPC API has **no auth by default**, so `SPLIIT_API_KEY` is
usually left blank. The web UI has **no authentication** (intended for LAN-only
use, per project decision).

## Deployment (Docker / Unraid)

The service must share a Docker network with your Paperless-ngx and Spliit
containers so it can reach them by name.

1. Copy config and fill in secrets:
   ```bash
   cp .env.example .env
   # edit .env
   ```
2. Point the compose file at your existing stack's network (edit the `networks`
   block in [`docker-compose.yml`](docker-compose.yml) — set it to the network
   Paperless/Spliit already use, `external: true`). Then:
   ```bash
   docker compose up -d --build
   ```
3. The data volume maps to the Unraid appdata convention
   `/mnt/user/appdata/receipt-importer` → `/app/data` (SQLite DB, stored PDFs,
   future Lidl token). Adjust the host path if yours differs.
4. Open `http://<host>:8000`.

### Wire up the Paperless webhook (recommended)

Mount [`scripts/paperless_post_consume.sh`](scripts/paperless_post_consume.sh)
into the Paperless container and set
`PAPERLESS_POST_CONSUME_SCRIPT` to its path, plus `RECEIPT_IMPORTER_URL` and
`RECEIPT_IMPORTER_WEBHOOK_TOKEN` (matching `PAPERLESS_WEBHOOK_TOKEN`). The script
notifies this service the moment a document is consumed; the importer re-checks
the Rewe tag and skips duplicates, so it's safe to call for every document. The
hourly poll covers any missed events even if you skip the webhook.

## Usage

* **Receipts** list — every imported receipt, newest first, with source badge
  and status (`pending` / `settled`).
* **Receipt detail** — the PDF shown alongside the extracted items. Tick items
  to include, correct name/quantity/price inline, watch the live selected
  total, then **Create Spliit expense**. Settled receipts are read-only.
* **Manual add** — paste Paperless OCR text, upload a PDF, or paste Lidl Plus
  API JSON to create a receipt to review.
* **Poll Paperless** — trigger the Rewe poll on demand.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
POLL_ENABLED=false DB_PATH=./data/receipts.db RECEIPT_FILES_DIR=./data/files python app.py
```

### Tests

```bash
python -m unittest discover -s tests -p 'test*.py'
```

Tests focus on the risk areas: deterministic item matching, duplicate
detection, and **Spliit payload construction** (a bug there would create
incorrect real expenses).

## Project layout

```
app.py                     Flask entrypoint (routes, webhook, wiring)
receipts/
  config.py                env-var configuration
  models.py                dataclasses (ExtractedItem, Receipt, …)
  db.py                    SQLite: receipts / items / known_items
  extraction.py            Lidl mapping + Rewe hybrid extraction + Ollama
  paperless.py             Paperless-ngx REST client
  spliit.py                Spliit tRPC client (participants, create expense)
  ingest.py                orchestration + duplicate detection
  scheduler.py             background polling thread
templates/  static/        web UI
scripts/paperless_post_consume.sh   Paperless webhook hook
```

## Roadmap

* Automated Lidl Plus API ingestion (login + OTP, persisted refresh token,
  scheduled fetch) feeding the same data model/UI.
* Per-expense custom split modes (Spliit supports shares/amount/percentage).
