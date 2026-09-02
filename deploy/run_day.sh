#!/bin/sh
# One auction day, start to finish. Runs once a day from cron; the watch
# command itself sleeps between sales, so a single process covers all of them.
#
# Alcopa strips a lot's price the second it sells, so the whole point of this
# container is to be awake at each closing second without anyone's desktop
# being involved.
set -e
cd /app
mkdir -p /app/data /data

# 1. Rebuild the watch list FIRST, every day, from the sale pages.
#    Baking it into the image was the old bug: the file went stale after a
#    day and every later run reported "nothing closing inside the horizon"
#    and exited 0 — a silent no-op that looks exactly like a healthy run.
#    Sale pages are also the only place a room lot's clock exists at all.
python alcopa_scrape.py sales --horizon 1209600 --out /data/watchlist.json

# 2. Sit on every sale closing in the next 24h. One process, not one per
#    sale: groups minutes apart must share a timeline or the second sale's
#    pre-close burst is slept through while the first one's finishes.
exec python alcopa_scrape.py watch \
     --lots /data/watchlist.json \
     --out "/data/prices-$(date -u +%Y-%m-%d).jsonl" \
     --horizon 86400 --workers 24
