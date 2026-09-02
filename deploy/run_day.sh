#!/bin/sh
# One auction day, then sleep to the next. Runs as the container's only
# process, so `restart: unless-stopped` is the whole scheduler — no cron
# inside the image, nothing to install on the host.
#
# Alcopa strips a lot's price the second it sells, so the point of this
# container is to be awake at each closing second on a machine that is
# already on.
#
# Everything is written under $ALCOPA_DATA (set to /data in the image), so a
# single volume mount captures the watch list, the captures, the WAF token
# and any debug dumps.
set -e
cd /app
DATA="${ALCOPA_DATA:-/data}"
START_HOUR="${RUN_AT_UTC:-04:30}"
mkdir -p "$DATA"

while :; do
    DAY=$(date -u +%Y-%m-%d)
    echo "=== $(date -u +%H:%M:%SZ) starting $DAY"

    # Rebuild the watch list FIRST, every day, from the sale pages. Carrying
    # yesterday's over makes the watcher print "nothing closing inside the
    # horizon" and exit 0 — a silent no-op that reads as a healthy run. Sale
    # pages are also the only place a room lot's clock exists at all.
    python -u alcopa_scrape.py sales --horizon 1209600 \
           --out "$DATA/watchlist.json" || echo "!! sales failed, skipping the day"

    if [ -s "$DATA/watchlist.json" ]; then
        # One process for the whole day: sales minutes apart must share a
        # timeline, or the second sale's pre-close burst is slept through
        # while the first one's is still finishing.
        python -u alcopa_scrape.py watch \
               --lots "$DATA/watchlist.json" \
               --out "$DATA/prices-$DAY.jsonl" \
               --horizon 86400 --workers 24 || echo "!! watch exited non-zero"
    fi

    # Sleep to the next start. Computed in UTC on purpose: the scheduling
    # inside the watcher is epoch-based and timezone-independent, and this
    # keeps the daily boundary stable regardless of the container's TZ.
    NOW=$(date -u +%s)
    NEXT=$(date -u -d "today $START_HOUR" +%s 2>/dev/null || echo 0)
    [ "$NEXT" -le "$NOW" ] && NEXT=$(date -u -d "tomorrow $START_HOUR" +%s)
    echo "=== sleeping until $(date -u -d "@$NEXT" +'%Y-%m-%d %H:%M:%SZ')"
    sleep $((NEXT - NOW))
done
