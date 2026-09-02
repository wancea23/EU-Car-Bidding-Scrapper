#!/bin/bash
# Provision a fresh Linux box (Oracle Always Free, or any VPS) as the Alcopa
# deadline watcher. Idempotent: safe to re-run.
#
#   scp -i ~/.ssh/oracle_alcopa alcopa_scrape.py vpauto_scrape.py ubuntu@IP:~/
#   scp -i ~/.ssh/oracle_alcopa -r deploy ubuntu@IP:~/
#   ssh -i ~/.ssh/oracle_alcopa ubuntu@IP 'bash deploy/provision.sh <GEMINI_API_KEY>'
#
# Deliberately does NOT `git clone`: the GitHub copy predates the MINT_LEAD fix
# and still bakes a watch list into the image, so a clone would deploy a
# watcher that captures nothing and reports success.
set -euo pipefail

KEY="${1:?usage: provision.sh <GEMINI_API_KEY>}"
APP=/opt/alcopa

# ---------------------------------------------------------------- preflight
# The single question that decides whether this host is usable at all.
# 405 = AWS WAF captcha, which the solver handles.  403 = CloudFront has
# banned this IP outright and no amount of solving will help (that is what
# GitHub Actions' Azure ranges return). Fail loudly before installing 450 MB
# of Chromium onto a box that can never reach the site.
echo "== preflight: is this IP allowed to see the site at all?"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' \
       https://www.alcopa-auction.fr/robots.txt || echo 000)
echo "   HTTP $CODE"
case "$CODE" in
  405) echo "   OK - captcha challenge, solvable. Continuing." ;;
  200) echo "   OK - already unchallenged. Continuing." ;;
  403) echo "   FATAL: this IP is banned at the CDN edge. Destroy this box and"
       echo "   try another region or provider. Do not install anything here."
       exit 1 ;;
  *)   echo "   FATAL: unexpected $CODE - no network, or DNS is broken."; exit 1 ;;
esac

# ------------------------------------------------------------------- system
echo "== packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv curl >/dev/null

# Oracle's Always Free AMD shape has 1 GB of RAM and Chromium wants roughly
# that on its own. Swap is the difference between a working watcher and an
# OOM kill halfway through a sale. The ARM A1 shapes have plenty and skip this.
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
echo "== RAM: ${MEM_MB} MB"
if [ "$MEM_MB" -lt 2048 ] && [ ! -f /swapfile ]; then
  echo "   adding 4G swap (Chromium will not fit otherwise)"
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# Oracle images default to a locked-down iptables that drops outbound too
# rarely, but they DO ship a REJECT rule that breaks nothing here. Left alone
# on purpose: this box only makes outbound connections.

# ---------------------------------------------------------------- the app
echo "== app -> $APP"
sudo mkdir -p "$APP/data"
sudo cp ~/alcopa_scrape.py ~/vpauto_scrape.py "$APP/"
sudo cp -r ~/deploy "$APP/"
sudo chown -R "$USER":"$USER" "$APP"

echo "== python deps + chromium"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q playwright==1.48.0
"$APP/venv/bin/playwright" install --with-deps chromium

# The key never goes in the repo or the crontab line; root-only file instead.
printf 'GEMINI_API_KEY=%s\n' "$KEY" | sudo tee /etc/alcopa.env >/dev/null
sudo chmod 600 /etc/alcopa.env

# ------------------------------------------------------------------ daily run
# One process per auction day. `watch` sleeps between the day's sales itself,
# so this is a single 04:30 start, not a poll every N minutes.
cat > "$APP/run_day.sh" <<'RUN'
#!/bin/bash
set -euo pipefail
cd /opt/alcopa
set -a; . /etc/alcopa.env; set +a
export ALCOPA_BROWSER=playwright
DAY=$(date -u +%Y-%m-%d)
mkdir -p data
# Rebuild the watch list first, every day: a list left over from yesterday
# makes the watcher print "nothing closing" and exit 0, which reads as healthy.
./venv/bin/python -u alcopa_scrape.py sales --horizon 1209600 --out data/watchlist.json
exec ./venv/bin/python -u alcopa_scrape.py watch \
     --lots data/watchlist.json --out "data/prices-$DAY.jsonl" \
     --horizon 86400 --workers 24
RUN
chmod +x "$APP/run_day.sh"

( crontab -l 2>/dev/null | grep -v 'alcopa/run_day.sh' || true
  echo "30 4 * * * /opt/alcopa/run_day.sh >> /opt/alcopa/data/run.log 2>&1" ) | crontab -

echo
echo "== provisioned."
echo "   smoke test (solves one captcha, ~60s):"
echo "     cd $APP && set -a && . /etc/alcopa.env && set +a && \\"
echo "     ALCOPA_BROWSER=playwright ALCOPA_DEBUG=1 ./venv/bin/python -c \\"
echo "       'import alcopa_scrape as a; t=a.waf_token(force=True); print(\"token\", len(t))'"
echo "   then: crontab -l    # 04:30 daily"
