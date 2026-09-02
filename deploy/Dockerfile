# Playwright's own image already carries Chromium and every system library it
# needs. Building Chrome onto a bare python image is the usual way this fails,
# and it is why nothing has to be installed on the host.
#
#   docker build -f deploy/Dockerfile -t alcopa-watcher .     (context = repo root)
#
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alcopa_scrape.py vpauto_scrape.py ./
COPY deploy/run_day.sh /usr/local/bin/run_day
RUN chmod +x /usr/local/bin/run_day

# Runs as an unprivileged user; the image needs no capabilities, no
# privileged mode and no docker-group membership on the host.
#
# The Playwright base image ALREADY ships a uid-1000 account ("pwuser"), so
# `useradd -m -u 1000 runner` collides with it and fails the build outright.
# Reuse the existing uid instead of trying to create it.
RUN mkdir -p /data && chown -R 1000:1000 /data /app
USER 1000

# Chromium needs a writable HOME for its profile and crash dumps; without one
# it exits with an unhelpful error that looks like a sandbox problem.
ENV HOME=/tmp

# Every file this writes — watch list, captures, WAF token, debug dumps —
# lands under ALCOPA_DATA, so one volume mounted at /data is the whole of it.
ENV ALCOPA_DATA=/data \
    ALCOPA_BROWSER=playwright \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris

# TZ above only affects how timestamps PRINT. Scheduling is epoch-based
# throughout (the site's own data-ts against time.time()), so the container's
# timezone cannot shift when a capture fires.
#
# The watch list is deliberately NOT baked in: a list copied at build time is
# stale by the next day, and a stale list makes the watcher capture nothing
# while still exiting 0.

VOLUME ["/data"]
CMD ["run_day"]
