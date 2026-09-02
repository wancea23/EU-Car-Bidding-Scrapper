"""The Alcopa deadline watcher, as a Hugging Face Space.

Alcopa strips a lot's price the second it sells — the page survives, the number
does not — so the only chance to record what a car actually made is the last
seconds before its sale closes. That has to happen unattended, on a machine
that is not his desktop, which is what this Space is for.

Three things run here:

  * a scheduler thread that does one full auction day, then sleeps to the next;
  * a tiny HTTP server on 7860, because a Space with no listening port is
    marked as crashed — and because it doubles as the download endpoint and as
    the target for the uptime ping that stops a free Space pausing;
  * an optional push of each day's capture to a HF dataset repo, since a free
    Space has no persistent disk and a restart would otherwise take the data.
"""
from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

HOME = pathlib.Path(os.environ.get("HOME", "/home/user"))
REPO = HOME / "app" / "repo"
DATA = HOME / "app" / "data"
PORT = int(os.environ.get("PORT", 7860))
RUN_AT_UTC = dt.time(4, 30)          # rebuild the list, then sit on the day

STATE: dict = {"preflight": None, "phase": "starting", "day": None,
               "last_run": None, "next_run": None, "log": []}


def note(msg: str) -> None:
    line = f"{dt.datetime.utcnow():%Y-%m-%d %H:%M:%S}Z  {msg}"
    print(line, flush=True)
    STATE["log"] = (STATE["log"] + [line])[-200:]


