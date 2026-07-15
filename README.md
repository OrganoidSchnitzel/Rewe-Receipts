# Rewe-Receipts

Minimal web app to process REWE receipts from paperless-ngx and transfer selected totals to Spliit.

## What it does

- Accepts REWE receipt input as either:
  - PDF file (text extracted via `pypdf`), or
  - plain text (for paperless OCR text)
- Parses item lines and prices
- Shows a UI with selectable items
- Sums selected items
- Sends total + selected item details to Spliit API

## Run with Docker

```bash
docker build -t rewe-receipts .
docker run --rm -p 8000:8000 \
  -e SPLIIT_API_URL="https://your-spliit-endpoint" \
  -e SPLIIT_API_KEY="optional-api-key" \
  rewe-receipts
```

Open `http://localhost:8000`.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Tests

```bash
python -m unittest discover -s tests
```
