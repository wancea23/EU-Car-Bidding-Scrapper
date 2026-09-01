# Playwright's own image already carries Chromium and every system library it
# needs — building Chrome onto a bare python image is the usual way this fails.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alcopa_scrape.py vpauto_scrape.py ./
COPY data/watchlist.json ./data/watchlist.json

ENV ALCOPA_BROWSER=playwright \
    PYTHONUNBUFFERED=1

# --lots keeps this independent of the local SQLite file; observations are
# appended as JSONL and merged back on the machine that owns the database.
CMD ["python", "alcopa_scrape.py", "watch", \
     "--lots", "data/watchlist.json", \
     "--out", "/data/observations.jsonl", \
     "--horizon", "5400", "--workers", "12"]
