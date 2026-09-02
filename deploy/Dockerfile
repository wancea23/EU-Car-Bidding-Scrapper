# Playwright's own image already carries Chromium and every system library it
# needs — building Chrome onto a bare python image is the usual way this fails.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alcopa_scrape.py vpauto_scrape.py ./
COPY deploy/run_day.sh /usr/local/bin/run_day
RUN chmod +x /usr/local/bin/run_day && mkdir -p /app/data

# The watch list is NOT baked in: it is rebuilt at the start of every run.
# A list copied at build time is stale by the next day, and a stale list makes
# the watcher exit 0 having captured nothing.

ENV ALCOPA_BROWSER=playwright \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris

# Needs GEMINI_API_KEY and a volume on /data (watch list + captured prices).
# Before deploying to any host, confirm that host's IP is not banned outright:
#   curl -s -o /dev/null -w '%{http_code}\n' -A 'Mozilla/5.0' \
#        https://www.alcopa-auction.fr/robots.txt
# 405 = WAF captcha, which this solves.  403 = CloudFront IP ban, unwinnable —
# that is what GitHub Actions' Azure ranges return. Test before you pay.
CMD ["run_day"]