# --------------------------------------------------------------- preflight
def preflight() -> int:
    """The one request that says whether this host can work at all.

    405 is the AWS WAF captcha, which the solver handles. 403 is CloudFront
    refusing the IP outright — that is what GitHub Actions' Azure ranges
    return, and no amount of solving fixes it. Checked at boot and shown on
    the status page, so a dead host is obvious in one glance instead of
    looking like a watcher that simply never captures anything.
    """
    req = urllib.request.Request(
        "https://www.alcopa-auction.fr/robots.txt",
        headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:                                   # noqa: BLE001
        return 0


# ------------------------------------------------------------------ the day
def run(args: list[str]) -> int:
    p = subprocess.run([sys.executable, "-u", *args], cwd=REPO,
                       env={**os.environ, "ALCOPA_BROWSER": "playwright"})
    return p.returncode


def publish(path: pathlib.Path) -> None:
    """Ship the capture off the Space, because the disk here is not durable.

    Free Spaces have ephemeral storage: a restart or a rebuild takes anything
    written locally, and a missed close cannot be re-scraped. Needs HF_TOKEN
    and HF_DATASET; without them the file only lives at /files until then.
    """
    token, repo_id = os.environ.get("HF_TOKEN"), os.environ.get("HF_DATASET")
    if not (token and repo_id and path.exists()):
        return
    try:
        from huggingface_hub import HfApi
        HfApi(token=token).upload_file(
            path_or_fileobj=str(path), path_in_repo=f"prices/{path.name}",
            repo_id=repo_id, repo_type="dataset")
        note(f"published {path.name} -> {repo_id}")
    except Exception as e:                              # noqa: BLE001
        note(f"publish FAILED ({str(e)[:120]}) — file still at /files/{path.name}")


def one_day() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.utcnow().strftime("%Y-%m-%d")
    STATE.update(day=day, phase="rebuilding watch list")

    # Rebuild from the SALE pages every day, before anything else. A room
    # sale's lots carry no clock of their own — it lives on the sale page —
    # and a list carried over from yesterday makes the watcher print
    # "nothing closing inside the horizon" and exit 0, which reads as healthy.
    if run(["alcopa_scrape.py", "sales", "--horizon", "1209600",
            "--out", str(DATA / "watchlist.json")]):
        note("sales failed — no watch list, skipping the day")
        STATE["phase"] = "sales failed"
        return

    out = DATA / f"prices-{day}.jsonl"
    STATE["phase"] = "watching"
    note(f"watching {day}")
    run(["alcopa_scrape.py", "watch", "--lots", str(DATA / "watchlist.json"),
         "--out", str(out), "--horizon", "86400", "--workers", "24"])
    STATE.update(phase="idle", last_run=day)
    publish(out)


def scheduler() -> None:
    # Pull first: a fix pushed to GitHub then reaches the watcher on a plain
    # restart, with no image rebuild.
    subprocess.run(["git", "pull", "--ff-only", "-q"], cwd=REPO)

    # Refuse to run pre-MINT_LEAD code. Without it the watcher mints its WAF
    # token *at* each burst instant; a solve costs ~55s, so the T-20s and T-5s
    # passes land ~35s AFTER the hammer and read lots whose price is already
    # stripped. It exits 0 throughout, so a stale checkout would look like a
    # healthy watcher that simply never finds anything worth recording.
    src = REPO / "alcopa_scrape.py"
    if not src.exists() or "MINT_LEAD" not in src.read_text(encoding="utf-8"):
        note("FATAL: checkout predates the MINT_LEAD fix — push it to GitHub first")
        STATE["phase"] = "stale code — push the MINT_LEAD fix to GitHub"
        return

    STATE["preflight"] = code = preflight()
    note(f"preflight HTTP {code} "
         f"({'captcha, solvable' if code in (200, 405) else 'BANNED — this host is unusable'})")
    if code not in (200, 405):
        STATE["phase"] = "unusable host"
        return                                   # keep serving so /  explains why

    # If the Space starts mid-morning, do not wait until tomorrow: the day's
    # sales are still ahead, and watch() simply skips any group already past.
    if dt.datetime.utcnow().time() < dt.time(19, 0):
        one_day()

    while True:
        now = dt.datetime.utcnow()
        nxt = dt.datetime.combine(now.date(), RUN_AT_UTC)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        STATE.update(next_run=nxt.isoformat(timespec="seconds") + "Z")
        note(f"sleeping until {STATE['next_run']}")
        time.sleep((nxt - now).total_seconds())
        try:
            one_day()
        except Exception as e:                          # noqa: BLE001
            note(f"day FAILED: {str(e)[:200]}")
            STATE["phase"] = "error"


# ------------------------------------------------------------------ the port
PAGE = """<!doctype html><meta charset=utf-8><title>Alcopa watcher</title>
<style>body{{font:14px/1.6 system-ui;margin:2rem;max-width:60rem}}
code,pre{{background:#f4f4f5;padding:.1rem .3rem;border-radius:3px}}
pre{{padding:.8rem;overflow:auto;max-height:26rem}}
.ok{{color:#15803d}}.bad{{color:#b91c1c;font-weight:600}}</style>
<h1>Alcopa deadline watcher</h1>
<p>Preflight: <span class="{cls}">HTTP {pf} — {pfmsg}</span></p>
<p>Phase: <b>{phase}</b> &middot; day: {day} &middot; last completed: {last}
   &middot; next run: {next}</p>
<h2>Captures</h2><ul>{files}</ul>
<h2>Log</h2><pre>{log}</pre>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):                    # keep the uptime ping quiet
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                             # noqa: N802
        if self.path.startswith("/health"):
            return self._send(b"ok", "text/plain")
        if self.path.startswith("/files/"):
            f = DATA / pathlib.PurePosixPath(self.path[7:]).name   # no traversal
            if not f.is_file():
                return self._send(b"not found", "text/plain", 404)
            return self._send(f.read_bytes(), "application/x-ndjson")
        pf = STATE["preflight"]
        files = "".join(
            f'<li><a href="/files/{p.name}">{p.name}</a> '
            f'({p.stat().st_size/1024:.0f} KB)</li>'
            for p in sorted(DATA.glob("*.jsonl"))) or "<li>none yet</li>"
        self._send(PAGE.format(
            pf=pf, cls="ok" if pf in (200, 405) else "bad",
            pfmsg=("captcha challenge, solvable" if pf in (200, 405)
                   else "CloudFront has banned this IP — this host cannot work"),
            phase=STATE["phase"], day=STATE["day"] or "-",
            last=STATE["last_run"] or "-", next=STATE["next_run"] or "-",
            files=files, log="\n".join(STATE["log"][-80:]) or "(nothing yet)"
        ).encode())


if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as srv:
        note(f"listening on {PORT}")
        srv.serve_forever()
