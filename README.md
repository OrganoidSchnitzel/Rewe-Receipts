# Receipt Importer for Lidl & REWE

A minimal web app to process receipts from **Lidl** (via API) and **REWE** (via paperless-ngx OCR), then transfer selected items to Spliit.

## Features

- Support for two store sources:
  - **Lidl**: Parse JSON data from Lidl Plus API
  - **REWE**: Parse OCR text from paperless-ngx or PDF uploads
- Parse item lines and prices
- Select receipt items in a simple UI
- Sum selected items
- Send selected total + items to Spliit API
- Keep a list of processed receipts and reopen/edit them later

## Quick install with Docker

1. Build the image:

```bash
docker build -t receipt-importer:latest .
```

2. Create a local data directory (for processed receipts history):

```bash
mkdir -p ./receipt-importer-data
```

3. Run the container:

```bash
docker run -d \
  --name receipt-importer \
  -p 8000:8000 \
  -v "$(pwd)/receipt-importer-data:/app/data" \
  -e RECEIPTS_DB_PATH="/app/data/processed_receipts.json" \
  -e SPLIIT_API_URL="https://your-spliit-endpoint" \
  -e SPLIIT_API_KEY="optional-api-key" \
  receipt-importer:latest
```

4. Open `http://localhost:8000`.

## Unraid setup (easy path)

1. Open **Docker** in Unraid.
2. Add a new container from your built image/tag.
3. Set port mapping: `8000` (container) -> `8000` (host).
4. Add a persistent path mapping:
   - Host path: e.g. `/mnt/user/appdata/receipt-importer`
   - Container path: `/app/data`
5. Add environment variables:
   - `RECEIPTS_DB_PATH=/app/data/processed_receipts.json`
   - `SPLIIT_API_URL=<your spliit endpoint>`
   - `SPLIIT_API_KEY=<optional key>`
6. Start container and open `http://<unraid-ip>:8000`.

## How to use

### For REWE receipts (paperless-ngx)

1. Export the OCR text from paperless-ngx for your REWE receipt PDF
2. Select "REWE (paperless-ngx / PDF)" as the store source
3. Paste the OCR text into the text area (or upload the PDF directly)
4. Click **Parse receipt**
5. Select items to include
6. Click **Transfer selected amount**

### For Lidl receipts (Lidl Plus API)

1. Use the Lidl Plus API to fetch your receipt data (JSON format)
2. Select "Lidl (Plus API JSON)" as the store source
3. Paste the JSON response into the text area
4. Click **Parse receipt**
5. Select items to include
6. Click **Transfer selected amount**

### View processed receipts

Open **View processed receipts** to reopen any previous receipt and edit selection.

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

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `RECEIPTS_DB_PATH` | Path to store processed receipts JSON file | No (default: `/app/data/processed_receipts.json`) |
| `SPLIIT_API_URL` | URL of your Spliit API endpoint | No (if not set, no transfer is made) |
| `SPLIIT_API_KEY` | API key for Spliit (if required) | No |

## Example Lidl API Response Format

The app expects Lidl receipt data in this format:

```json
{
  "lineItems": [
    {
      "name": "Bananas",
      "totalPrice": {
        "value": 199
      }
    },
    {
      "name": "Bread",
      "totalPrice": {
        "value": 249
      }
    }
  ]
}
```

Note: Prices from Lidl API are expected in cents (will be divided by 100).
