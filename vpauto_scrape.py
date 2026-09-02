"""VPauto (FR) auction scraper -> SQLite, with Moldovan landed-cost model.

Public pages only: /vehicule/liste?page=N and /vehicule/<id>/<slug>. No login,
no paywall, no anti-bot bypass. Single-threaded with a polite delay.

Where the numbers live, which is not obvious:
  * listing pages  -> "Adjugé <price>" (the HAMMER price), "Enchère en cours"
                      (the live bid), or "Mise à prix" (not open yet)
  * detail pages   -> the car itself: cc, registration date, Euro norm, km,
                      Cote (VPauto's own market valuation), photos, defects
Neither page has both, so the tool reads listing pages for money and detail
pages for metal, and joins them on the lot id.

Why VPauto and not Alcopa: alcopa-auction.fr sits behind an AWS WAF CAPTCHA
that deliberately gates automated access, so it is off limits.

Usage:
    python vpauto_scrape.py --search --pages 10 --detail 60   # sweep + price
    python vpauto_scrape.py --watch --minutes 180 --every 120 # follow a live sale
    python vpauto_scrape.py --report                          # ranked buy list
    python vpauto_scrape.py --photos <lot_id>                 # that lot's photos
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sqlite3
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DB_PATH = DATA / "vpauto.db"
PHOTOS = DATA / "photos"
MD_DB = Path(os.environ.get("MD_DB", r"E:\DB\listings.db"))  # the 999.md scraper's DB

BASE = "https://www.vpauto.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DELAY = (2.0, 4.0)          # seconds between requests — keep it polite
TIMEOUT = 30

# ---------------------------------------------------------------- MD fiscal model
# lei/cm3 by age column: 0-2, 3-4, 5-6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16...20+
AGE_BOUNDS = [0, 3, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
BANDS = {
    "p1000": [9.56, 10.00, 10.23, 11.25, 12.38, 13.62, 16.34, 21.24, 26.24, 31.24,
              36.24, 41.24, 46.24, 51.24, 56.24, 61.24, 66.24],
    "p1500": [12.23, 12.67, 12.90, 14.19, 15.61, 17.17, 20.60, 26.79, 31.79, 36.79,
              41.79, 46.79, 51.79, 56.79, 61.79, 66.79, 71.79],
    "p2000": [18.90, 19.34, 19.57, 21.53, 23.68, 26.05, 31.26, 40.63, 45.79, 50.63,
              55.63, 60.63, 65.63, 70.63, 75.63, 80.63, 85.63],
    "p3000": [31.14, 31.58, 31.81, 34.99, 38.49, 42.34, 50.81, 66.05, 71.05, 76.05,
              81.05, 86.05, 91.05, 96.05, 101.05, 106.05, 111.05],
    "pbig": [55.60, 56.04, 56.27, 61.90, 68.09, 74.90, 89.87, 116.84, 121.84, 126.84,
             131.84, 136.84, 141.84, 146.84, 151.84, 156.84, 161.84],
}
MDL_PER_EUR = float(os.environ.get("MDL_EUR", "19.9863"))   # BNM 22.08.2026
SHIPPING_EUR = float(os.environ.get("SHIPPING_EUR", "900"))  # UNVERIFIED — get a real quote
DOSSIER_EUR = 200.0      # VPauto frais de dossier, per lot
TARGET_MARGIN = 1500.0


def _age_col(age_years: int) -> int:
    col = 0
    for i, bound in enumerate(AGE_BOUNDS):
        if age_years >= bound:
            col = i
    return col


def _band(cc: int, fuel: str) -> str:
    # Diesel must be detected anywhere in the label, not just at the start:
    # VPauto writes "Gazole", "Electricite / Gazole", "GAS + ELEK HR", and
    # Alcopa's raw codes were the same class of bug (GO/ES/EL vs startswith).
    # Getting this wrong moves a car across the 1500cc cliff, which is worth
    # thousands.
    low = fuel.lower()
    diesel = low.startswith("diesel") or "gazole" in low or "diesel" in low
    if diesel:
        return "p1500" if cc <= 1500 else ("p3000" if cc <= 2500 else "pbig")
    if cc <= 1000:
        return "p1000"
    if cc <= 1500:
        return "p1500"
    if cc <= 2000:
        return "p2000"
    if cc <= 3000:
        return "p3000"
    return "pbig"


def excise_eur(cc: int, fuel: str, first_reg: str | None) -> float | None:
    """Moldovan excise in EUR. `first_reg` is dd/mm/yyyy."""
    if not fuel or not first_reg:
        return None
    f = fuel.lower()
    # Only a pure EV is exempt. VPauto writes combined labels like
    # "Electricite / Gazole" (a diesel plug-in hybrid) and "ESS + ELEK HR",
    # and testing for "lectri" first exempted a real diesel engine from excise
    # altogether — a Mercedes GLC 300 de came out at 0 EUR instead of several
    # thousand. If another fuel is named too, it is a hybrid, not an EV.
    combustion = ("gazole", "diesel", "essence", "ess ", "ess+", "gpl",
                  "gaz", "eth", "hybride")
    if "lectri" in f or "elek" in f:
        if not any(k in f for k in combustion):
            return 0.0          # genuinely electric only; cc is 0 on those lots
        if not cc:
            return None
        # combined label = a hybrid; fall through so the multiplier applies
    if not cc:
        return None
    mult = 1.0
    # VPauto abbreviates on some lots: "HR" = hybride rechargeable (plug-in),
    # "HNR" = hybride non rechargeable. Those were matching none of the long
    # spellings, so 40 rows silently paid full excise.
    if "hybride rechargeable" in f or "plug" in f or " hr" in f or f.endswith("hr"):
        mult = 0.5
    elif ("hybride" in f or "hnr" in f
          or ("lectri" in f or "elek" in f))             and "micro" not in f and "mild" not in f:
        # A combined electric+combustion label with no explicit "rechargeable"
        # marker is treated as an ordinary hybrid: that understates the
        # discount rather than overstating it, which is the safe direction for
        # a cost estimate.
        mult = 0.75
    try:
        d, m, y = (int(x) for x in first_reg.split("/"))
    except ValueError:
        return None
    today = date.today()
    age = today.year - y - ((today.month, today.day) < (m, d))
    coef = BANDS[_band(cc, fuel)][_age_col(max(age, 0))]
    return cc * coef * mult / MDL_PER_EUR


def landed_eur(price_ttc: float, cc: int, fuel: str, first_reg: str | None) -> float | None:
    ex = excise_eur(cc, fuel, first_reg)
    if ex is None:
        return None
    return price_ttc + DOSSIER_EUR + ex + SHIPPING_EUR + min(price_ttc * 0.004, 1800.0)


# ---------------------------------------------------------------- fetching
def fetch(url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:            # noqa: BLE001 - network is best-effort
            last = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def nap() -> None:
    time.sleep(random.uniform(*DELAY))


# ---------------------------------------------------------------- parsing
def _clean(html: str) -> str:
    return html.replace("&nbsp;", " ").replace("\u00a0", " ").replace("\u202f", " ")


def _field(html: str, label: str) -> str | None:
    """Pull a value out of the '<span>Label : </span> value</li>' spec list.

    The value is usually bare text after the label's closing tag, but a few
    rows wrap it in another tag — try both, in that order.
    """
    for pat in (label + r"\s*:?\s*</[^>]+>\s*([^<]{1,60})",
                label + r"\s*:?\s*</[^>]+>\s*<[^>]+>\s*([^<]{1,60})"):
        m = re.search(pat, html)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _euro(html: str, pattern: str) -> float | None:
    m = re.search(pattern, html)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


# Wording VPauto uses in Observation(s) that changes what a lot is worth.
# "Non roulant / moteur hors service" is the difference between a 56% discount
# that is a bargain and one that is a dead engine on a truck.
RED_FLAGS = {
    "NON-ROULANT": r"non\s*roulant",
    "MOTEUR-HS": r"moteur\s+hors\s+service|moteur\s+HS",
    "BOITE-HS": r"bo[iî]te\s+(?:hors\s+service|HS)",
    "ACCIDENT": r"accident|choc|sinistr",
    "EN-L-ETAT": r"vendu\s+en\s+l.{0,6}[ée]tat",
    "PAS-PARTICULIER": r"d[ée]conseill[ée]e?\s+.{0,6}particulier",
    "IMPORT": r"origine\s+import",
    "CARNET-KO": r"pas\s+.{0,3}\s*jour|sans\s+carnet",
    "CT-KO": r"contr[oô]le\s+technique\s+(?:non|d[ée]favorable)",
}


def red_flags(observations: str | None, service_book: str | None) -> str | None:
    text = " ".join(x for x in (observations, service_book) if x)
    if not text:
        return None
    hits = [name for name, pat in RED_FLAGS.items()
            if re.search(pat, text, re.I)]
    return ",".join(hits) if hits else None


CARD_RE = re.compile(r'<article class="element"[\s\S]*?</article>')
# On a listing card the money sits in one of two places, and WHICH one it is
# tells you where the lot is in its life:
#   vehicle-salingState -> "Adjugé 15800 €"        = the hammer price. Final.
#   elmt-prix           -> "Mise à prix 20700 €"   = not open yet
#                       -> "Enchère en cours 12700 €" = live, right now
# The detail page shows none of this, which is why an earlier pass concluded
# — wrongly — that VPauto never publishes what a lot sold for.
SALING_RE = re.compile(
    r'vehicle-salingState[\s\S]{0,400}?<span>\s*([^<]+?)\s*</span>\s*'
    r'<span>\s*([0-9 .,  ]+)\s*&euro;')
PRIX_RE = re.compile(
    r'elmt-prix[\s\S]{0,300}?<span>\s*([^<]+?)\s*</span>\s*'
    r'<span class="prix">\s*([0-9 .,  ]+)\s*&euro;')


def _num(raw: str) -> float | None:
    raw = re.sub(r"[\s  ]", "", raw).replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _state_of(label: str) -> str:
    low = label.lower()
    if "adjug" in low:
        return "adjuge"
    if "en cours" in low:
        return "en_cours"
    if "mise" in low:
        return "mise_a_prix"
    return low[:24]


def parse_cards(html: str) -> list[dict]:
    """One row per listing card: url, year, km, and the live/final price."""
    out = []
    for card in CARD_RE.findall(html):
        m = re.search(r'href="(/vehicule/[0-9a-f]+/[^"]+)"', card)
        if not m:
            continue
        hit = SALING_RE.search(card) or PRIX_RE.search(card)
        state = _state_of(hit.group(1)) if hit else None
        price = _num(hit.group(2)) if hit else None
        yr = re.search(r"<span>((?:19|20)\d{2})</span>", card)
        km = re.search(r"<span>\s*([0-9  ]+?)\s*Km\s*</span>", card)
        out.append({
            "url": m.group(1),
            "lot_id": m.group(1).split("/")[2],
            "card_year": int(yr.group(1)) if yr else None,
            "card_km": int(re.sub(r"\D", "", km.group(1))) if km else None,
            "sale_state": state,
            "sale_price": price if state == "adjuge" else None,
            "current_bid": price if state == "en_cours" else None,
            "card_mise": price if state == "mise_a_prix" else None,
        })
    return out


def parse_search(html: str) -> list[str]:
    return [c["url"] for c in parse_cards(html)]


def parse_lot(html: str, url: str) -> dict:
    h = _clean(html)
    title = re.search(r"<title>\s*(.*?)\s*(?:\||</title>)", h, re.S)
    lot_id = url.split("/")[2] if url.startswith("/vehicule/") else url

    # Order matters: the negative must be tested first, because "n'a pas été
    # adjugé" contains "adjugé".
    sold, hammer = None, None
    if re.search(r"n(?:&#039;|')a pas .t. adjug", h):
        sold = "non_adjuge"
    elif re.search(r"a .t. adjug|V.hicule adjug", h):
        # A sold lot says "Véhicule adjugé", NOT "a été adjugé" — matching only
        # the latter left every sold VPauto lot reading as still open, showing
        # its opening price as if bidding had not happened.
        sold = "adjuge"
        hammer = _euro(h, r'V.hicule adjug.[\s\S]{0,120}?'
                          r'class="amount"[^>]*>\s*([0-9 .,]+)\s*&euro;')

    # VPauto lots DO have a closing time — we were simply never reading it, so
    # every VPauto card said "no closing time" while the site showed a live
    # countdown. It is on the .countdown element as data-end-date, in Paris
    # local time. A page carries two: the live opening (09:00) and the lot's
    # own sale start (10:00); the later one is the lot's, so take the max.
    # Pick the countdown by its LABEL, never by max(). A page carries several
    # data-end-date stamps and the latest one is usually the wrong one:
    #   * on an unsold lot the widget counts down to "Fin de l'apres vente",
    #     a post-sale offer window up to a day after bidding actually ended —
    #     max() chose it for all 201 unsold lots, so their clock said the
    #     auction was still open when it was over and lost;
    #   * on a sold lot the latest stamp is the "Exposition" viewing banner.
    # The sale's own clock sits under "Ouverture de la vente".
    ends_ts = apres_vente = None
    try:
        from zoneinfo import ZoneInfo
        paris = ZoneInfo("Europe/Paris")

        def _epoch(s: str) -> int | None:
            m = re.match(r"(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})$", s)
            if not m:
                return None
            return int(dt.datetime(*(int(x) for x in m.groups()),
                                   tzinfo=paris).timestamp())

        # each stamp with the ~400 chars of markup before it, so the nearest
        # preceding label can be read
        for m in re.finditer(r'data-end-date="([^"]+)"', h):
            before = h[max(0, m.start() - 400):m.start()]
            ts = _epoch(m.group(1))
            if ts is None:
                continue
            if re.search(r"apr[eè]s[\s-]*vente", before, re.I):
                apres_vente = apres_vente or ts
            elif re.search(r"Ouverture de la vente|id=\"countdown\"", before, re.I):
                ends_ts = ends_ts or ts
        # Fall back to the EARLIEST stamp rather than the latest: the sale
        # close precedes both the after-sale deadline and the viewing banner.
        if ends_ts is None:
            all_ts = [t for t in (_epoch(x) for x in
                                  re.findall(r'data-end-date="([^"]+)"', h)) if t]
            ends_ts = min(all_ts) if all_ts else None
    except Exception as exc:                                # noqa: BLE001
        # Narrow enough to notice: a missing tz database would otherwise null
        # every VPauto closing time silently.
        print(f"  ! ends_ts parse failed: {str(exc)[:80]}")
        ends_ts = None

    photos = sorted({u for u in re.findall(r"https?://cdn\.vpauto\.fr/[^\"')\s]+\.jpe?g", h)
                     if u.endswith("-1200.jpg")})

    obs = re.search(r"Observation\(s\)(.{0,1200}?)</div>", h, re.S)
    if obs:
        obs_txt = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", obs.group(1))).strip()[:600]
    else:
        obs_txt = None

    row = {
        "lot_id": lot_id,
        "url": BASE + url if url.startswith("/") else url,
        "title": title.group(1).strip() if title else None,
        "first_reg": _field(h, "Mise en circulation"),
        "km": _field(h, "Kilom.trage") or _field(h, "Kilométrage"),
        "cc": _field(h, "Cylindr.e") or _field(h, "Cylindrée"),
        "fuel": _field(h, "Energie"),
        "gearbox": _field(h, "Type de boite"),
        "body": _field(h, "Carrosserie"),
        "power_hp": _field(h, "Puissance \\(ch\\)") or _field(h, "Puissance"),
        "euro_norm": _field(h, "Norme Euro"),
        "service_book": _field(h, "Carnet d.{0,8}Entretien"),
        "location": _field(h, "Localisation"),
        # the starting price is carried as attributes on <span class="amount">
        "mise_a_prix": _euro(h, r'class="amount"[^>]*data-ttc="([0-9 .,]+)"'),
        "mise_a_prix_ht": _euro(h, r'class="amount"[^>]*data-ht="([0-9 .,]+)"'),
        # bound the gap: an unbounded [\s\S]*? happily jumps to the NEXT lot's
        # amount when this lot has no Cote block, inventing 80%+ discounts
        "cote": _euro(h, r"<span>Cote</span>[\s\S]{0,80}?<span[^>]*>\s*([0-9 .,]+)\s*&euro;"),
        "prix_neuf": _euro(h, r"<span>Prix neuf</span>[\s\S]{0,80}?<span[^>]*>\s*([0-9 .,]+)\s*&euro;"),
        "tva_recup": 1 if re.search(r"TVA\s*:\s*oui", h, re.I) else 0,
        "sold_status": sold,
        "ends_ts": ends_ts,
        # the hammer price, which only a sold lot has
        "sale_price": hammer,
        "observations": obs_txt,
        "photo_count": len(photos),
        "photos": "\n".join(photos),
        "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    for k in ("km", "cc", "power_hp"):
        row[k] = int(row[k]) if row[k] and str(row[k]).isdigit() else None

    row["red_flags"] = red_flags(row["observations"], row["service_book"])

    is_ev = bool(row["fuel"]) and "lectri" in row["fuel"].lower()
    if row["mise_a_prix"] and row["fuel"] and (row["cc"] or is_ev):
        row["excise_eur"] = excise_eur(row["cc"], row["fuel"], row["first_reg"])
        row["landed_eur"] = landed_eur(row["mise_a_prix"], row["cc"],
                                       row["fuel"], row["first_reg"])
    else:
        row["excise_eur"] = row["landed_eur"] = None
    # discount of the starting price against VPauto's own market valuation
    if row["mise_a_prix"] and row["cote"]:
        row["discount_pct"] = round(100 * (1 - row["mise_a_prix"] / row["cote"]), 1)
    else:
        row["discount_pct"] = None
    return row


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    lot_id TEXT PRIMARY KEY, url TEXT, title TEXT, first_reg TEXT, km INTEGER,
    cc INTEGER, fuel TEXT, gearbox TEXT, body TEXT, power_hp INTEGER,
    euro_norm TEXT, service_book TEXT, location TEXT,
    mise_a_prix REAL, mise_a_prix_ht REAL, cote REAL, prix_neuf REAL, tva_recup INTEGER,
    sold_status TEXT, observations TEXT, red_flags TEXT,
    photo_count INTEGER, photos TEXT,
    excise_eur REAL, landed_eur REAL, discount_pct REAL, scraped_at TEXT,
    sale_state TEXT, sale_price REAL, current_bid REAL,
    card_year INTEGER, card_km INTEGER, last_seen TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS idx_lots_cc ON lots(cc, fuel);
CREATE INDEX IF NOT EXISTS idx_lots_disc ON lots(discount_pct);
CREATE INDEX IF NOT EXISTS idx_lots_state ON lots(sale_state);

-- every observed price, so a lot watched through its sale leaves a trail from
-- opening bid to hammer instead of just its final value
CREATE TABLE IF NOT EXISTS price_log (
    lot_id TEXT, ts TEXT, state TEXT, price REAL,
    PRIMARY KEY (lot_id, ts)
);
"""

