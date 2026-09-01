"""Alcopa Auction scraper — talks to the site directly, through the AWS WAF.

This replaces the clipboard workaround in alcopa_clip_watch.py. That file exists
because alcopa-auction.fr sits behind AWS WAF and nothing server-side could get
in; you had to browse and Ctrl+C. The WAF is passable: its CAPTCHA is a 3x3
image grid ("choose all the curtains"), a vision model reads it, and solving it
mints an `aws-waf-token` cookie that plain HTTP requests carry for exactly 300
seconds. So one browser solve buys a five-minute window of ordinary scraping.

    python alcopa_scrape.py discover              # sitemap -> every lot URL
    python alcopa_scrape.py harvest --limit 50    # full detail into the DB
    python alcopa_scrape.py watch                 # poll lots as their sale closes

WHY `watch` MATTERS AND IS NOT OPTIONAL
A live lot carries data-current-price and data-ts (auction end, unix epoch).
The moment it sells, Alcopa strips BOTH: the sold page keeps its photos and
specs but shows no price anywhere and no countdown, just a VANDUT badge. The
price therefore exists only in the window before the hammer, and is not
recoverable afterwards from any page. Every lot in a sale shares one identical
data-ts, so they all close in the same second and must be polled in parallel —
sequential polling would smear the capture across the only minute that counts.

Everything lands in the same data/vpauto.db as the VPauto lots, tagged
source='alcopa', with Moldovan excise and landed cost from the shared helpers.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vpauto_scrape import (  # noqa: E402
    db, excise_eur, landed_eur, red_flags,
)

HERE = Path(__file__).parent
DATA = HERE / "data"
BASE = "https://www.alcopa-auction.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

TOKEN_PATH = DATA / "alcopa_token.txt"
# AWS WAF's default CAPTCHA immunity is 300s; measured on this site at
# 287s alive / 307s dead. Refresh early so a burst never straddles expiry.
TOKEN_TTL = 270
SESSION = "alcopa"

GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/openai/"
              "chat/completions")
GEMINI_MODEL = "gemini-3.6-flash"


# ------------------------------------------------------------------ WAF token
def _gemini_keys() -> list[str]:
    env = os.environ.get("GEMINI_API_KEY")
    if env:
        return [env.strip()]
    kf = Path(os.environ["LOCALAPPDATA"]) / "gem-pool" / "aistudio-key.txt"
    return [ln.split()[0] for ln in kf.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _browser_exe() -> str:
    """agent-browser is an npm shim; on Windows only the .cmd is executable."""
    import shutil
    for name in ("agent-browser.cmd", "agent-browser.exe", "agent-browser"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("agent-browser not on PATH — npm i -g agent-browser")


def _ab(*args: str, timeout: int = 120) -> str:
    """One agent-browser call against our own session."""
    out = subprocess.run([_browser_exe(), "--session", SESSION, *args],
                         capture_output=True, text=True, timeout=timeout)
    return (out.stdout or "").strip()


def _eval(js: str) -> str:
    out = subprocess.run([_browser_exe(), "--session", SESSION, "eval", "--stdin"],
                         input=js, capture_output=True, text=True, timeout=120)
    return (out.stdout or "").strip().strip('"').replace('\\"', '"')


def _solve_grid(instruction: str, png: Path) -> list[int]:
    """Ask a vision model which of the 9 tiles match. Returns tile numbers."""
    import base64
    img = base64.b64encode(png.read_bytes()).decode()
    prompt = ("3x3 grid, tiles numbered 1-9 in reading order (1,2,3 top row; "
              "4,5,6 middle; 7,8,9 bottom). Instruction: "
              f"'{instruction}'. Return ONLY a JSON array of matching tile "
              "numbers, e.g. [1,4,7]. No prose.")
    body = json.dumps({
        "model": GEMINI_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + img}}]}],
        "max_tokens": 2000}).encode()
    for key in _gemini_keys():
        for attempt in range(3):
            req = urllib.request.Request(
                GEMINI_URL, data=body,
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"})
            try:
                r = json.load(urllib.request.urlopen(req, timeout=90))
            except Exception:
                time.sleep(2 * (attempt + 1))  # 503s here are common and transient
                continue
            txt = (r["choices"][0]["message"].get("content") or "")
            m = re.search(r"\[[\d,\s]*\]", txt)
            if m:
                return json.loads(m.group(0))
        # this key is unhappy (quota, or a run of 503s) — try the next one
    return []


TILE_CLICK_JS = """(want) => {
  const w = new Set(want.map(String));
  const b = [...document.querySelectorAll('button')]
      .filter(x => /^[1-9]$/.test((x.textContent || '').trim()));
  let n = 0;
  for (const x of b) if (w.has(x.textContent.trim())) { x.click(); n++; }
  return n;
}"""


def _mint_playwright() -> str | None:
    """Same captcha flow, but through Playwright instead of agent-browser.

    agent-browser is a local dev tool; this path is what lets the watcher run
    on a server, in CI, or in a container, where the only browser is the one
    we install ourselves. Headless by default — the WAF's silent challenge
    does not care, because we solve the visible captcha either way.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    png = DATA / "_captcha.png"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=os.environ.get("ALCOPA_HEADFUL") != "1",
            args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(user_agent=UA, locale="fr-FR",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(BASE + "/", timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            btn = page.query_selector("#amzn-captcha-verify-button")
            if btn:
                btn.click()
                page.wait_for_timeout(3000)
            instr = page.evaluate(
                "() => (document.body.innerText.match(/Choose all[^\\n]*/)||['?'])[0]")
            canvas = page.query_selector("canvas")
            if not canvas:
                return None
            canvas.screenshot(path=str(png))
            tiles = _solve_grid(instr, png)
            if not tiles:
                return None
            page.evaluate(TILE_CLICK_JS, tiles)
            page.click("#amzn-btn-verify-internal")
            page.wait_for_timeout(5000)
            for c in ctx.cookies():
                if c["name"] == "aws-waf-token" and len(c["value"]) > 100:
                    return c["value"]
            return None
        finally:
            ctx.close()
            browser.close()


def _mint() -> str | None:
    """Drive a real browser through the WAF captcha and come back with a token."""
    # Prefer Playwright wherever it exists: it is the only path that also works
    # off this machine. agent-browser stays as the local fallback.
    if os.environ.get("ALCOPA_BROWSER", "playwright") == "playwright":
        try:
            tok = _mint_playwright()
            if tok:
                return tok
        except Exception as e:                     # noqa: BLE001
            print(f"  [waf] playwright mint failed: {str(e)[:120]}")
    _ab("open", BASE + "/", timeout=180)
    time.sleep(4)
    _ab("click", "#amzn-captcha-verify-button")
    time.sleep(3)
    instr = _eval('(() => (document.body.innerText.match'
                  '(/Choose all[^\\n]*/)||["?"])[0])()')
    png = DATA / "_captcha.png"
    _ab("screenshot", "canvas", str(png))
    tiles = _solve_grid(instr, png)
    if not tiles:
        return None
    want = ",".join(f'"{int(t)}"' for t in tiles)
    # The nine tiles are 0x0 screen-reader buttons layered over one <canvas>.
    # A plain .click() on them registers selection — no coordinates needed.
    _eval(f'(() => {{ const w=new Set([{want}]); '
          'const b=[...document.querySelectorAll("button")]'
          '.filter(x=>/^[1-9]$/.test((x.textContent||"").trim())); '
          'let n=0; for(const x of b) if(w.has(x.textContent.trim()))'
          '{x.click();n++;} return JSON.stringify({clicked:n}); })()')
    _ab("click", "#amzn-btn-verify-internal")
    time.sleep(5)
    tok = _eval('(() => { const m=/aws-waf-token=([^;"]+)/.exec(document.cookie);'
                ' return m?m[1]:""; })()')
    return tok if tok and len(tok) > 100 else None


def _token_alive(tok: str) -> bool:
    try:
        req = urllib.request.Request(
            BASE + "/robots.txt",
            headers={"User-Agent": UA, "Cookie": f"aws-waf-token={tok}"})
        return urllib.request.urlopen(req, timeout=20).status == 200
    except Exception:
        return False


_tok_cache: dict = {"tok": None, "at": 0.0}
_tok_lock = __import__("threading").Lock()


def waf_token(force: bool = False, min_remaining: int = 0) -> str:
    """A currently-valid token, minting only when the cached one is stale.

    Serialised: without the lock every worker thread would notice the same
    expiry at the same instant and each drive its own browser through the
    captcha, burning vision quota and fighting over one browser session.

    `min_remaining` re-mints early when less than that many seconds are left.
    A burst launched with 5s of token life will expire mid-flight, and every
    worker then blocks behind a ~25s captcha solve — through the exact second
    the auction closes, which is the only second that matters.
    """
    with _tok_lock:
        if (min_remaining and _tok_cache["tok"]
                and time.time() - _tok_cache["at"] > TOKEN_TTL - min_remaining):
            force = True
        return _waf_token_locked(force)


def _waf_token_locked(force: bool) -> str:
    now = time.time()
    # A forcing caller that queued behind another thread's mint does not need
    # its own: anything this fresh was minted while we waited for the lock.
    if force and _tok_cache["tok"] and now - _tok_cache["at"] < 30:
        return _tok_cache["tok"]
    if not force and _tok_cache["tok"] and now - _tok_cache["at"] < TOKEN_TTL:
        return _tok_cache["tok"]
    if not force and TOKEN_PATH.exists():
        disk = TOKEN_PATH.read_text().strip()
        if disk and time.time() - TOKEN_PATH.stat().st_mtime < TOKEN_TTL \
                and _token_alive(disk):
            _tok_cache.update(tok=disk, at=TOKEN_PATH.stat().st_mtime)
            return disk
    for attempt in range(4):
        tok = _mint()
        if tok and _token_alive(tok):
            DATA.mkdir(parents=True, exist_ok=True)
            TOKEN_PATH.write_text(tok)
            _tok_cache.update(tok=tok, at=time.time())
            print(f"  [waf] token minted (attempt {attempt + 1})")
            return tok
        print(f"  [waf] mint failed (attempt {attempt + 1})")
    raise RuntimeError("could not mint a WAF token")


def fetch(url: str, retries: int = 2) -> str:
    """GET through the WAF. A 405 means the token died mid-run; re-mint once."""
    if not url.startswith("http"):
        url = BASE + url
    for attempt in range(retries + 1):
        tok = waf_token(force=attempt > 0)
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Cookie": f"aws-waf-token={tok}"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body = r.read().decode("utf-8", "replace")
            if "Human Verification" not in body[:2500]:
                return body
        except Exception:
            if attempt == retries:
                raise
        time.sleep(1)
    return ""


# ------------------------------------------------------------------- parsing
SPEC_LABELS = {
    "marca": "make", "marque": "make", "make": "make",
    "model": "model", "modele": "model", "modèle": "model",
    "finisaje": "trim", "finition": "trim", "finish": "trim",
    "inmatriculare": "plate", "immatriculation": "plate", "registration": "plate",
    "combustibil": "fuel", "energie": "fuel", "fuel": "fuel",
    "punere in circulatie": "first_reg", "mise en circulation": "first_reg",
    "kilometraj": "km", "kilometrage": "km", "mileage": "km",
    "numar de serie": "vin", "numero de serie": "vin", "serial number": "vin",
    "culoare": "colour", "couleur": "colour", "colour": "colour",
    "tva recuperabil": "tva_recup", "tva recuperable": "tva_recup",
    "tip": "body_type", "type": "body_type",
    "caroserie": "body", "carrosserie": "body",
    "loc de depozitare": "location", "lieu de stockage": "location",
    "co2": "co2",
    "engine capacity": "cc", "cylindree": "cc", "cilindree": "cc",
    "cylindree moteur": "cc", "capacitate motor": "cc",
    "gearbox": "gearbox", "boite de vitesse": "gearbox",
    "cutie de viteza": "gearbox", "boîte de vitesse": "gearbox",
}


def _spec_key(label: str) -> str | None:
    """Map a table label to a field. Alcopa varies the wording per locale
    ("Cylindree" vs "Cylindree moteur"), so fall back to containment —
    longest key first, or "type" would shadow "type de boite".
    """
    lab = _deaccent(html.unescape(label).strip().lower())
    if lab in SPEC_LABELS:
        return SPEC_LABELS[lab]
    for k in sorted(SPEC_LABELS, key=len, reverse=True):
        if k in lab:
            return SPEC_LABELS[k]
    return None


# Alcopa reports energy as two-letter codes. The shared excise helpers key off
# VPauto's French words — _band() literally tests fuel.startswith("diesel") and
# the exemption tests for "lectri" — so an unmapped "GO" is silently banded as
# PETROL and "EL" is charged excise instead of being exempt. Normalise here,
# once, before anything computes money.
FUEL_MAP = {
    "GO": "Diesel",                 # gazole
    "ES": "Essence",
    "EH": "Essence Hybride",        # essence + electric
    "EE": "Essence Hybride",
    "GH": "Diesel Hybride",
    "GE": "Diesel Hybride",
    "EL": "Electricite",            # exempt, cc is 0 on these
    "GP": "GPL", "GN": "GNV",
    "XX": None, "NC": None,         # unknown — better null than a wrong band
}


def norm_fuel(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().upper()
    if c in FUEL_MAP:
        return FUEL_MAP[c]
    return code if len(code) > 3 else None   # already a word, or unrecognised


def _deaccent(s: str) -> str:
    for a, b in (("ăâà", "a"), ("îï", "i"), ("șş", "s"), ("țţ", "t"),
                 ("éèê", "e"), ("ô", "o"), ("û", "u"), ("ç", "c")):
        for ch in a:
            s = s.replace(ch, b)
    return s


def _num(s) -> int | None:
    if s is None:
        return None
    d = re.sub(r"[^\d]", "", str(s))
    return int(d) if d else None


def parse_lot(page: str, url: str) -> dict:
    """Everything the detail page carries: specs, photos, damage, price, clock."""
    txt = re.sub(r"<script.*?</script>", " ", page, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", html.unescape(txt))

    rec: dict = {"url": url}

    # --- price + clock. Both vanish the instant the lot sells, which is the
    # whole reason this scraper polls instead of visiting once.
    m = re.search(r'data-current-price="(\d+)"', page)
    rec["current_price"] = int(m.group(1)) if m else None
    m = re.search(r'data-ts="(\d+)"', page)
    rec["ends_ts"] = int(m.group(1)) if m else None
    m = re.search(r"(?:Mise . prix|Pre. de pornire|Starting price)\s*:?\s*"
                  r"([\d][\d  .]*)", txt)
    rec["mise_a_prix"] = _num(m.group(1)) if m else None

    # "Enchere courante" (current bid) vs "Mise a prix" (opening price) is the
    # difference between a lot being bid on RIGHT NOW and one merely priced.
    # Room sales show the former and carry no data-ts at all — their clock
    # lives on the sale page, not the lot page — so keying "live" off ends_ts
    # marked every room lot as stateless and hid that they are biddable.
    rec["live_bid"] = bool(re.search(
        r"ench[eè]re\s+courante|current\s+bid|licita[țt]ia\s+curent", txt, re.I))

    # Only trust a sold badge, never the word loose in the page: sellers write
    # "vendu en l'etat" / "vendu sans controle technique" in the notes of LIVE
    # lots, and a false 'adjuge' drops the lot out of watch() for good — the
    # one failure that loses the price permanently.
    sold = bool(re.search(
        r'class="[^"]*(?:badge|tag|label|vendu|sold)[^"]*"[^>]*>\s*'
        r'(?:VENDU|V[ÂA]NDUT|SOLD)\b', page, re.I))
    if not sold and rec["ends_ts"] is None:
        # no clock left and the word present as its own line => really sold
        sold = bool(re.search(r"(?:^|\|)\s*(?:VENDU|V[ÂA]NDUT|SOLD)\s*(?:\||$)",
                              txt, re.I | re.M))
    # A third state, between "in preparation" and "live": the car is fully
    # photographed and specced but Alcopa has not set an opening price yet
    # ("Prix de depart bientot disponible"). It carries no price and no clock,
    # so without this it looks identical to a scrape failure — and would never
    # be revisited once the price appears.
    rec["price_pending"] = bool(re.search(
        r"mise\s+[àa]\s+prix\s+bient[ôo]t\s+disponible|"
        r"starting\s+price\s+available\s+soon|"
        r"pre[țt]\s+de\s+pornire\s+disponibil", txt, re.I)
    ) and rec["current_price"] is None

    if sold:
        rec["sale_state"] = "adjuge"
    elif rec["price_pending"]:
        rec["sale_state"] = "pending"
    elif rec["live_bid"]:
        rec["sale_state"] = "en_cours"
    elif rec["ends_ts"] and rec["ends_ts"] > time.time():
        rec["sale_state"] = "en_cours"
    elif rec["ends_ts"]:
        rec["sale_state"] = "termine"
    else:
        rec["sale_state"] = None

    # fees ride differently per sale channel: web sales quote fees included,
    # LIVE sales quote them on top. Same number means different money.
    if re.search(r"Frais inclus|Comision de v[âa]nzare inclus|fees included", txt, re.I):
        rec["fees"] = "inclus"
    elif re.search(r"Frais en sus|fees in addition", txt, re.I):
        rec["fees"] = "en_sus"
    else:
        rec["fees"] = None

    # --- spec table
    specs: dict = {}
    for lab, val in re.findall(
            r"<t[dh][^>]*>\s*([^<>]{2,40}?)\s*</t[dh]>\s*<t[dh][^>]*>\s*"
            r"([^<>]{1,60}?)\s*</t[dh]>", page):
        key = _spec_key(lab)
        if key and key not in specs:
            specs[key] = html.unescape(val).strip()
    rec["specs"] = specs
    rec["km"] = _num(specs.get("km"))
    rec["cc"] = _num(specs.get("cc"))
    rec["vin"] = specs.get("vin")
    rec["location"] = specs.get("location")
    rec["gearbox"] = specs.get("gearbox")
    rec["fuel_code"] = specs.get("fuel")
    rec["fuel"] = norm_fuel(specs.get("fuel"))
    rec["colour"] = specs.get("colour")
    rec["co2"] = _num(specs.get("co2"))
    rec["tva_recup"] = 1 if (specs.get("tva_recup") or "").upper().startswith(
        ("Y", "O", "D")) else 0
    fr = specs.get("first_reg") or ""
    m = re.search(r"([0-3]?\d/[01]?\d/(?:19|20)\d\d)", fr)
    rec["first_reg"] = m.group(1) if m else (fr or None)
    m = re.search(r"(?:19|20)\d\d", fr)
    rec["year"] = int(m.group(0)) if m else None

    title = re.search(r"<title>(.*?)</title>", page, re.S)
    rec["title"] = " ".join(
        (specs.get("make", ""), specs.get("model", ""), specs.get("trim", ""))
    ).strip() or (html.unescape(title.group(1)).strip() if title else None)

    # --- photos, in Alcopa's own order.
    # Never sort these: the filenames are hashes, so alphabetical order is
    # random and the car's main exterior shot stops being first. And the
    # /damages/ close-ups belong to the defect panel, not the gallery —
    # leaving them in floods it with engine bays and door edges.
    # JSON embeds the URLs with escaped slashes (\/damages\/), so unescape the
    # backslashes too or the /damages/ test below silently never fires.
    flat = html.unescape(page).replace("\\/", "/")
    # Renditions, per family, for one lot: internalCropped ~14, big ~7,
    # small ~7. The page's own gallery is built from internalCropped and the
    # rest ("Voir 35 autres photos") is fetched by JS, so it is NOT in this
    # HTML at all — keeping only /big/ threw away half of what we do get.
    # Take internalCropped first, then any /big/ shot it does not already
    # cover, matching on the shared filename.
    # One photo appears under several size folders, and the folder names are
    # NOT stable between lots: /big/, /small/, /cropped/, /internalCropped/
    # have all been seen. Keying on two hard-coded folders silently dropped
    # whole lots' worth of pictures. Instead: walk every URL in document order
    # (which is the order Alcopa's own thumbnail strip uses), group by
    # FILENAME, and show the largest rendition each photo has.
    RANK = {"big": 3, "cropped": 2, "internalCropped": 2, "small": 1}
    order: list[str] = []
    best: dict[str, tuple[int, str]] = {}
    for u in re.findall(
            r"https://photos\.static\.alcopa-auction\.net/[^\"'\\ ]+?\.jpg", flat):
        if "/damages/" in u:
            continue                      # those belong to the defect panel
        fam = re.sub(r"^.*?/photos/\d+/\d+/", "", u).split("/")[0]
        stem = u.rsplit("/", 1)[-1]
        rank = RANK.get(fam, 0)
        if stem not in best:
            order.append(stem)
            best[stem] = (rank, u)
        elif rank > best[stem][0]:
            best[stem] = (rank, u)
    gallery = [best[s][1] for s in order]
    # the record flagged is_main is the hero shot wherever it happens to sit,
    # and the two JSON keys appear in either order
    main = (re.search(r'"is_main"\s*:\s*1.{0,400}?"vehiculephoto_url"\s*:\s*"([^"]+)"',
                      flat, re.S)
            or re.search(r'"vehiculephoto_url"\s*:\s*"([^"]+)".{0,400}?"is_main"\s*:\s*1',
                         flat, re.S))
    if main:
        hero = main.group(1)
        if hero in gallery:
            gallery.insert(0, gallery.pop(gallery.index(hero)))
    rec["photos"] = gallery

    # --- damage: a JSON blob whose `zone` codes are the ids of the inline SVG,
    # so the diagram can be redrawn locally and stay clickable.
    dmg = []
    for m in re.finditer(
            r'"damage"\s*:\s*\{(?P<d>[^{}]*)\}\s*,\s*"vehiculephoto_url"\s*:\s*'
            r'"(?P<u>[^"]+)"', html.unescape(page)):
        # Decode as JSON rather than slicing strings out: the labels are full
        # of è escapes ("Aile arrière droite") that would otherwise
        # be stored and rendered literally.
        try:
            d = json.loads("{" + m.group("d") + "}")
        except ValueError:
            continue
        zone = d.get("zone")
        if zone:
            dmg.append({"zone": zone, "zone_label": d.get("zone_label"),
                        "type": d.get("type"), "type_label": d.get("type_label"),
                        "photo": m.group("u").replace("\\/", "/")})
    rec["damage"] = dmg

    # The condition diagram is two views of the car (near side / off side), so
    # keep every svg — taking only the first loses half the damage zones.
    svgs = re.findall(r"<svg[^>]*>.*?</svg>", page, re.S)
    rec["svg"] = "\n".join(svgs) if svgs else None

    m = re.search(r"(?:Informations|Informa[țt]ii|Comentarii|Commentaires)\s*(.{0,400})", txt)
    rec["observations"] = m.group(1).strip() if m else None
    return rec


# ------------------------------------------------------------------ storage
EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS damage (
    lot_id TEXT, zone TEXT, zone_label TEXT, dtype TEXT, type_label TEXT,
    photo TEXT,
    PRIMARY KEY (lot_id, photo)
);
CREATE TABLE IF NOT EXISTS lot_svg (lot_id TEXT PRIMARY KEY, svg TEXT);
CREATE INDEX IF NOT EXISTS idx_damage_lot ON damage(lot_id);
"""


def con_with_extras() -> sqlite3.Connection:
    con = db()
    # A multi-hour harvest writes continuously while serve_cars.py reads the
    # same file; in the default journal mode those readers hit "database is
    # locked". WAL lets them run concurrently. It is a persistent property of
    # the file, so setting it once here fixes the server too.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(EXTRA_SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(lots)")}
    for col, decl in (("ends_ts", "INTEGER"), ("fees", "TEXT"), ("vin", "TEXT"),
                      ("colour", "TEXT"), ("co2", "INTEGER")):
        if col not in have:
            con.execute(f"ALTER TABLE lots ADD COLUMN {col} {decl}")
    con.commit()
    return con


def lot_id_for(url: str) -> str:
    m = re.search(r"-(\d+)(?:\.html)?/?$", url.rstrip("/"))
    return f"alcopa:{m.group(1)}" if m else "alcopa:" + re.sub(
        r"\W+", "", url)[-24:]


def save_lot(con: sqlite3.Connection, rec: dict, full: bool = True) -> str:
    lot_id = lot_id_for(rec["url"])
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    price = rec.get("current_price")
    state = rec.get("sale_state")
    con.execute("INSERT OR IGNORE INTO lots (lot_id, url) VALUES (?, ?)",
                (lot_id, rec["url"] if rec["url"].startswith("http")
                 else BASE + rec["url"]))
    if full:
        first_reg = rec.get("first_reg")
        fuel = rec.get("fuel") or ""
        exc = excise_eur(rec.get("cc"), fuel, first_reg)
        land = landed_eur(price, rec.get("cc"), fuel, first_reg) if price else None
        con.execute(
            "UPDATE lots SET source='alcopa', title=?, first_reg=?, km=?, cc=?, "
            "fuel=?, gearbox=?, location=?, tva_recup=?, observations=?, "
            "red_flags=?, photos=?, photo_count=?, excise_eur=?, landed_eur=?, "
            "card_year=?, card_km=?, scraped_at=?, ends_ts=?, fees=?, vin=?, "
            "colour=?, co2=?, body=? WHERE lot_id=?",
            (rec.get("title"), first_reg, rec.get("km"), rec.get("cc"), fuel or None,
             rec.get("gearbox"), rec.get("location"), rec.get("tva_recup"),
             rec.get("observations"), red_flags(rec.get("observations"), None),
             "\n".join(rec.get("photos") or []), len(rec.get("photos") or []),
             exc, land, rec.get("year"), rec.get("km"), ts,
             rec.get("ends_ts"), rec.get("fees"), rec.get("vin"),
             rec.get("colour"), rec.get("co2"),
             rec.get("specs", {}).get("body"), lot_id))
        for d in rec.get("damage") or []:
            con.execute("INSERT OR IGNORE INTO damage VALUES (?,?,?,?,?,?)",
                        (lot_id, d["zone"], d["zone_label"], d["type"],
                         d["type_label"], d["photo"]))
        if rec.get("svg"):
            con.execute("INSERT OR REPLACE INTO lot_svg VALUES (?,?)",
                        (lot_id, rec["svg"]))

    # sale_price only becomes meaningful once the lot is marked sold, and by
    # then the page no longer shows a number — so it is the last price we saw.
    # Prefer a price still on this very page (a lot can be found already sold
    # while the figure lingers); fall back to the newest logged price, then to
    # the opening bid. Reading price_log alone left sale_price NULL for every
    # lot first seen in the sold state.
    if state == "adjuge":
        last = con.execute(
            "SELECT price FROM price_log WHERE lot_id=? AND price IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (lot_id,)).fetchone()
        final = price or (last[0] if last else None) or rec.get("mise_a_prix")
        con.execute("UPDATE lots SET sale_state=?, last_seen=?, "
                    "sale_price=COALESCE(sale_price, ?) WHERE lot_id=?",
                    (state, ts, final, lot_id))
    else:
        con.execute("UPDATE lots SET sale_state=?, current_bid=?, last_seen=?, "
                    "mise_a_prix=COALESCE(mise_a_prix, ?) WHERE lot_id=?",
                    (state, price, ts, rec.get("mise_a_prix") or price, lot_id))
    if price is not None:
        # price_log's key is (lot_id, ts); at one-second resolution two polls
        # inside the same second collide and INSERT OR IGNORE drops the later
        # one — precisely what happens during the closing burst. Log with
        # milliseconds so no observation is silently discarded.
        ts_ms = f"{ts}.{int(time.time() * 1000) % 1000:03d}"
        con.execute("INSERT OR IGNORE INTO price_log VALUES (?,?,?,?)",
                    (lot_id, ts_ms, state, price))
    con.commit()
    return lot_id


# ------------------------------------------------------------------ commands
def cmd_discover(args) -> None:
    idx = fetch("/sitemap.xml")
    maps = re.findall(r"<loc>([^<]+)</loc>", idx)
    urls: list[str] = []
    for sm in maps:
        if "items-fr" not in sm:
            continue
        body = fetch(sm)
        urls += [u for u in re.findall(r"<loc>([^<]+)</loc>", body)
                 if re.search(r"/(voiture|utilitaire)-occasion/", u)]
    urls = sorted(set(urls))
    out = DATA / "alcopa_lots.txt"
    out.write_text("\n".join(urls), encoding="utf-8")
    print(f"{len(urls)} lot URLs -> {out}")


def _harvest_one(url: str) -> dict | None:
    try:
        return parse_lot(fetch(url), url)
    except Exception as e:
        print(f"  ! {url[-50:]}: {e}")
        return None


def cmd_harvest(args) -> None:
    src = DATA / "alcopa_lots.txt"
    if not src.exists():
        sys.exit("run `discover` first")
    urls = [u for u in src.read_text(encoding="utf-8").split() if u]
    if args.limit:
        urls = urls[:args.limit]
    waf_token()
    con = con_with_extras()
    n = 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for rec in ex.map(_harvest_one, urls):
            if not rec:
                continue
            save_lot(con, rec)
            n += 1
            if n % 25 == 0:
                print(f"  {n}/{len(urls)}")
    con.close()
    print(f"harvested {n} lots")


def cmd_watch(args) -> None:
    """Poll every lot closing soon, all at once, right up to the hammer.

    Lots in a sale share one closing second, so this fires the whole set in
    parallel rather than walking them; a sequential pass would spread the
    capture over the only seconds that carry the final price.
    """
    now = int(time.time())
    con = None
    if args.lots:
        # Portable mode: the watch list travels as a small JSON file, so this
        # can run on a server that has no copy of the database. Observations
        # go to a JSONL that gets merged back later.
        rows = [(r["lot_id"], r["url"], int(r["ends_ts"]))
                for r in json.loads(Path(args.lots).read_text(encoding="utf-8"))
                if now - 600 < int(r["ends_ts"]) < now + args.horizon]
    else:
        con = con_with_extras()
        rows = con.execute(
            "SELECT lot_id, url, ends_ts FROM lots WHERE source='alcopa' "
            "AND ends_ts IS NOT NULL AND ends_ts > ? AND ends_ts < ? "
            "AND COALESCE(sale_state,'') != 'adjuge'",
            (now - 600, now + args.horizon)).fetchall()
    if not rows:
        print("nothing closing inside the horizon")
        return

    # Lots inside ONE sale share a closing second, but several sales can fall
    # inside the horizon at different times. Group by close time and give each
    # group its own burst schedule — keying off a single max() would let the
    # earlier sale close unobserved.
    groups: dict[int, list] = {}
    for r in rows:
        groups.setdefault(r[2], []).append(r)
    print(f"{len(rows)} lots across {len(groups)} closing time(s)")
    for end in sorted(groups):
        print(f"  {len(groups[end])} lots @ "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end))}")

    # dense near the close, then one pass after it to read the VANDUT badge
    offsets = [-300, -120, -60, -20, -5, +90]

    # One global timeline rather than a loop per group. Handling groups in
    # sequence means a group's +90s pass blocks the next one: two sales a
    # minute apart, and the second sale's whole pre-close burst is slept
    # through and its prices lost for good.
    schedule = sorted((end + off, end, off)
                      for end in groups for off in offsets)
    now = time.time()
    for at, end, off in schedule:
        if at < now - 30 and off < -20:
            continue                      # that moment is already gone
        wait = at - time.time()
        if wait > 0:
            print(f"  sleeping {wait:.0f}s -> "
                  f"{time.strftime('%H:%M:%S', time.localtime(end))} T{off:+d}s")
            time.sleep(wait)
        urls = [r[1] for r in groups[end]]
        # never start a burst on a token about to expire
        waf_token(min_remaining=90)
        t0 = time.time()
        with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            recs = list(ex.map(_harvest_one, urls))
        got = 0
        for rec, (lid, lurl, _e) in zip(recs, groups[end]):
            if not rec:
                continue
            got += 1
            if con is not None:
                save_lot(con, rec, full=False)
            if args.out:
                with open(args.out, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "lot_id": lid, "url": lurl, "ends_ts": end, "offset": off,
                        "observed_at": time.time(),
                        "price": rec.get("current_price"),
                        "state": rec.get("sale_state"),
                        "fees": rec.get("fees"),
                    }, ensure_ascii=False) + "\n")
        print(f"  {time.strftime('%H:%M:%S', time.localtime(end))} T{off:+d}s  "
              f"{got}/{len(urls)} lots in {time.time() - t0:.1f}s")
    if con is not None:
        con.close()


def cmd_refresh(args) -> None:
    """Re-fetch lots that were incomplete when first seen.

    Alcopa lists cars weeks before their sale in an "En preparation" state:
    the page exists but carries a placeholder instead of photos, and often no
    price or clock. Harvesting once would freeze them that way forever, so
    revisit anything still missing photos or a closing time. Lots already sold
    are skipped — their pages never regain what was stripped.
    """
    con = con_with_extras()
    rows = con.execute(
        "SELECT lot_id, url FROM lots WHERE source='alcopa' "
        "AND COALESCE(sale_state,'') != 'adjuge' "
        "AND (COALESCE(photo_count,0) = 0 OR ends_ts IS NULL "
        "     OR cc IS NULL OR vin IS NULL "
        "     OR sale_state = 'pending' "
        "     OR (current_bid IS NULL AND mise_a_prix IS NULL)) "
        "ORDER BY COALESCE(ends_ts, 9e18)").fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} incomplete lots to revisit")
    waf_token()
    filled = 0
    with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, rec in enumerate(ex.map(_harvest_one, [r[1] for r in rows]), 1):
            if not rec:
                continue
            save_lot(con, rec)
            if rec.get("photos"):
                filled += 1
            if i % 25 == 0:
                print(f"  {i}/{len(rows)}  (with photos now: {filled})")
    con.close()
    print(f"revisited {len(rows)}, now carrying photos: {filled}")


SALE_RE = re.compile(r"/(?:vente-encheres-en-ligne|salle-de-vente-encheres)/"
                     r"[a-z0-9-]*/?(\d+)$")


def cmd_sales(args) -> None:
    """Build the watch list from the SALE pages, not the lot pages.

    Two reasons this is the right source:
      * A room sale's lots carry no data-ts of their own — the clock lives on
        the sale page. Harvesting lot pages therefore leaves every 'frais en
        sus' lot with no known deadline, which is most of the catalogue.
      * One sale page lists 20 lots, so ~70 fetches covers a 1 400-lot sale.
        Reading each lot page instead would be 1 400.

    Output is the same portable JSON the cloud watcher consumes, so this can
    run unattended and keep itself current without any local database.
    """
    idx = fetch("/sitemap.xml")
    sales_map = [u for u in re.findall(r"<loc>([^<]+)</loc>", idx) if "sales" in u]
    sale_urls: list[str] = []
    for sm in sales_map:
        for u in re.findall(r"<loc>([^<]+)</loc>", fetch(sm)):
            # keep the French canonical form only; the other locales repeat it
            if re.search(r"alcopa-auction\.fr/(?:vente|salle)", u) and SALE_RE.search(u):
                sale_urls.append(u)
    sale_urls = sorted(set(sale_urls))
    print(f"{len(sale_urls)} sales in the sitemap")

    out: dict[str, dict] = {}
    now = int(time.time())
    for su in sale_urls:
        try:
            first = fetch(su)
        except Exception as e:                              # noqa: BLE001
            print(f"  ! {su[-40:]}: {e}")
            continue
        ts = re.search(r'data-ts="(\d+)"', first)
        if not ts:
            print(f"  - {su[-40:]}: no clock, skipped")
            continue
        end = int(ts.group(1))
        if end < now - 600 or end > now + args.horizon:
            continue
        page, added = 1, 0
        while page <= args.max_pages:
            body = first if page == 1 else fetch(f"{su}?page={page}")
            links = re.findall(r'href="(/(?:voiture|utilitaire)-occasion/[^"]+)"', body)
            fresh = 0
            for href in set(links):
                m = re.search(r"-(\d+)$", href.rstrip("/"))
                if not m:
                    continue
                lid = f"alcopa:{m.group(1)}"
                if lid not in out:
                    out[lid] = {"lot_id": lid, "url": BASE + href, "ends_ts": end}
                    fresh += 1
            added += fresh
            if len(set(links)) < 20:        # short page = last page
                break
            page += 1
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(end))}  "
              f"{added:>4} lots  {su.rsplit('/', 2)[-2]}")

    dest = Path(args.out or (DATA / "watchlist.json"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(sorted(out.values(), key=lambda r: r["ends_ts"]),
                               ensure_ascii=False, indent=1), encoding="utf-8")
    groups = sorted({r["ends_ts"] for r in out.values()})
    print(f"\n{len(out)} lots across {len(groups)} closing times -> {dest}")
    for g in groups[:14]:
        n = sum(1 for r in out.values() if r["ends_ts"] == g)
        print(f"   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(g))}  {n:>5} lots")


def cmd_exportlots(args) -> None:
    """Write the portable watch list the cloud runner consumes."""
    con = con_with_extras()
    now = int(time.time())
    rows = con.execute(
        "SELECT lot_id, url, ends_ts FROM lots WHERE source='alcopa' "
        "AND ends_ts IS NOT NULL AND ends_ts > ? AND ends_ts < ? "
        "AND COALESCE(sale_state,'') != 'adjuge' ORDER BY ends_ts",
        (now - 600, now + args.horizon)).fetchall()
    out = Path(args.out or (DATA / "watchlist.json"))
    out.write_text(json.dumps(
        [{"lot_id": a, "url": b, "ends_ts": c} for a, b, c in rows],
        ensure_ascii=False, indent=1), encoding="utf-8")
    con.close()
    groups = sorted({c for _a, _b, c in rows})
    print(f"{len(rows)} lots across {len(groups)} closing times -> {out}")
    for g in groups[:12]:
        n = sum(1 for _a, _b, c in rows if c == g)
        print(f"   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(g))}  {n:>4} lots")


def cmd_report(args) -> None:
    con = con_with_extras()
    print("-- alcopa rows by state")
    for st, n in con.execute(
            "SELECT COALESCE(sale_state,'?'), COUNT(*) FROM lots "
            "WHERE source='alcopa' GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {st:12s} {n}")
    row = con.execute(
        "SELECT COUNT(*), SUM(photo_count) FROM lots WHERE source='alcopa'"
    ).fetchone()
    print(f"-- {row[0]} lots, {row[1] or 0} photos")
    print(f"-- damage rows: "
          f"{con.execute('SELECT COUNT(*) FROM damage').fetchone()[0]}")
    print("-- lots whose price moved above the opening bid")
    for lot, lo, hi in con.execute(
            "SELECT lot_id, MIN(price), MAX(price) FROM price_log "
            "WHERE lot_id LIKE 'alcopa:%' GROUP BY lot_id HAVING MAX(price)>MIN(price) "
            "ORDER BY MAX(price)-MIN(price) DESC LIMIT 15"):
        print(f"   {lot:22s} {lo:>8,.0f} -> {hi:>8,.0f}")
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover")
    h = sub.add_parser("harvest")
    h.add_argument("--limit", type=int, default=0)
    h.add_argument("--workers", type=int, default=4)
    w = sub.add_parser("watch")
    w.add_argument("--horizon", type=int, default=86400,
                   help="only lots closing within this many seconds")
    w.add_argument("--workers", type=int, default=12)
    w.add_argument("--lots", metavar="FILE",
                   help="JSON watch list [{lot_id,url,ends_ts}] instead of the DB, "
                        "so this can run where the database does not exist")
    w.add_argument("--out", metavar="FILE",
                   help="append every observation to this JSONL")
    rf = sub.add_parser("refresh")
    rf.add_argument("--limit", type=int, default=0)
    rf.add_argument("--workers", type=int, default=4)
    sa = sub.add_parser("sales")
    sa.add_argument("--horizon", type=int, default=14*86400,
                    help="only sales closing within this many seconds")
    sa.add_argument("--max-pages", type=int, default=90)
    sa.add_argument("--out")
    el = sub.add_parser("exportlots")
    el.add_argument("--horizon", type=int, default=86400)
    el.add_argument("--out")
    sub.add_parser("report")
    args = ap.parse_args()
    {"discover": cmd_discover, "harvest": cmd_harvest, "refresh": cmd_refresh,
     "watch": cmd_watch, "report": cmd_report,
     "exportlots": cmd_exportlots, "sales": cmd_sales}[args.cmd](args)


if __name__ == "__main__":
    main()
