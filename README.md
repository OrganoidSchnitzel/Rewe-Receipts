# Rewe-Receipts

A minimal web app to process REWE receipts from paperless-ngx and transfer selected totals to Spliit.

## Features

- Accept receipt input as either:
  - PDF file (text extracted via `pypdf`), or
  - plain text (paperless OCR text)
- Parse item lines and prices
- Select receipt items in a simple UI
- Sum selected items
- Send selected total + items to Spliit API
- Keep a list of processed receipts and reopen/edit them later

## Quick install with Docker

1. Build the image:

```bash
docker build -t rewe-receipts:latest .
```

2. Create a local data directory (for processed receipts history):

```bash
mkdir -p ./rewe-receipts-data
```

3. Run the container:

```bash
docker run -d \
  --name rewe-receipts \
  -p 8000:8000 \
  -v "$(pwd)/rewe-receipts-data:/app/data" \
  -e RECEIPTS_DB_PATH="/app/data/processed_receipts.json" \
  -e SPLIIT_API_URL="https://your-spliit-endpoint" \
  -e SPLIIT_API_KEY="optional-api-key" \
  rewe-receipts:latest
```

4. Open `http://localhost:8000`.

## Unraid setup (easy path)

1. Open **Docker** in Unraid.
2. Add a new container from your built image/tag.
3. Set port mapping: `8000` (container) -> `8000` (host).
4. Add a persistent path mapping:
   - Host path: e.g. `/mnt/user/appdata/rewe-receipts`
   - Container path: `/app/data`
5. Add environment variables:
   - `RECEIPTS_DB_PATH=/app/data/processed_receipts.json`
   - `SPLIIT_API_URL=<your spliit endpoint>`
   - `SPLIIT_API_KEY=<optional key>`
6. Start container and open `http://<unraid-ip>:8000`.

## How to use

1. Paste OCR text from paperless, or upload a receipt PDF.
2. Click **Parse receipt**.
3. Select items to include.
4. Click **Transfer selected amount**.
5. Open **View processed receipts** to reopen any previous receipt and edit selection.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Tests

```bash
python -m unittest discover -s tests -p 'test*.py'
```