CARD_COLS = ("sale_state", "sale_price", "current_bid", "card_year", "card_km")


def db() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(lots)")}
    for col, decl in (("red_flags", "TEXT"), ("mise_a_prix_ht", "REAL"),
                      ("sale_state", "TEXT"), ("sale_price", "REAL"),
                      ("current_bid", "REAL"), ("card_year", "INTEGER"),
                      ("card_km", "INTEGER"), ("last_seen", "TEXT"), ("source", "TEXT"),
                      # VPauto lots have a closing time too — it was simply
                      # never read, so every VPauto card said "no closing time"
                      ("ends_ts", "INTEGER"), ("sold_status", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE lots ADD COLUMN {col} {decl}")
    # backfill flags for rows stored before the column existed
    for lot_id, obs, book in con.execute(
            "SELECT lot_id, observations, service_book FROM lots "
            "WHERE red_flags IS NULL AND observations IS NOT NULL").fetchall():
        con.execute("UPDATE lots SET red_flags=? WHERE lot_id=?",
                    (red_flags(obs, book), lot_id))
    con.commit()
    return con


def upsert(con: sqlite3.Connection, row: dict) -> None:
    cols = [c for c in row if c != "photos"] + ["photos"]
    vals = [row[c] for c in cols]
    con.execute(
        f"INSERT OR REPLACE INTO lots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals)
    con.commit()


# ---------------------------------------------------------------- MD comparison
def md_median_price(make: str, model: str, year: int) -> tuple[int | None, int]:
    """Median EUR price of comparable SOLD cars on 999.md, +/- 1 model year."""
    if not MD_DB.exists():
        return None, 0
    con = sqlite3.connect(f"file:{MD_DB}?mode=ro", uri=True, timeout=30)
    rows = con.execute(
        "SELECT price_eur FROM listings WHERE status='sold' AND make LIKE ? "
        "AND model LIKE ? AND year BETWEEN ? AND ? AND price_eur BETWEEN 2000 AND 40000",
        (make, f"%{model}%", year - 1, year + 1)).fetchall()
    con.close()
    prices = sorted(r[0] for r in rows if r[0])
    if len(prices) < 5:
        return None, len(prices)
    return prices[len(prices) // 2], len(prices)


# ---------------------------------------------------------------- commands
def save_card(con: sqlite3.Connection, card: dict) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    con.execute("INSERT OR IGNORE INTO lots (lot_id, url) VALUES (?, ?)",
                (card["lot_id"], BASE + card["url"]))
    con.execute(
        "UPDATE lots SET sale_state=?, card_year=?, card_km=?, last_seen=?, "
        "sale_price=COALESCE(?, sale_price), current_bid=? WHERE lot_id=?",
        (card["sale_state"], card["card_year"], card["card_km"], ts,
         card["sale_price"], card["current_bid"], card["lot_id"]))
    if card["card_mise"]:
        con.execute("UPDATE lots SET mise_a_prix=COALESCE(mise_a_prix, ?) WHERE lot_id=?",
                    (card["card_mise"], card["lot_id"]))
    price = card["sale_price"] or card["current_bid"] or card["card_mise"]
    if price is not None:
        con.execute("INSERT OR IGNORE INTO price_log VALUES (?, ?, ?, ?)",
                    (card["lot_id"], ts, card["sale_state"], price))
    con.commit()


def sweep_pages(con: sqlite3.Connection, max_pages: int) -> list[dict]:
    """Walk the listing pages. One request per ~96 lots, and the only place
    the hammer price is published.

    Note: the ?maker= parameter is decorative — it returns the same global
    catalogue whatever you pass — so brands are filtered locally instead.
    """
    found, seen_ids = [], set()
    for page in range(1, max_pages + 1):
        url = f"{BASE}/vehicule/liste?page={page}"
        try:
            cards = parse_cards(fetch(url))
        except Exception as exc:                          # noqa: BLE001
            print(f"    !! p{page}: {exc}")
            break
        if not cards:
            break
        fresh = [c for c in cards if c["lot_id"] not in seen_ids]
        for c in fresh:
            save_card(con, c)
            seen_ids.add(c["lot_id"])
        found += fresh
        states: dict[str | None, int] = {}
        for c in cards:
            states[c["sale_state"]] = states.get(c["sale_state"], 0) + 1
        print(f"    p{page}: {len(cards)} lots ({len(fresh)} new)  {states}")
        if not fresh:                  # pagination exhausted / looping
            break
        nap()
    return found


def cmd_search(brands: list[str] | None, pages: int, detail_limit: int) -> None:
    con = db()
    print(f"=== sweeping {pages} listing pages")
    all_cards = sweep_pages(con, pages)

    if brands:
        pats = [b.lower() for b in brands]
        all_cards = [c for c in all_cards
                     if any(p in c["url"].lower() for p in pats)]
        print(f"    filtered to {len(all_cards)} lots matching {brands}")

    live = [c for c in all_cards if c["sale_state"] in ("adjuge", "en_cours")]
    print(f"\n{len(all_cards)} lots seen | {len(live)} with a real bid/hammer price")

    # detail pages only for lots that actually have a price worth pricing out
    todo = [c for c in live if c["sale_state"] == "adjuge"] + \
           [c for c in live if c["sale_state"] == "en_cours"]
    todo = todo[:detail_limit]
    if not todo:
        return
    print(f"fetching {len(todo)} detail pages for cc/registration/photos\n")
    for c in todo:
        try:
            row = parse_lot(fetch(BASE + c["url"]), c["url"])
        except Exception as exc:                          # noqa: BLE001
            print(f"    !! {c['url']}: {exc}")
            nap()
            continue
        # the card knows the real price; the detail page knows the car
        row.pop("lot_id", None)
        cols = [k for k in row if k != "photos"] + ["photos"]
        con.execute(f"UPDATE lots SET {','.join(c2 + '=?' for c2 in cols)} WHERE lot_id=?",
                    [row[c2] for c2 in cols] + [c["lot_id"]])
        con.commit()
        price = c["sale_price"] or c["current_bid"]
        landed = landed_eur(price, row["cc"], row["fuel"], row["first_reg"]) \
            if (row["fuel"] and price) else None
        print(f"    {(row['title'] or '')[:40]:42.42s} {c['sale_state']:9s} "
              f"{price:>8,.0f} cote={row['cote'] or 0:>7,.0f} "
              f"landed={landed or 0:>8,.0f} {row['red_flags'] or ''}")
        nap()


def cmd_refresh(limit: int) -> None:
    """Fetch detail pages for lots that only ever got their listing card.

    `cmd_search` fetches detail for at most `--detail` priced lots per run, so
    a sweep leaves most rows card-level: no photos, no cc, and therefore no
    excise and no landed cost. Those rows are not failures, just unfinished —
    and nothing ever came back for them. This is that second pass.
    """
    con = db()
    rows = con.execute(
        "SELECT lot_id, url FROM lots "
        "WHERE COALESCE(source,'vpauto')='vpauto' "
        # ends_ts and sold_status were added later, so a lot whose detail page
        # was fetched before then looks complete while carrying no clock and no
        # sale outcome — which is what made every VPauto card read "no closing
        # time" and show a sold lot's opening price as if it were still open.
        "  AND (COALESCE(photo_count,0)=0 OR cc IS NULL OR ends_ts IS NULL) "
        "ORDER BY COALESCE(sale_price, current_bid, mise_a_prix) DESC"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    print(f"{len(rows)} card-level VPauto lots to complete")
    done = 0
    for lot_id, url in rows:
        try:
            path = url.replace(BASE, "") if url else ""
            row = parse_lot(fetch(BASE + path), path)
        except Exception as exc:                          # noqa: BLE001
            print(f"    !! {url}: {exc}")
            nap()
            continue
        row.pop("lot_id", None)
        cols = [k for k in row if k != "photos"] + ["photos"]
        con.execute(f"UPDATE lots SET {','.join(c + '=?' for c in cols)} WHERE lot_id=?",
                    [row[c] for c in cols] + [lot_id])
        con.commit()
        done += 1
        if done % 20 == 0:
            print(f"    {done}/{len(rows)}")
        nap()
    print(f"completed {done} lots")


def cmd_watch(brands: list[str] | None, pages: int, minutes: int, every: int) -> None:
    """Re-poll listing pages until the sale closes, logging every price move.

    This is how a hammer price gets captured: a lot shows 'Enchère en cours'
    while bidding, then flips to 'Adjugé <price>' the moment it sells.
    """
    con = db()
    deadline = time.time() + minutes * 60
    rnd = 0
    while time.time() < deadline:
        rnd += 1
        print(f"\n--- pass {rnd}  {time.strftime('%H:%M:%S')}")
        for c in sweep_pages(con, pages):
                if c["sale_state"] == "adjuge":
                    print(f"    ADJUGE {c['sale_price']:>8,.0f}  {c['url'].split('/')[-1][:52]}")
        left = deadline - time.time()
        if left <= 0:
            break
        print(f"--- sleeping {every}s ({left/60:.0f} min left)")
        time.sleep(min(every, max(left, 0)))
    print("\nwatch done — run --report")


KILLER_FLAGS = ("NON-ROULANT", "MOTEUR-HS", "BOITE-HS", "ACCIDENT")


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def cmd_report() -> None:
    con = db()
    rows = con.execute(
        "SELECT title, first_reg, COALESCE(km, card_km), cc, fuel, cote, "
        "  COALESCE(sale_price, current_bid, mise_a_prix) AS paid, "
        "  sale_state, tva_recup, red_flags "
        "FROM lots WHERE cote IS NOT NULL AND cote > 0 "
        "  AND COALESCE(sale_price, current_bid, mise_a_prix) IS NOT NULL "
        "ORDER BY sale_state='adjuge' DESC, paid").fetchall()
    if not rows:
        print("no priced lots yet — run --search first")
        return

    out = []
    for (title, reg, km, cc, fuel, cote, paid, state, tva, flags) in rows:
        disc = 100 * (1 - paid / cote)
        land = landed_eur(paid, cc, fuel, reg) if (fuel and reg) else None
        out.append((title, reg, km, cc, cote, paid, state, disc, land, tva, flags))

    print(f"{'lot':38s} {'stare':10s} {'platit':>8} {'cote':>7} {'disc':>6} "
          f"{'km':>8} {'landed':>8} flags")
    for (title, reg, km, cc, cote, paid, state, disc, land, tva, flags) in out:
        print(f"{(title or '')[:38]:38.38s} {state or '?':10.10s} {paid:>8,.0f} "
              f"{cote:>7,.0f} {disc:>5.1f}% {km or 0:>8,} "
              f"{land or 0:>8,.0f} {flags or ''}")

    def stats(label: str, sel: list) -> None:
        if not sel:
            print(f"  {label:34s} —")
            return
        ds = [r[7] for r in sel]
        print(f"  {label:34s} n={len(sel):<4} mediana {_median(ds):5.1f}%  "
              f"[{min(ds):.1f} .. {max(ds):.1f}]")

    clean = [r for r in out if not (r[10] and any(f in r[10] for f in KILLER_FLAGS))]
    hammered = [r for r in clean if r[6] == "adjuge"]
    print(f"\nloturi cu cote + pret: {len(out)}  "
          f"({len(out) - len(clean)} cu defect major, exclusi mai jos)")
    stats("discount la ADJUDECARE (real)", hammered)
    stats("discount la enchere en cours", [r for r in clean if r[6] == "en_cours"])
    stats("discount la mise a prix", [r for r in clean if r[6] == "mise_a_prix"])
    print("\n  Doar randul ADJUDECARE e un pret platit efectiv. Restul urca.")
    print("  Prag: mediana la adjudecare >= 25% tine afacerea in viata.")


# ---------------------------------------------------------------- offline import
# Alcopa gates automated access behind an AWS WAF CAPTCHA, so it is never
# fetched here. What this does instead: you browse Alcopa yourself, Ctrl+S a
# lot page, drop the .html in a folder, and it lands in the same table under
# the same landed-cost model as the VPauto lots.
GENERIC_FIELDS = {
    "km": [r"([0-9][0-9  .]{2,9})\s*(?:km|KM|Km)\b"],
    "cc": [r"[Cc]ylindr[ée]e?\s*:?\s*([0-9]{3,4})", r"\b([0-9]{3,4})\s*cm3\b"],
    "first_reg": [r"([0-3]?[0-9]/[0-1]?[0-9]/(?:19|20)[0-9]{2})",
                  r"[Mm]ise en circulation\s*:?\s*([0-9/]{8,10})"],
    "fuel": [r"\b(Diesel|Essence|Electrique|Hybride(?: rechargeable)?|GPL|GNV)\b"],
    "euro_norm": [r"[Nn]orme?\s+[Ee]uro\s*:?\s*([0-9][a-d]?)"],
}


def sniff_source(html: str, path: Path) -> str:
    low = (html[:6000] + path.name).lower()
    if "vpauto" in low:
        return "vpauto"
    if "alcopa" in low:
        return "alcopa"
    return "unknown"


def parse_generic(html: str, path: Path) -> dict:
    """Best-effort extraction from a page whose markup we have not mapped."""
    h = _clean(html)
    text = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ",
                                      re.sub(r"(?is)<(script|style).*?</\1>", " ", h)))
    row: dict = {"lot_id": f"{sniff_source(html, path)}:{path.stem}"[:80],
                 "url": str(path), "title": None}
    t = re.search(r"<title>\s*(.*?)\s*</title>", h, re.S)
    row["title"] = (t.group(1)[:160] if t else path.stem)
    for key, pats in GENERIC_FIELDS.items():
        for p in pats:
            m = re.search(p, text)
            if m:
                row[key] = m.group(1).strip()
                break
        else:
            row[key] = None
    for k in ("km", "cc"):
        if row[k]:
            digits = re.sub(r"\D", "", row[k])
            row[k] = int(digits) if digits else None
    # every labelled money amount, so a human can pick the real one
    amounts = re.findall(r"([A-Za-zÀ-ÿ' ]{3,28}?)\s*:?\s*([0-9][0-9  .]{2,9})\s*(?:€|EUR)", text)
    row["observations"] = " | ".join(f"{a.strip()}={b.strip()}" for a, b in amounts[:12]) or None
    row["photos"] = "\n".join(sorted(set(
        re.findall(r"https?://[^\"')\s]+\.(?:jpe?g|webp)", h))))
    row["photo_count"] = len([p for p in row["photos"].split("\n") if p])
    row["red_flags"] = red_flags(row["observations"], None)
    return row


def import_harvest_json(con: sqlite3.Connection, path: Path) -> int:
    """Rows exported by alcopa_harvest.user.js — read off the page the user
    was looking at, so no request was ever made from here."""
    rows = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(rows, dict):
        rows = list(rows.values())
    STATE = {"adjuge": "adjuge", "vendu": "adjuge", "en_cours": "en_cours",
             "mise_a_prix": "mise_a_prix"}
    n = 0
    for r in rows:
        price = r.get("price")
        state = STATE.get(r.get("price_label") or "", None)
        year = r.get("year")
        # the harvester sees a year, not a registration date; 1 July keeps the
        # excise age band honest either side of a birthday
        first_reg = f"01/07/{year}" if year else None
        row = {
            "lot_id": f"alcopa:{abs(hash(r.get('url') or r.get('title'))) % 10**12}",
            "url": r.get("url"), "title": (r.get("title") or "")[:160],
            "source": r.get("source") or "alcopa",
            "first_reg": first_reg, "card_year": year,
            "km": r.get("km"), "card_km": r.get("km"), "cc": r.get("cc"),
            "fuel": r.get("fuel"), "euro_norm": r.get("euro_norm"),
            "sale_state": state,
            "sale_price": price if state == "adjuge" else None,
            "current_bid": price if state == "en_cours" else None,
            "mise_a_prix": price if state == "mise_a_prix" else None,
            "cote": price if r.get("price_label") == "estimation" else None,
            "observations": r.get("raw_text"),
            "photos": "\n".join(r.get("photos") or []),
            "photo_count": len(r.get("photos") or []),
            "last_seen": r.get("seen_at"), "scraped_at": r.get("seen_at"),
        }
        row["red_flags"] = red_flags(row["observations"], None)
        row["excise_eur"] = excise_eur(row["cc"], row["fuel"] or "", first_reg)
        row["landed_eur"] = (landed_eur(price, row["cc"], row["fuel"] or "", first_reg)
                             if price else None)
        cols = [c for c in row if c != "photos"] + ["photos"]
        con.execute(f"INSERT OR REPLACE INTO lots ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        n += 1
    con.commit()
    return n


def cmd_import(folder: str) -> None:
    con = db()
    jsons = sorted(Path(folder).glob("*.json"))
    for j in jsons:
        try:
            n = import_harvest_json(con, j)
            print(f"  [harvest] {j.name}: {n} lots")
        except Exception as exc:                          # noqa: BLE001
            print(f"  !! {j.name}: {exc}")

    files = sorted(Path(folder).glob("*.htm*"))
    if not files:
        if jsons:
            print("\nimported — run --report")
        else:
            print(f"no .html or .json files in {folder}")
        return
    print(f"{len(files)} saved pages in {folder}\n")
    for f in files:
        html = f.read_text(encoding="utf-8", errors="replace")
        src = sniff_source(html, f)
        if src == "vpauto" and "/vehicule/" in html:
            m = re.search(r"/vehicule/[0-9a-f]+/[a-z0-9\-]+", html)
            row = parse_lot(html, m.group(0) if m else f"/vehicule/local/{f.stem}")
        else:
            row = parse_generic(html, f)
        row.setdefault("cote", None)
        row.setdefault("mise_a_prix", None)
        row["source"] = src
        cols = [c for c in row if c != "photos"] + ["photos"]
        con.execute(
            f"INSERT OR REPLACE INTO lots ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", [row[c] for c in cols])
        con.commit()
        ex = excise_eur(row.get("cc"), row.get("fuel") or "", row.get("first_reg"))
        print(f"  [{src:7s}] {(row['title'] or '')[:44]:46.46s} "
              f"cc={row.get('cc') or '?':>5} km={row.get('km') or '?':>7} "
              f"reg={row.get('first_reg') or '?':>10} accize="
              f"{f'{ex:,.0f}' if ex is not None else '?':>7}")
        if src == "unknown":
            print(f"            amounts found: {row['observations']}")
    print("\nimported — run --report")


def cmd_photos(lot_id: str) -> None:
    con = db()
    row = con.execute("SELECT title, photos FROM lots WHERE lot_id=?", (lot_id,)).fetchone()
    if not row:
        print(f"lot {lot_id} not in db")
        return
    out = PHOTOS / lot_id
    out.mkdir(parents=True, exist_ok=True)
    urls = [u for u in (row[1] or "").split("\n") if u]
    print(f"{row[0]} — {len(urls)} photos -> {out}")
    for i, u in enumerate(urls, 1):
        dest = out / f"{i:02d}_{u.rsplit('/', 1)[-1]}"
        if dest.exists():
            continue
        req = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            dest.write_bytes(r.read())
        print(f"  {dest.name}")
        time.sleep(0.4)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--search", nargs="*", metavar="BRAND",
                    help="brands to sweep, e.g. Renault Peugeot Nissan")
    ap.add_argument("--watch", nargs="*", metavar="BRAND",
                    help="re-poll these brands until the sale closes")
    ap.add_argument("--pages", type=int, default=4, help="listing pages per brand")
    ap.add_argument("--detail", type=int, default=25,
                    help="how many priced lots to fetch detail pages for")
    ap.add_argument("--minutes", type=int, default=120, help="--watch duration")
    ap.add_argument("--every", type=int, default=180, help="--watch poll interval, seconds")
    ap.add_argument("--report", action="store_true", help="ranked buy list from the db")
    ap.add_argument("--import-dir", metavar="DIR",
                    help="parse .html pages you saved from your own browser "
                         "(Alcopa and anything else) into the same database")
    ap.add_argument("--photos", metavar="LOT_ID", help="download one lot's photos")
    ap.add_argument("--refresh", nargs="?", const=0, type=int, metavar="N",
                    help="complete card-level lots (no photos / no cc) by "
                         "fetching their detail pages; optional max count")
    args = ap.parse_args()

    if args.refresh is not None:
        cmd_refresh(args.refresh)
    elif args.search is not None:
        cmd_search(args.search, args.pages, args.detail)
    elif args.watch is not None:
        cmd_watch(args.watch, args.pages, args.minutes, args.every)
    elif args.import_dir:
        cmd_import(args.import_dir)
    elif args.report:
        cmd_report()
    elif args.photos:
        cmd_photos(args.photos)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
