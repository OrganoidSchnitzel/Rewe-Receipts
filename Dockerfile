FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data (SQLite DB, stored receipt PDFs, Lidl token) lives on a mounted volume.
VOLUME ["/app/data"]

EXPOSE 8000

CMD ["python", "app.py"]
