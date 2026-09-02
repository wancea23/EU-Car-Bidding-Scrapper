#!/usr/bin/env python3
"""Local catalogue for the scraped VPauto lots.

    python serve_cars.py                 # http://localhost:8020  (and on the LAN)
    python serve_cars.py --port 8030
    python serve_cars.py --shipping 1200 # re-price everything at a different freight cost

Serves every row in data/vpauto.db. 142 lots carry full detail (photos, cc,
registration date, cote); the rest are list-card rows and show what the card
published. Excise and landed cost are computed live through the same functions
the scraper uses, so changing --shipping re-prices the whole catalogue.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "vpauto.db"
MD_DB = HERE / "data" / "md_demand_reference.db"

# reuse the scraper's verified fiscal model rather than restating it
_spec = importlib.util.spec_from_file_location("vp", HERE / "vpauto_scrape.py")
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)

_fspec = importlib.util.spec_from_file_location("fr_en", HERE / "fr_en.py")
fr = importlib.util.module_from_spec(_fspec)
_fspec.loader.exec_module(fr)

# ------------------------------------------------------------------ mapping
FUEL_MAP = {
    "diesel": "diesel", "essence": "petrol", "essence hybride": "hybrid",
    "diesel hybride": "hybrid", "fh": "hybrid", "electricite": "ev",
    "essence / gpl": "lpg", "essence / gnv": "cng",
    "electricite / gazole": "phev", "hybride rechargeable": "phev",
}
YEAR_BANDS = [(2006, 2010, "2006-2010"), (2011, 2013, "2011-2013"),
              (2014, 2015, "2014-2015"), (2016, 2017, "2016-2017"),
              (2018, 2019, "2018-2019"), (2020, 2021, "2020-2021"),
              (2022, 2023, "2022-2023"), (2024, 9999, "2024+")]


def cc_band(cc: int | None) -> str | None:
    """Match the MD database's convention: displacement rounded to 0.1 L."""
    if not cc:
        return None
    litres = round(cc / 100) / 10
    if litres <= 1.2:
        return "<=1.2"
    if litres <= 1.4:
        return "1.3-1.4"
    if litres <= 1.5:
        return "1.5"
    if litres <= 1.8:
        return "1.6-1.8"
    if litres <= 2.0:
        return "1.9-2.0"
    if litres <= 2.5:
        return "2.1-2.5"
    if litres <= 3.0:
        return "2.6-3.0"
    return ">3.0"


def year_band(year: int | None) -> str | None:
    for lo, hi, label in YEAR_BANDS:
        if year and lo <= year <= hi:
            return label
    return None


def slug_parts(url: str) -> tuple[str, str]:
    """Make and model out of a lot URL, for either site.

    VPauto:  /vehicule/<hex>/kia-sportage-16-crdi-...
    Alcopa:  /voiture-occasion/kia/sportage-16-crdi-...-1100675
             (also /utilitaire-occasion/, and locale-prefixed variants such as
             /ro/voiture-second-hand/...)

    Alcopa puts the make in its own path segment. Matching only the VPauto
    shape left make and model empty for every Alcopa lot, which silently
    emptied the make dropdown, broke the make filter, and stopped the MD
    comparison joining at all.
    """
    u = url or ""
    m = re.search(r"/vehicule/[0-9a-f]+/([^/?#]+)", u)
    if m:
        slug = m.group(1)
        # Split on the FIRST hyphen only and a two-word marque loses its second
        # half: "alfa-romeo-junior..." became make "Alfa" while the same brand
        # from Alcopa (which has its own path segment) came through as
        # "Alfa-Romeo". The dropdown then listed both, each holding half the
        # cars, with nothing to say they were the same marque.
        for two in ("alfa-romeo", "land-rover", "mercedes-benz", "aston-martin",
                    "rolls-royce", "great-wall", "ssang-yong", "dr-automobiles"):
            if slug.startswith(two + "-"):
                return two, slug[len(two) + 1:]
        head, _, rest = slug.partition("-")
        # VPauto writes "mercedes-classe-a" where Alcopa writes "mercedes-benz",
        # so the same marque still arrived under two dropdown entries.
        return {"mercedes": "mercedes-benz", "vw": "volkswagen"}.get(head, head), rest
    m = re.search(r"/(?:[a-z]{2}/)?(?:voiture|utilitaire|vehicule)[a-z-]*/"
                  r"([^/?#]+)/([^/?#]+)", u)
    if m:
        make, slug = m.group(1), m.group(2)
        # strip the numeric lot id the slug always ends with
        return make, re.sub(r"-\d+$", "", slug)
    return "", ""


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ------------------------------------------------------------------ MD join
_MD_CELLS: list[dict] = []


def load_md() -> None:
    if not MD_DB.exists():
        return
    con = sqlite3.connect(MD_DB)
    con.row_factory = sqlite3.Row
    for r in con.execute("SELECT * FROM md_reference"):
        d = dict(r)
        d["_make"] = norm(d["make"])
        d["_model"] = norm(d["model"])
        _MD_CELLS.append(d)
    con.close()


def md_match(url: str, year: int | None, cc: int | None, fuel: str | None) -> dict | None:
    """Find the Moldovan reference cell for a lot. Longest model match wins."""
    make, rest = slug_parts(url)
    if not make:
        return None
    yb, cb = year_band(year), cc_band(cc)
    fu = FUEL_MAP.get((fuel or "").strip().lower())
    nmake, nrest = norm(make), norm(rest)
    best = None
    for c in _MD_CELLS:
        if c["_make"] != nmake or not nrest.startswith(c["_model"]):
            continue
        if yb and c["year_band"] != yb:
            continue
        if cb and c["cc_band"] != cb:
            continue
        if fu and c["fuel"] != fu:
            continue
        if best is None or len(c["_model"]) > len(best["_model"]):
            best = c
    return best


# ------------------------------------------------------------------ loading
def fmt_left(secs: float) -> str:
    """A closing clock people can read at a glance, not a duration dump.

    Precision follows urgency: days out, nobody cares about seconds; inside the
    last minute, seconds are the only thing that matters.
    """
    secs = int(abs(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ------------------------------------------------------------------- saved
# A shortlist kept in a plain JSON file rather than in the lots database. The
# scrapers hold that database open and write to it while the site is being
# browsed, and a saved lot is his own note — it should not be at risk from a
# re-harvest, nor should a click here ever contend for a write lock with a
# scrape that is capturing a closing price.
SAVED_PATH = Path(__file__).parent / "data" / "saved.json"
_saved_lock = threading.Lock()


def saved_ids() -> set[str]:
    try:
        return set(json.loads(SAVED_PATH.read_text(encoding="utf-8")))
    except Exception:                                   # noqa: BLE001
        return set()


def saved_toggle(lot_id: str) -> bool:
    with _saved_lock:
        ids = saved_ids()
        on = lot_id not in ids
        ids.add(lot_id) if on else ids.discard(lot_id)
        SAVED_PATH.parent.mkdir(parents=True, exist_ok=True)
        SAVED_PATH.write_text(json.dumps(sorted(ids)), encoding="utf-8")
        return on


def price_of(r: dict) -> tuple[float | None, str]:
    """The most meaningful money on the row, and what it means."""
    # A VPauto detail page states the outcome outright ("Véhicule adjugé", or
    # "n'a pas été adjugé"). That beats the listing card, which keeps showing
    # the opening price long after the sale has run — which is how a lot that
    # made 5 400 was still being displayed as "OPENING 5 400".
    if r.get("sold_status") == "adjuge" and r.get("sale_price"):
        return r["sale_price"], "hammer"
    if r.get("sale_price"):
        return r["sale_price"], "hammer"
    if r.get("current_bid"):
        # Alcopa's data-current-price carries the OPENING price until somebody
        # actually bids — the page labels that very number "Mise a prix".
        # Treating it as a live bid put "LIVE / live bid" on ~2 030 lots that
        # had never received one. Equal to the opening price means no bid yet.
        if r.get("mise_a_prix") and r["current_bid"] == r["mise_a_prix"]:
            return r["current_bid"], "opening"
        return r["current_bid"], "live bid"
    if r.get("mise_a_prix"):
        return r["mise_a_prix"], "opening"
    return None, "—"


def build_rows() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = []
    for raw in con.execute("SELECT * FROM lots"):
        r = dict(raw)
        make, rest = slug_parts(r.get("url", ""))
        year = r.get("card_year")
        if not year and r.get("first_reg"):
            m = re.search(r"(\d{4})", r["first_reg"])
            year = int(m.group(1)) if m else None
        km = r.get("km") or r.get("card_km")
        cc, fuel = r.get("cc"), r.get("fuel")
        price, kind = price_of(r)

        excise = landed = None
        if price and cc and fuel:
            try:
                excise = vp.excise_eur(cc, fuel, r.get("first_reg"))
                landed = vp.landed_eur(price, cc, fuel, r.get("first_reg"))
            except Exception:
                pass

        cote = r.get("cote")
        discount = round(100 * (1 - price / cote), 1) if price and cote else None

        cell = md_match(r.get("url", ""), year, cc, fuel)
        md_price = cell["price_median"] if cell else None
        margin = None          # recomputed below, once landed is source-aware

        photos = [p for p in (r.get("photos") or "").split("\n") if p.strip()]
        title = r.get("title") or (make + " " + rest.replace("-", " ")).strip().title()

        obs_raw = r.get("observations")
        warns = fr.warnings_for(html.unescape(obs_raw) if obs_raw else None)
        blocked = any(sev == "block" for _, sev in warns)
        try:
            details = json.loads(r.get("details_json") or "{}")
        except Exception:
            details = {}
        try:
            equipment = json.loads(r.get("equipment_json") or "[]")
        except Exception:
            equipment = []

        row = {
            "id": r["lot_id"], "url": r.get("url"), "title": title,
            "make": make.title(), "year": year, "km": km, "cc": cc, "fuel": fuel,
            "gearbox": r.get("gearbox"), "power": r.get("power_hp"),
            "euro": r.get("euro_norm"), "book": r.get("service_book"),
            "location": r.get("location"), "first_reg": r.get("first_reg"),
            # The detail page's verdict wins over the listing card's state:
            # the card goes stale and keeps a sold lot looking open.
            "state": ("adjuge" if r.get("sold_status") == "adjuge"
                      else "non_adjuge" if r.get("sold_status") == "non_adjuge"
                      else r.get("sale_state")),
            "sold_status": r.get("sold_status"),
            "price": price, "price_kind": kind,
            "cote": cote, "prix_neuf": r.get("prix_neuf"), "discount": discount,
            "excise": round(excise) if excise else None,
            "landed": round(landed) if landed else None,
            "md_price": md_price, "md_n": cell["n_sold"] if cell else None,
            "md_cell": (f'{cell["make"]} {cell["model"]} {cell["year_band"]} '
                        f'{cell["cc_band"]} {cell["fuel"]}') if cell else None,
            "md_sell": cell["sell_through_pct"] if cell else None,
            "excise_ok": bool(cell["excise_disp_ok"]) if cell else (
                bool(cc and cc <= 1500 and fuel and "diesel" in fuel.lower())),
            "margin": margin,
            "flags": r.get("red_flags"), "obs": obs_raw,
            "obs_en": fr.obs_en(obs_raw), "warns": warns, "blocked": blocked,
            "details": details, "equipment": equipment,
            "damage_img": r.get("damage_img"), "ct_pdf": r.get("ct_pdf"),
            "se_pdf": r.get("se_pdf"),
            "tva": r.get("tva_recup"), "photos": photos, "nphotos": len(photos),
            # lot_id is prefixed at write time, so it stays right even for the
            # older rows stored before the `source` column existed
            "source": (r.get("source")
                       or ("alcopa" if str(r["lot_id"]).startswith("alcopa:")
                           else "vpauto")),
            # Alcopa quotes an opening price and no cote, so the card shows
            # "start" where a VPauto card shows "vs cote".
            "start": r.get("mise_a_prix"),
            "fees": r.get("fees"),
            "vin": r.get("vin"), "colour": r.get("colour"),
            "co2": r.get("co2"), "body": r.get("body"),
            "ends_ts": r.get("ends_ts"),
        }
        # Landed cost comes from the SAME stack the detail page prints, not
        # from vp.landed_eur(). That helper charges VPauto's 200 EUR dossier
        # fee to every row and knows nothing of Alcopa's 14.40% buyer's
        # premium, so the grid understated an Alcopa en_sus lot by ~1 100 EUR
        # while the detail page had it right — and "MD margin" is the column
        # the whole grid is ranked by. One source of truth now.
        try:
            _, row["landed"] = cost_lines(row)
        except Exception:                                   # noqa: BLE001
            pass
        if row["landed"]:
            row["landed"] = round(row["landed"])
        row["margin"] = (round(md_price - row["landed"])
                         if (md_price and row["landed"]) else None)
        out.append(row)
    con.close()
    return out


ROWS: list[dict] = []
_STATE = {"mtime": 0.0, "checked": 0.0}


def refresh_if_stale(min_gap: float = 20.0) -> None:
    """Pick up rows written by a scrape that is still running, without
    rebuilding on every request (the enricher commits every few seconds)."""
    import time
    now = time.time()
    if now - _STATE["checked"] < min_gap:
        return
    _STATE["checked"] = now
    # The database runs in WAL mode, so a scrape's writes land in vpauto.db-wal
    # and the main file's mtime does not move until a checkpoint. Watching only
    # the main file therefore made the site serve stale rows for as long as a
    # backfill ran — measured at 26 minutes behind while two were writing, and
    # exactly the "prices don't update" symptom. Take the newest of the set.
    try:
        mtime = max(p.stat().st_mtime
                    for p in (DB, DB.with_name(DB.name + "-wal"),
                              DB.with_name(DB.name + "-shm"))
                    if p.exists())
    except (OSError, ValueError):
        return
    if mtime == _STATE["mtime"]:
        return
    _STATE["mtime"] = mtime
    fresh = build_rows()
    ROWS[:] = fresh


# ------------------------------------------------------------------ filtering
def apply_filters(rows: list[dict], q: dict) -> list[dict]:
    def g(k, d=""):
        return (q.get(k, [d])[0] or "").strip()

    text = g("q").lower()
    if text:
        # search the model slug too — "berlingo" and "1.5 dci" live there,
        # not in the title
        rows = [r for r in rows
                if text in (r["title"] or "").lower()
                or text in (r.get("model") or "").lower()]

    # Multi-select: a comma-separated list means OR within the field, AND
    # across fields — the 999 scrapper's model. Single values still work, so
    # every existing bookmarked URL keeps behaving the same.
    def multi(key, get, lower=True):
        raw = g(key)
        if not raw:
            return None
        vals = {v.strip().lower() if lower else v.strip()
                for v in raw.split(",") if v.strip()}
        return vals or None

    for key, getter in (
            ("src", lambda r: (r.get("source") or "vpauto")),
            ("state", lambda r: r["state"]),
            ("fuel", lambda r: r["fuel"]),
            ("make", lambda r: r["make"]),
            ("gearbox", lambda r: r.get("gearbox")),
            ("location", lambda r: r.get("location")),
            ("fees", lambda r: r.get("fees")),
            ("body", lambda r: r.get("body")),
    ):
        vals = multi(key, getter)
        if vals:
            rows = [r for r in rows if (getter(r) or "").lower() in vals]
    # Carrosserie-code exclusions, ON unless explicitly switched off ("0").
    # These are noise for a car-import play: a trailer has no engine to tax and
    # a specialised body will not resell as a passenger car in Moldova.
    VAN_CODES = {"ctte", "vu"}                       # camionnette, utilitaire
    SPECIAL_CODES = {"vasp", "rem", "resp", "srem",  # specialised, trailers
                     "tra", "cam", "trr"}            # tractors, trucks
    if g("vans", "0") != "1":
        rows = [r for r in rows if (r.get("body") or "").lower() not in VAN_CODES]
    if g("special", "0") != "1":
        rows = [r for r in rows
                if (r.get("body") or "").lower() not in SPECIAL_CODES]

    if g("photos") == "1":
        rows = [r for r in rows if r["nphotos"]]
    if g("clean") == "1":
        rows = [r for r in rows if not r["flags"]]
    if g("excise") == "1":
        rows = [r for r in rows if r["excise_ok"]]
    if g("exportable") == "1":
        rows = [r for r in rows if not r["blocked"]]
    if g("docs") == "1":
        rows = [r for r in rows if r["ct_pdf"] or r["se_pdf"]]
    if g("md") == "1":
        rows = [r for r in rows if r["md_price"]]
    # Closed vs still open, straight off the clock rather than off sale_state —
    # a lot can sit at "en_cours" long after its sale has actually run.
    if g("closed") == "1":
        rows = [r for r in rows if r.get("ends_ts") and r["ends_ts"] <= time.time()]
    if g("opennow") == "1":
        rows = [r for r in rows if r.get("ends_ts") and r["ends_ts"] > time.time()]
    if g("saved") == "1":
        keep = saved_ids()
        rows = [r for r in rows if str(r["id"]) in keep]

    # Min/max on every numeric field, the 999 scrapper's <field>_min /
    # <field>_max scheme. What was here before was an ad-hoc minpx / maxpx /
    # mindisc / minmargin quartet: three fields, with an upper bound on only
    # one of them. You could not ask for "2018 to 2021" or "under 150 000 km",
    # which is how anyone actually shops for a car to import. All four old
    # names still work, folded into the same machinery, so saved URLs and the
    # JSON links keep behaving exactly as before.
    RANGES = {"price": "price", "landed": "landed", "year": "year", "km": "km",
              "cc": "cc", "power": "power", "margin": "margin",
              "discount": "discount", "mdprice": "md_price"}

    def num(v):
        """Tolerate what people actually type: '150 000', '12,5', ''."""
        try:
            return float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return None

    bounds: dict[str, list] = {}
    for base, field in RANGES.items():
        lo, hi = num(g(f"{base}_min")), num(g(f"{base}_max"))
        if lo is not None or hi is not None:
            bounds[field] = [lo, hi]
    for old, field, side in (("minpx", "price", 0), ("maxpx", "price", 1),
                             ("mindisc", "discount", 0), ("minmargin", "margin", 0)):
        v = num(g(old))
        if v is not None:
            bounds.setdefault(field, [None, None])[side] = v

    for field, (lo, hi) in bounds.items():
        kept = []
        for r in rows:
            v = num(r.get(field))
            # A lot carrying no value for the field cannot satisfy a bound, so
            # it drops out — the same as the code this replaces. Worth knowing
            # for cc and margin, which plenty of lots simply do not have.
            if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
                continue
            kept.append(r)
        rows = kept

    sort = g("sort", "discount")
    rev = g("dir", "desc") != "asc"
    rows = sorted(rows, key=lambda r: (r.get(sort) is None, r.get(sort) or 0), reverse=rev)
    if rev:  # keep the missing values at the bottom either way
        rows = [r for r in rows if r.get(sort) is not None] + \
               [r for r in rows if r.get(sort) is None]
    return rows


# ------------------------------------------------------------------ markup
def page_shell(body: str, title: str) -> bytes:
    return (STYLE_HEAD.replace("{{TITLE}}", html.escape(title)) + body + "</body></html>").encode()


STYLE_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>
:root{--bg:#0F1311;--surf:#171E1B;--surf2:#1F2723;--ink:#E4EAE6;--ink2:#B4C0BA;
 --mut:#8A9992;--rule:#26302C;--acc:#5CBDB4;--good:#6FBF7F;--warn:#D9A54A;--crit:#E0705F}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:1500px;margin:0 auto;padding:0 18px}
header{border-bottom:1px solid var(--rule);background:var(--surf);position:sticky;top:0;z-index:30}
header .wrap{display:flex;align-items:center;gap:16px;height:56px;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:-.02em;font-size:17px;white-space:nowrap}
.brand span{color:var(--acc)}
.count{color:var(--mut);font-size:13px;white-space:nowrap}
form.filters{display:flex;gap:7px;flex-wrap:wrap;align-items:center;padding:13px 0}
input,select{background:var(--surf2);border:1px solid var(--rule);color:var(--ink);
 border-radius:7px;padding:7px 9px;font:13px "IBM Plex Sans",sans-serif}
input:focus,select:focus{outline:2px solid var(--acc);outline-offset:0}
input[type=number]{width:104px}
input[name=q]{width:210px}
label.chk{display:flex;align-items:center;gap:5px;font-size:12.5px;color:var(--ink2);
 background:var(--surf2);border:1px solid var(--rule);border-radius:7px;padding:6px 10px;cursor:pointer}
label.chk input{accent-color:var(--acc)}
button{background:var(--acc);color:#06201E;border:0;border-radius:7px;padding:8px 15px;
 font:600 13px "IBM Plex Sans",sans-serif;cursor:pointer}
button.ghost{background:var(--surf2);color:var(--ink2);border:1px solid var(--rule)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(288px,1fr));gap:14px;padding:6px 0 40px}
.card{background:var(--surf);border:1px solid var(--rule);border-radius:11px;overflow:hidden;
 display:flex;flex-direction:column;transition:border-color .15s}
.card:hover{border-color:var(--acc)}
.thumb{aspect-ratio:4/3;background:var(--surf2);position:relative;overflow:hidden;display:block}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .none{position:absolute;inset:0;display:flex;flex-direction:column;
 align-items:center;justify-content:center;gap:5px;
 color:var(--mut);font-size:12px;text-align:center;padding:16px;
 background:repeating-linear-gradient(135deg,var(--surf2) 0 12px,#1a211d 12px 24px)}
.thumb .none svg{width:74px;height:auto;fill:none;stroke:var(--mut);stroke-width:1.6;
 stroke-linecap:round;stroke-linejoin:round;opacity:.5;margin-bottom:2px}
.thumb .none b{font-size:12.5px;font-weight:600;color:var(--ink2);letter-spacing:.01em}
.thumb .none span{font-size:11px;line-height:1.45;max-width:23ch;opacity:.75}
.thumb .none em{font-size:10px;font-style:normal;letter-spacing:.06em;text-transform:uppercase;
 opacity:.5;margin-top:3px}
.thumb .none.prep svg{stroke:#D9A54A;opacity:.55}
.thumb .none.prep b{color:#D9A54A}
.badges{position:absolute;top:8px;left:8px;display:flex;gap:5px;flex-wrap:wrap}
.b{font-family:"IBM Plex Mono",monospace;font-size:10px;padding:3px 7px;border-radius:999px;
 font-weight:500;backdrop-filter:blur(4px)}
.b-sold{background:#6FBF7FE0;color:#06200C}
.b-live{background:#D9A54AE0;color:#241800}
.b-open{background:#1F2723E0;color:var(--ink2)}
.b-flag{background:#E0705FE0;color:#2A0A06}
.b-exc{background:#5CBDB4E0;color:#06201E}
.b-block{background:#B3261EF2;color:#fff;font-weight:700}
.b-doc{background:#3C4A44E0;color:#CFDAD4}
.card.blocked{opacity:.62}
.card.blocked:hover{opacity:1}
.warns{display:flex;flex-direction:column;gap:6px}
.wn{display:flex;gap:9px;align-items:flex-start;font-size:13px;padding:8px 11px;border-radius:7px;
 border-left:3px solid var(--rule);background:var(--surf2)}
.wn b{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.06em;padding-top:2px}
.wn.block{border-left-color:#E0705F;background:#E0705F1F}
.wn.severe{border-left-color:#E0705F}
.wn.warn{border-left-color:var(--warn)}
.wn.info{border-left-color:var(--mut)}
.eq{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:5px 14px;font-size:13px}
.eq div{color:var(--ink2)}
.eq div::before{content:"·";color:var(--acc);margin-right:6px}
.docs{display:flex;flex-direction:column;gap:8px}
.docs a{display:flex;gap:9px;align-items:center;background:var(--surf2);border:1px solid var(--rule);
 border-radius:7px;padding:10px 12px;font-size:13.5px;font-weight:500}
.docs a:hover{border-color:var(--acc)}
.dmg{width:100%;border-radius:7px;background:#fff;padding:6px}
/* --- source identity: alcopa vs vpauto, told apart at a glance --- */
.src{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.4px;
  text-transform:uppercase;padding:2px 7px;border-radius:99px;border:1px solid}
.src-vpauto{color:#7FB3E0;border-color:#7FB3E080;background:#7FB3E015}
.src-alcopa{color:#E8A03C;border-color:#E8A03C80;background:#E8A03C15}
.card.is-alcopa{border-left:3px solid #E8A03C}
.card.is-vpauto{border-left:3px solid #7FB3E0}
.pager{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px;align-items:center;
  justify-content:center;padding:22px 0 6px}
.pager a{padding:6px 12px;border:1px solid var(--rule);border-radius:6px;font-size:13px;
  color:var(--mut);text-decoration:none;background:var(--surf2)}
.pager a:hover{border-color:var(--acc);color:var(--acc)}
.pager a.on{background:var(--acc);color:#06201E;border-color:var(--acc);font-weight:600}
.pager .gap{color:var(--mut);padding:0 2px}
.pager .pginfo{margin-left:10px;color:var(--mut);font-size:12px;font-family:"IBM Plex Mono",monospace}
.b-prep{background:#8B6D3F22;color:#D9A54A;border:1px solid #D9A54A66}
/* --- photo lightbox --- */
#lbox{position:fixed;inset:0;background:#000000ee;display:none;z-index:70;
  align-items:center;justify-content:center}
#lbox.on{display:flex}
#lbimg{max-width:92vw;max-height:88vh;object-fit:contain;border-radius:6px;
  box-shadow:0 10px 50px #000}
#lbox button{position:absolute;background:#ffffff14;border:1px solid #ffffff2e;color:#fff;
  cursor:pointer;border-radius:50%;width:46px;height:46px;font-size:26px;line-height:1;
  display:flex;align-items:center;justify-content:center;transition:background .12s}
#lbox button:hover{background:#ffffff2e}
#lbclose{top:18px;right:20px;font-size:24px}
#lbprev{left:18px}#lbnext{right:18px}
#lbcount{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);color:#cfd6d3;
  font:12px "IBM Plex Mono",monospace;background:#0009;padding:5px 12px;border-radius:99px}
@media(max-width:600px){#lbox button{width:38px;height:38px}#lbprev{left:6px}#lbnext{right:6px}}
.hero,.gal a{cursor:zoom-in}
/* --- per-fee explanation --- */
.ci{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;
  margin-left:6px;border-radius:50%;border:1px solid var(--mut);color:var(--mut);
  font-size:9px;font-style:italic;font-weight:700;cursor:help;vertical-align:middle;
  position:relative;font-family:Georgia,serif}
.ci:hover,.ci:focus{border-color:var(--acc);color:var(--acc);outline:none}
/* the "=" twin of "i": what arithmetic produced this number */
.ci.eq{font-family:"IBM Plex Mono",monospace;font-style:normal;font-size:10px;
  margin-left:3px;border-color:#3f5a54;color:var(--acc)}
.ci.eq::after{white-space:pre-line;font-family:"IBM Plex Mono",monospace;
  font-size:11px;width:320px}
.ci::after{content:attr(data-info);white-space:pre-line;position:absolute;bottom:150%;left:50%;
  transform:translateX(-50%);width:290px;background:#0d1117;color:#c9d1d9;
  border:1px solid var(--rule);border-radius:7px;padding:9px 11px;font-size:11.5px;
  font-style:normal;font-weight:400;line-height:1.5;text-align:left;
  opacity:0;visibility:hidden;transition:opacity .12s;z-index:60;pointer-events:none;
  box-shadow:0 6px 22px #000a;font-family:inherit}
.ci:hover::after,.ci:focus::after{opacity:1;visibility:visible}
@media(max-width:600px){.ci::after{width:210px;left:auto;right:0;transform:none}}
/* --- clickable damage diagram --- */
.dz-wrap{background:#fff;border-radius:7px;padding:8px;display:flex;gap:8px;flex-wrap:wrap}
.dz-wrap svg{flex:1 1 46%;min-width:200px;max-width:100%;height:auto}
.dz-legend{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.dz-key{display:inline-flex;align-items:center;gap:5px;font-size:11px;
  background:var(--surf2);border:1px solid var(--rule);border-radius:5px;padding:3px 7px}
.dz-key i{width:9px;height:9px;border-radius:2px;display:inline-block}
/* The modal sat on a surface one shade off the page behind it, with an
   undefined --dim for its subtitle and close button, so it read as part of the
   page rather than on top of it. Now: a heavier scrim that blurs the page, a
   lifted panel with a real shadow and accent edge, and a sticky header so the
   close control is findable however far you scroll inside it. */
#dzmodal{position:fixed;inset:0;background:#050807e8;display:none;z-index:50;
  align-items:center;justify-content:center;padding:20px;
  backdrop-filter:blur(4px) saturate(.7)}
#dzmodal.on{display:flex;animation:dzin .14s ease-out}
@keyframes dzin{from{opacity:0}to{opacity:1}}
#dzbox{background:var(--surf2);border:1px solid #3b4a45;border-top:3px solid var(--acc);
  border-radius:12px;max-width:820px;width:100%;max-height:88vh;overflow:auto;
  padding:0 18px 18px;box-shadow:0 24px 70px #000c,0 0 0 1px #0008;
  animation:dzup .16s ease-out}
@keyframes dzup{from{transform:translateY(10px)}to{transform:none}}
#dzbox h4{margin:0 0 3px;font-size:17px;letter-spacing:-.01em}
#dzbox .t{color:var(--mut);font-size:12px;margin-bottom:11px}
#dzbox img{width:100%;border-radius:6px;margin-bottom:9px}
/* keeps the title and the X visible while the body scrolls under them */
#dzbox>h4,#dzbox>#dzclose{position:sticky;top:0;background:var(--surf2);
  padding-top:16px;z-index:2}
#dzclose{float:right;cursor:pointer;color:var(--mut);font-size:20px;line-height:1;
  background:none;border:0}
.langbar{display:flex;gap:5px;margin-left:auto}
.langbar a{font-size:11.5px;padding:4px 10px;border-radius:6px;background:var(--surf2);
 border:1px solid var(--rule);color:var(--mut);font-family:"IBM Plex Mono",monospace}
.langbar a.on{background:var(--acc);color:#06201E;border-color:var(--acc);font-weight:600}
.body{padding:12px 13px 13px;display:flex;flex-direction:column;gap:9px;flex:1}
.t{font-weight:600;font-size:13.5px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;
 -webkit-box-orient:vertical;overflow:hidden;min-height:37px}
.spec{color:var(--mut);font-size:11.5px;display:flex;gap:8px;flex-wrap:wrap}
.money{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:auto}
.mv{background:var(--surf2);border-radius:7px;padding:7px 9px}
.mv .k{font-size:9.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--mut)}
.mv .v{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
 font-size:15px;font-weight:500;margin-top:2px}
.v.good{color:var(--good)}.v.crit{color:var(--crit)}.v.acc{color:var(--acc)}
.empty{padding:70px 20px;text-align:center;color:var(--mut)}
/* detail */
.det{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(300px,1fr);gap:26px;padding:24px 0 60px}
.gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:7px}
.gal img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:7px;background:var(--surf2);cursor:zoom-in}
.hero{width:100%;border-radius:11px;margin-bottom:9px;background:var(--surf2);display:block}
.panel{background:var(--surf);border:1px solid var(--rule);border-radius:11px;padding:17px;margin-bottom:14px}
.panel h3{margin:0 0 12px;font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);font-weight:600}
table.kv{width:100%;border-collapse:collapse;font-size:13.5px}
table.kv td{padding:6px 0;border-bottom:1px solid var(--rule)}
table.kv tr:last-child td{border-bottom:0}
table.kv td:first-child{color:var(--mut);width:47%}
table.kv td:last-child{text-align:right;font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
tr.tot td{font-weight:700;color:var(--ink);border-top:2px solid var(--rule);padding-top:9px}
.note{font-size:12px;color:var(--mut);margin-top:11px;line-height:1.5}
h1.dt{font-size:23px;margin:0 0 5px;letter-spacing:-.02em;line-height:1.25}
.sub{color:var(--mut);font-size:13px;margin-bottom:16px}
.back{font-size:13px;display:inline-block;margin:16px 0 0}
@media(max-width:900px){.det{grid-template-columns:1fr}}
/* --- price history --- */
.hist-head{display:flex;gap:22px;flex-wrap:wrap;margin:2px 0 12px}
.hist-head span{display:flex;flex-direction:column}
.hist-head b{font-family:"IBM Plex Mono",monospace;font-size:17px;font-weight:600;
  font-variant-numeric:tabular-nums}
.hist-head em{font-style:normal;font-size:10.5px;color:var(--mut);
  text-transform:uppercase;letter-spacing:.05em;margin-top:1px}
.hist-head .good b{color:var(--good)}
.hist-head .crit b{color:var(--crit)}
svg.hist{width:100%;height:120px;display:block;background:var(--surf2);
  border:1px solid var(--rule);border-radius:7px}
svg.hist polyline{fill:none;stroke:var(--acc);stroke-width:2;
  stroke-linejoin:round;vector-effect:non-scaling-stroke}
svg.hist polygon{fill:var(--acc);opacity:.10}
svg.hist circle{fill:var(--acc)}
/* --- min/max range pairs --- */
.rng{display:inline-flex;align-items:center;gap:4px;background:var(--surf2);
  border:1px solid var(--rule);border-radius:7px;padding:3px 8px}
.rng b{font-size:10.5px;color:var(--mut);font-weight:600;text-transform:uppercase;
  letter-spacing:.04em;margin-right:2px;white-space:nowrap}
.rng em{color:var(--mut);font-style:normal}
.rng input{width:66px;padding:3px 5px;font-size:12.5px;background:var(--bg);
  border:1px solid var(--rule);border-radius:5px;color:var(--ink)}
/* --- closing clock --- */
.clock{display:flex;align-items:baseline;gap:7px;margin:7px 0 2px;font-size:12px;
  padding:4px 8px;border-radius:6px;border:1px solid var(--rule);background:var(--surf2)}
.clock span{color:var(--mut);text-transform:uppercase;font-size:10px;letter-spacing:.05em}
.clock b{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;
  font-size:13.5px;font-weight:600;color:var(--ink)}
.clock.open{border-color:#2f4a44}
.clock.open b{color:var(--acc)}
.clock.urgent{border-color:var(--warn);background:#2a1f0e}
.clock.urgent b{color:var(--warn)}
.clock.closed{border-color:#3a2c2c;background:#221a1a}
.clock.closed b{color:var(--mut)}
.clock.none{opacity:.45}
/* A closed lot is history, not a buying opportunity — mute the whole card so
   the live ones carry the eye, but keep its final price legible. */
.card.is-closed{opacity:.82}
.card.is-closed .thumb img{filter:grayscale(.55)}
.b-closed{background:#3a2c2c;color:#d6a9a2}
/* went to auction and found no buyer — not the same as never opened */
.b-unsold{background:#2f2a1c;color:#d9c48a}
.v.unsold{color:var(--warn)}
.v.final{color:var(--good);font-weight:700}
.v.start{color:var(--ink2)}
.v.soon{color:var(--mut);font-style:italic;font-size:14px}
/* --- save for later --- */
.card{position:relative}
.save{position:absolute;top:8px;right:8px;z-index:4;width:30px;height:30px;
  border-radius:50%;border:1px solid var(--rule);background:#0d1117cc;color:var(--mut);
  font-size:15px;line-height:1;cursor:pointer;backdrop-filter:blur(3px);transition:.12s}
.save:hover{color:var(--warn);border-color:var(--warn);transform:scale(1.08)}
.save.on{color:var(--warn);border-color:var(--warn);background:#2a1f0ecc}
</style></head><body>
<div id="lbox"><button id="lbclose" aria-label="close">&times;</button>
  <button id="lbprev" aria-label="previous">&#8249;</button>
  <img id="lbimg" alt="">
  <button id="lbnext" aria-label="next">&#8250;</button>
  <div id="lbcount"></div></div>
<div id="dzmodal"><div id="dzbox">
  <button id="dzclose" aria-label="close">&times;</button>
  <h4 id="dztitle"></h4><div class="t" id="dztype"></div><div id="dzpics"></div>
</div></div>
<script>
// Photo lightbox: the detail gallery is the only place with .hero/.gal, so
// collect those once and let the arrows and keyboard walk the same list.
(function(){
  var box=document.getElementById('lbox'), img=document.getElementById('lbimg'),
      cnt=document.getElementById('lbcount'), pics=[], at=0;
  function collect(){
    pics=[];
    var h=document.querySelector('img.hero'); if(h) pics.push(h.src);
    document.querySelectorAll('.gal a').forEach(function(a){ pics.push(a.href); });
    if(!pics.length) document.querySelectorAll('.gal img').forEach(function(i){ pics.push(i.src); });
  }
  function show(i){
    if(!pics.length) return;
    at=(i+pics.length)%pics.length;
    img.src=pics[at]; cnt.textContent=(at+1)+' / '+pics.length;
    box.classList.add('on');
  }
  function close(){ box.classList.remove('on'); img.src=''; }
  document.getElementById('lbclose').onclick=close;
  document.getElementById('lbprev').onclick=function(e){e.stopPropagation();show(at-1);};
  document.getElementById('lbnext').onclick=function(e){e.stopPropagation();show(at+1);};
  box.onclick=function(e){ if(e.target===box) close(); };
  document.addEventListener('keydown',function(e){
    if(!box.classList.contains('on')) return;
    if(e.key==='Escape') close();
    if(e.key==='ArrowLeft') show(at-1);
    if(e.key==='ArrowRight') show(at+1);
  });
  document.addEventListener('click',function(e){
    var h=e.target.closest && e.target.closest('img.hero, .gal a, .gal img');
    if(!h) return;
    e.preventDefault();
    collect();
    var src=h.tagName==='A'?h.href:h.src;
    var i=pics.indexOf(src);
    show(i<0?0:i);
  });
})();
// Damage zones: the svg element ids are Alcopa's own zone codes (CAPOT, PAVG...),
// so a click anywhere inside a zone walks up to the first id we have photos for.
(function(){
  var m=document.getElementById('dzmodal');
  function close(){ m.classList.remove('on'); }
  document.getElementById('dzclose').onclick=close;
  m.onclick=function(e){ if(e.target===m) close(); };
  document.addEventListener('keydown',function(e){ if(e.key==='Escape') close(); });
  document.addEventListener('click',function(e){
    var dz=window.__DZ; if(!dz) return;
    var n=e.target;
    while(n && n!==document){
      if(n.id && dz[n.id]){
        var d=dz[n.id];
        document.getElementById('dztitle').textContent=d.label;
        document.getElementById('dztype').textContent=
          d.type+' · '+d.photos.length+' photo'+(d.photos.length===1?'':'s');
        // build nodes instead of concatenating into innerHTML: a photo URL
        // carrying a quote would otherwise inject attributes
        var pics=document.getElementById('dzpics');
        pics.textContent='';
        d.photos.forEach(function(p){
          var im=document.createElement('img');
          im.loading='lazy'; im.src=p; pics.appendChild(im);
        });
        m.classList.add('on');
        return;
      }
      n=n.parentNode;
    }
  });
})();

// Live closing clocks. The server already rendered a correct value, so this
// only keeps it honest while the page is open — a card left on screen through
// a sale would otherwise still claim the lot closes in two minutes.
(function(){
  function fmt(s){
    s=Math.abs(Math.floor(s));
    var d=Math.floor(s/86400), h=Math.floor(s%86400/3600),
        m=Math.floor(s%3600/60), x=s%60;
    if(d) return d+'d '+String(h).padStart(2,'0')+'h';
    if(h) return h+'h '+String(m).padStart(2,'0')+'m';
    if(m) return m+'m '+String(x).padStart(2,'0')+'s';
    return x+'s';
  }
  function tick(){
    var now=Date.now()/1000;
    document.querySelectorAll('.clock[data-ends]').forEach(function(el){
      var ends=+el.dataset.ends, left=ends-now, b=el.querySelector('b'),
          k=el.querySelector('span');
      if(!b) return;
      if(left>0){
        el.className='clock open'+(left<3600?' urgent':'');
        k.textContent='closes in'; b.textContent=fmt(left);
      }else{
        // Crossing zero does not reveal the hammer price — that arrives with
        // the next scrape — so say the sale is over and leave the number be.
        el.className='clock closed';
        k.textContent='closed'; b.textContent=fmt(left)+' ago';
        el.closest('.card') && el.closest('.card').classList.add('is-closed');
      }
    });
  }
  tick(); setInterval(tick,1000);
})();

// Save for later. Server-side on purpose: the shortlist has to be the same
// list on the phone and on the desktop, which browser storage cannot do.
(function(){
  document.addEventListener('click',function(e){
    var b=e.target.closest('.save'); if(!b) return;
    e.preventDefault(); e.stopPropagation();
    b.disabled=true;
    fetch('/api/save?id='+encodeURIComponent(b.dataset.id),{method:'POST'})
      .then(function(r){return r.json();})
      .then(function(j){ b.classList.toggle('on', !!j.saved); })
      .catch(function(){ b.title='could not save — is the server still up?'; })
      .finally(function(){ b.disabled=false; });
  });
})();
</script>
"""


def money(v, cls="") -> str:
    if v is None:
        return '<span class="v mut">—</span>'
    return f'<span class="v {cls}">{v:,.0f}</span>'.replace(",", " ")


def card_html(r: dict, saved: set[str] | None = None) -> str:
    saved = saved or set()
    badge = {"adjuge": '<span class="b b-sold">SOLD</span>',
             "non_adjuge": '<span class="b b-unsold">UNSOLD</span>',
             # LIVE only when a bid actually exists — price_of() distinguishes
             # a real bid from Alcopa's opening price wearing the same field.
             "en_cours": ('<span class="b b-live">LIVE</span>'
                          if r["price_kind"] == "live bid"
                          else '<span class="b b-open">OPEN</span>'),
             "termine": '<span class="b b-closed">CLOSED</span>',
             "pending": '<span class="b b-prep">PRICE SOON</span>',
             "mise_a_prix": '<span class="b b-open">OPEN</span>',
             "estimation": '<span class="b b-open">EST</span>'}.get(r["state"], "")
    src = r.get("source") or "vpauto"
    badge = f'<span class="src src-{src}">{src}</span>' + badge
    if r["blocked"]:
        badge += '<span class="b b-block">CANNOT EXPORT</span>'
    elif r["flags"]:
        badge += f'<span class="b b-flag">{html.escape(r["flags"].split(",")[0])}</span>'
    if r["excise_ok"]:
        badge += '<span class="b b-exc">EXCISE OK</span>'
    if r["ct_pdf"]:
        badge += '<span class="b b-doc">MOT REPORT</span>'

    # An Alcopa lot with no photos is normally not a scrape failure: the car is
    # listed weeks early "En preparation" and Alcopa itself shows a placeholder.
    # Saying "no photos captured" blamed our scraper for the seller's state.
    if r["photos"]:
        thumb = f'<img src="{html.escape(r["photos"][0])}" loading="lazy" alt="">'
    elif src == "alcopa":
        thumb = (
            '<div class="none prep">'
            '<svg viewBox="0 0 64 30" aria-hidden="true">'
            '<path d="M3 22h4M57 22h4M8 22a4 4 0 1 0 8 0 4 4 0 1 0-8 0M48 22a4 4 0 1 0 8 0'
            ' 4 4 0 1 0-8 0M6 22c-2 0-3-1-3-3v-4c0-2 2-4 5-5l6-6c1-1 3-2 5-2h18c2 0 4 1 5 2'
            'l6 6c3 1 5 3 5 5v4c0 2-1 3-3 3M16 12h32"/>'
            '</svg>'
            '<b>In preparation</b>'
            '<span>Alcopa has not published photos for this lot yet</span>'
            '<em>re-checked automatically until they appear</em>'
            '</div>')
        badge += '<span class="b b-prep">IN PREP</span>'
    else:
        # Same story on the VPauto side: a lot can be listed before its detail
        # page exists, so the row is card-level only. That is a fetch still
        # owed, not a dead lot — say so and tag it for the revisit queue.
        thumb = ('<div class="none prep"><svg viewBox="0 0 64 30" aria-hidden="true">'
                 '<path d="M3 22h4M57 22h4M8 22a4 4 0 1 0 8 0 4 4 0 1 0-8 0M48 22a4 4 0 1 0 8 0'
                 ' 4 4 0 1 0-8 0M6 22c-2 0-3-1-3-3v-4c0-2 2-4 5-5l6-6c1-1 3-2 5-2h18c2 0 4 1 5 2'
                 'l6 6c3 1 5 3 5 5v4c0 2-1 3-3 3M16 12h32"/></svg>'
                 '<b>Detail not fetched yet</b>'
                 '<span>only the listing card was captured for this lot</span>'
                 '<em>queued for a revisit</em></div>')
        badge += '<span class="b b-prep">NEEDS REFETCH</span>'

    spec = " · ".join(x for x in [
        str(r["year"]) if r["year"] else None,
        f'{r["km"]:,} km'.replace(",", " ") if r["km"] else None,
        f'{r["cc"]} cc' if r["cc"] else None,
        r["fuel"] or None] if x)

    # Closing clock. Rendered server-side so the card is correct with no JS,
    # and carrying data-ends so the page can tick it live once loaded.
    ends, now = r.get("ends_ts"), time.time()
    expired = bool(ends and ends <= now)
    if not ends:
        clock = '<div class="clock none"><span>no closing time</span></div>'
    elif expired:
        clock = (f'<div class="clock closed" data-ends="{int(ends)}">'
                 f'<span>closed</span><b>{fmt_left(now - ends)} ago</b></div>')
    else:
        urgent = " urgent" if ends - now < 3600 else ""
        clock = (f'<div class="clock open{urgent}" data-ends="{int(ends)}">'
                 f'<span>closes in</span><b>{fmt_left(ends - now)}</b></div>')
    if expired:
        badge += '<span class="b b-closed">CLOSED</span>'

    dv = f'{r["discount"]}%' if r["discount"] is not None else "—"
    dcls = "good" if (r["discount"] or 0) >= 25 else "acc" if r["discount"] else ""

    # Three different meanings were all being rendered as an em dash:
    # "no price published yet", "bidding in progress", and "this is what it
    # made". A lot listed weeks early with no number is not missing data — it
    # is a car whose sale has not opened — so say so rather than showing "—".
    money = (f'{r["price"]:,.0f}'.replace(",", " ")
             if r["price"] is not None else "")
    if r["price"] is None:
        price_k, price_v, price_c = "price", "soon", "soon"
    elif r.get("sold_status") == "non_adjuge":
        # It went under the hammer and found no buyer, so the number is the
        # price it FAILED to reach. Calling that a "final price" states the
        # opposite of what happened — 200 lots were labelled that way.
        price_k, price_v, price_c = "unsold, asked", money, "unsold"
    elif expired and (r["price_kind"] == "hammer" or r["state"] == "termine"):
        price_k, price_v, price_c = "final price", money, "final"
    elif expired:
        # Clock has passed but nothing confirms an outcome. Say only that.
        price_k, price_v, price_c = "last seen", money, ""
    else:
        price_k, price_v, price_c = r["price_kind"], money, ""

    star = " on" if str(r["id"]) in saved else ""
    save = (f'<button class="save{star}" data-id="{html.escape(str(r["id"]))}"'
            f' title="save to check later" aria-label="save to check later">'
            f'&#9733;</button>')
    # Alcopa publishes no cote, so that tile would always read "—". Show the
    # opening price there instead: against a live bid it is the useful number.
    # The start price is what makes a closed lot readable: opening 900 -> final
    # 2 100 is the whole story of the sale. Show it wherever there is one and
    # no cote to compare against, not only on Alcopa lots.
    if r.get("start") and (src == "alcopa" or not r.get("cote")):
        second_k, second_c = "start", "start"
        second_v = f'{r["start"]:,.0f}'.replace(",", " ")
    elif src == "alcopa":
        second_k, second_v, second_c = "start", "—", ""
    else:
        second_k, second_v, second_c = "vs cote", dv, dcls
    if r["margin"] is not None:
        sign = "+" if r["margin"] >= 0 else "−"
        right_k, right_v, right_c = "MD margin", \
            f'{sign}{abs(r["margin"]):,}'.replace(",", " "), \
            ("good" if r["margin"] > 0 else "crit")
    else:
        right_k, right_v, right_c = "Landed MD", \
            (f'{r["landed"]:,}'.replace(",", " ") if r["landed"] else "—"), ""

    return f"""<div class="card is-{src}{' blocked' if r['blocked'] else ''}\
{' is-closed' if expired else ''}">
 <a class="thumb" href="/lot/{r['id']}">{thumb}<div class="badges">{badge}</div></a>
 {save}
 <div class="body">
  <div class="t"><a href="/lot/{r['id']}">{html.escape(r['title'] or '')}</a></div>
  <div class="spec">{html.escape(spec)}</div>
  {clock}
  <div class="money">
   <div class="mv"><div class="k">{html.escape(price_k)}</div>
     <div class="v {price_c}">{price_v}</div></div>
   <div class="mv"><div class="k">{second_k}</div><div class="v {second_c}">{second_v}</div></div>
   <div class="mv"><div class="k">{right_k}</div><div class="v {right_c}">{right_v}</div></div>
   <div class="mv"><div class="k">excise</div>
     <div class="v">{f"{r['excise']:,}".replace(",", " ") if r['excise'] else '—'}</div></div>
  </div>
 </div></div>"""


def index_html(rows: list[dict], q: dict, total: int) -> str:
    def g(k, d=""):
        return html.escape((q.get(k, [d])[0] or ""))

    def sel(name, opts, cur, blank):
        o = f'<option value="">{blank}</option>'
        for v, lab in opts:
            s = " selected" if str(v) == cur else ""
            o += f'<option value="{html.escape(str(v))}"{s}>{html.escape(lab)}</option>'
        return f'<select name="{name}">{o}</select>'

    def rng(base, label, step=""):
        """One labelled min-max pair, so a range reads as one control.

        Two bare number boxes side by side is what the old markup did and it
        was unreadable — four inputs in a row with placeholders like "min EUR"
        gave no clue which pair belonged to which quantity.
        """
        st = f' step="{step}"' if step else ""
        # The pre-rename names still filter, so they must still show: otherwise
        # a saved URL silently hides 60% of the catalogue with every box empty.
        LEGACY = {"price_min": "minpx", "price_max": "maxpx",
                  "discount_min": "mindisc", "margin_min": "minmargin"}

        def val(key):
            return g(key) or g(LEGACY.get(key, ""))

        return (f'<span class="rng"><b>{label}</b>'
                f'<input type="number" name="{base}_min" placeholder="min"'
                f' value="{val(base + "_min")}"{st}>'
                f'<em>&ndash;</em>'
                f'<input type="number" name="{base}_max" placeholder="max"'
                f' value="{val(base + "_max")}"{st}></span>')

    makes = sorted({r["make"] for r in ROWS if r["make"]})
    fuels = sorted({r["fuel"] for r in ROWS if r["fuel"]})
    gearboxes = sorted({r["gearbox"] for r in ROWS if r.get("gearbox")})
    bodies = sorted({r["body"] for r in ROWS if r.get("body")})
    locations = sorted({r["location"] for r in ROWS if r.get("location")})
    ck = lambda n, lab: (f'<label class="chk"><input type="checkbox" name="{n}" value="1"'
                         f'{" checked" if g(n) == "1" else ""}>{lab}</label>')

    # Exclusions that are ON by default: the box shows what INCLUDING them
    # would do, so an unticked box means "these are hidden right now".
    ckon = lambda n, lab: (f'<label class="chk on-default">'
                           f'<input type="checkbox" name="{n}" value="1"'
                           f'{" checked" if g(n) == "1" else ""}>{lab}</label>')

    # Paging. The old build rendered rows[:600] and told you to narrow the
    # filters — with 4 967 Alcopa lots that hid 87% of the catalogue behind a
    # message that read like an end-of-results.
    PER = 600
    npages = max(1, -(-len(rows) // PER))
    try:
        page = max(1, min(npages, int((q.get("page", ["1"])[0] or "1"))))
    except ValueError:
        page = 1
    lo = (page - 1) * PER
    saved = saved_ids()
    cards = "".join(card_html(r, saved) for r in rows[lo:lo + PER])
    if not rows:
        more = '<div class="empty">nothing matches those filters</div>'
    elif npages > 1:
        def link(p, label, on=False):
            keep = {k: v for k, v in q.items() if k != "page"}
            qs = "&".join(f"{k}={html.escape(v[0])}" for k, v in keep.items() if v and v[0])
            cls = ' class="on"' if on else ""
            return f'<a{cls} href="/?{qs}&page={p}">{label}</a>'
        win = [p for p in range(page - 2, page + 3) if 1 <= p <= npages]
        nav = ""
        if page > 1:
            nav += link(page - 1, "&larr; prev")
        if win and win[0] > 1:
            nav += link(1, "1") + ('<span class="gap">&hellip;</span>' if win[0] > 2 else "")
        nav += "".join(link(p, str(p), p == page) for p in win)
        if win and win[-1] < npages:
            nav += ('<span class="gap">&hellip;</span>' if win[-1] < npages - 1 else "")
            nav += link(npages, str(npages))
        if page < npages:
            nav += link(page + 1, "next &rarr;")
        more = (f'<div class="pager">{nav}'
                f'<span class="pginfo">{lo + 1:,}&ndash;{min(lo + PER, len(rows)):,} '
                f'of {len(rows):,}</span></div>')
    else:
        more = ""

    return f"""<header><div class="wrap">
 <div class="brand">Hammer<span>/</span>Chi&#537;in&#259;u</div>
 <div class="count mono">{len(rows):,} of {total:,} lots</div>
 <div style="flex:1"></div>
 <a class="count" href="/">reset</a>
</div></header>
<div class="wrap">
<form class="filters" method="get">
 <input name="q" placeholder="search title&hellip;" value="{g('q')}">
 {sel('make', [(m, m) for m in makes], g('make'), 'any make')}
 {sel('fuel', [(f, f) for f in fuels], g('fuel'), 'any fuel')}
 {sel('state', [('adjuge', 'sold'), ('non_adjuge', 'unsold'), ('en_cours', 'live'),
                ('mise_a_prix', 'opening'), ('estimation', 'estimating'),
                ('termine', 'finished'), ('pending', 'price soon')],
      g('state'), 'any state')}
 {sel('gearbox', [(x, x) for x in gearboxes], g('gearbox'), 'any gearbox')}
 {sel('body', [(x, x) for x in bodies], g('body'), 'any body')}
 {sel('location', [(x, x) for x in locations], g('location'), 'any location')}
 {rng('price', 'bid &euro;')}
 {rng('landed', 'landed &euro;')}
 {rng('year', 'year')}
 {rng('km', 'km')}
 {rng('cc', 'cc')}
 {rng('power', 'hp')}
 {rng('margin', 'MD margin &euro;')}
 {rng('discount', 'discount %')}
 {rng('mdprice', 'MD price &euro;')}
 {ck('saved', '&#9733; saved only')}{ck('closed', 'closed only')}{ck('opennow', 'still open')}
 {ck('photos', 'has photos')}{ck('clean', 'no red flags')}
 {ck('excise', 'excise-clean')}{ck('md', 'has MD match')}
 {ck('exportable', 'exportable only')}{ck('docs', 'has MOT/service docs')}
 {ckon('vans', 'include vans/CTTE')}{ckon('special', 'include special/trailers')}
 {sel('src', [('', 'both sites'), ('vpauto', 'VPauto only'), ('alcopa', 'Alcopa only')],
      g('src'), 'source')}
 {sel('sort', [('discount', 'discount'), ('price', 'price'), ('margin', 'MD margin'),
               ('landed', 'landed'), ('km', 'km'), ('year', 'year'), ('cote', 'cote')],
      g('sort') or 'discount', 'sort by')}
 {sel('dir', [('desc', 'high first'), ('asc', 'low first')], g('dir') or 'desc', '')}
 <button>Apply</button>
 <a class="chk" href="/api/lots?{urllib.parse.urlencode({k: v[0] for k, v in q.items()})}">JSON</a>
</form>
<div class="grid">{cards}</div>{more}
</div>"""


# Alcopa's own colour language for damage severity, keyed by its `type` code.
DMG_COLOURS = {
    "T": "#E0705F", "TP": "#E0705F", "MTP": "#E0705F",   # bodywork (+paint)
    "P": "#3B82F6", "MP": "#3B82F6", "RP": "#3B82F6",    # paint
    "MT": "#7FB3E0",                                     # dent removal
    "R": "#2B2B2B", "REM": "#2B2B2B",                    # replacement
}
DMG_DEFAULT = "#D9A54A"


def alcopa_damage(lot_id: str) -> tuple[str | None, dict]:
    """SVG + per-zone damage for one Alcopa lot, read on demand.

    Loaded per lot rather than in build_rows(): the SVGs are a few KB each and
    holding thousands of them in memory to render one page would be wasteful.
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT svg FROM lot_svg WHERE lot_id=?",
                          (lot_id,)).fetchone()
        svg = row["svg"] if row else None
        zones: dict = {}
        for d in con.execute(
                "SELECT zone, zone_label, dtype, type_label, photo FROM damage "
                "WHERE lot_id=? ORDER BY zone", (lot_id,)):
            # Alcopa gives these in French only, and older rows still hold the
            # raw \uXXXX escapes — alcopa_en() decodes and translates both.
            z = zones.setdefault(d["zone"], {
                "label": fr.alcopa_en(d["zone_label"]) or d["zone"],
                "type": fr.alcopa_en(d["type_label"] or d["dtype"]) or "",
                "colour": DMG_COLOURS.get((d["dtype"] or "").upper(), DMG_DEFAULT),
                "photos": []})
            z["photos"].append(d["photo"])
        return svg, zones
    except sqlite3.OperationalError:
        return None, {}          # tables absent until the first Alcopa harvest
    finally:
        con.close()


def damage_panel(lot_id: str) -> str:
    """Alcopa's condition diagram, with the damaged zones clickable."""
    svg, zones = alcopa_damage(lot_id)
    if not svg or not zones:
        return ""
    # Colour only the zones that actually carry damage. The svg element ids
    # (CAPOT, PAVG, ...) are exactly the `zone` codes, which is what makes the
    # diagram addressable at all.
    # zone codes and colours reach a <style> block, so keep them to characters
    # that cannot terminate it or forge a selector
    def css_id(z: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "", z)
    # Every zone id sits on a <g>, and its child <path class="cls-1"> carries
    # fill:none from the svg's own stylesheet. Filling the group alone paints
    # nothing — the child's explicit fill wins — so target the children.
    rules = "".join(
        f'#{css_id(z)}, #{css_id(z)} * {{ fill:{d["colour"]} !important; '
        f'cursor:pointer; }}'
        f'#{css_id(z)}:hover * {{ fill-opacity:.6 !important; }}'
        for z, d in zones.items() if css_id(z))
    # json.dumps does not escape "<", so a "</script>" anywhere in a label or
    # URL would close this tag and let the rest execute as markup
    payload = json.dumps({z: {"label": d["label"], "type": d["type"],
                              "photos": d["photos"]}
                          for z, d in zones.items()},
                         ensure_ascii=False).replace("<", "\\u003c")
    legend = "".join(
        f'<span class="dz-key"><i style="background:{d["colour"]}"></i>'
        f'{html.escape(d["label"])} &mdash; {html.escape(d["type"])} '
        f'({len(d["photos"])})</span>'
        for d in sorted(zones.values(), key=lambda x: x["label"]))
    return (
        f'<div class="panel"><h3>Aesthetic defects '
        f'({len(zones)} zone{"s" if len(zones) != 1 else ""})</h3>'
        f'<p class="note">Click a highlighted panel to see its photos.</p>'
        f'<style>{rules}</style>'
        f'<div class="dz-wrap" id="dzwrap">{svg}</div>'
        f'<div class="dz-legend">{legend}</div>'
        f'<script>window.__DZ={payload};</script></div>')


# --------------------------------------------------------------- cost stack
# Every figure below is sourced in data/cost_stack_verification.md (2026-09-01).
# The two auction houses are NOT comparable raw: VPauto shows a fees-included
# price, Alcopa shows a hammer that may or may not include them.
# Read directly from https://www.alcopa-auction.fr/cgv on 2026-09-02 (through
# the WAF), which upgrades these from index-snippets to primary source and
# settles the "frais inclus vs en sus" conflict the verification doc left open.
ALC_PREMIUM = 0.1440      # "12% HT soit 14.40% TTC"
ALC_PREMIUM_MIN = 360.0   # "avec un minimum de 300€ HT / 360€ TTC"
# The CGV quotes a France fee AND an Export fee. We are always export, so the
# France figure (116,67 HT / 140 TTC) is the wrong column to use.
# Export fees are quoted HT ONLY. The CGV's France rows give both figures
# ("116,67 HT soit 140 TTC") while every Export row stops at HT ("141,67 HT"),
# which is how an export-exempt line is written — so do NOT gross these up by
# 20%. An earlier version of this model did, and overstated both by a fifth.
ALC_SALE_FEE_EXPORT = 141.67  # salle sale, export, without CIRANO warranty
ALC_WEB_EXPORT_FEE = 25.0     # web sale, export
# Section 2.1.1.3 of the official CGV PDF, which the web page only pointed at:
# bidding remotely costs 45 EUR TTC through Alcopa's own LIVE tool and 80 EUR
# TTC through Interencheres. An earlier version of this model used 40 for both.
ALC_LIVE_FEE = 45.0            # Alcopa LIVE tool
ALC_INTERENCHERES_FEE = 80.0   # via the Interencheres partner site
VP_DOSSIER = 200.0        # 166.67 HT = 200 TTC
VP_EXPORT = 120.0         # 100 HT = 120 TTC, export outside the EU
LUX_THRESHOLD_EUR = 30000.0   # 600 000 MDL luxury excise surcharge starts here


def price_history(lot_id: str) -> list[tuple[str, float]]:
    """Every price ever recorded for one lot, oldest first.

    Read on demand rather than in build_rows(): 23 500 rows across 6 100 lots
    is cheap to query per page and pointless to hold in memory for a grid that
    never shows it.
    """
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts, price FROM price_log WHERE lot_id=? AND price IS NOT NULL "
            "ORDER BY ts", (lot_id,)).fetchall()
    except sqlite3.Error:
        rows = []
    con.close()
    return [(r[0], float(r[1])) for r in rows]


def history_panel(lot_id: str) -> str:
    """A lot's price over time, drawn as a plain inline SVG.

    Deliberately quiet when nothing happened: only 163 of 4 411 lots with more
    than one observation ever changed price, so a chart on every lot would be
    thousands of identical flat lines. A lot that never moved gets one line of
    text saying so, which is the useful fact about it.
    """
    pts = price_history(lot_id)
    if not pts:
        return ""
    first, last = pts[0][1], pts[-1][1]
    lo, hi = min(p for _, p in pts), max(p for _, p in pts)
    moved = hi != lo
    span = f"{pts[0][0][:16]} &rarr; {pts[-1][0][:16]}"

    # Format money HERE, never with a blanket .replace(",", " ") over the
    # finished markup — that also rewrote the commas inside the SVG's own
    # coordinate pairs. It rendered only because SVG tolerates space-separated
    # points; one more decimal place and it would have drawn nonsense.
    def eur(v: float) -> str:
        return f"{v:,.0f}".replace(",", " ")

    if not moved:
        return (f'<div class="panel"><h3>Price history</h3>'
                f'<p class="note">Unchanged at &euro;{eur(first)} across '
                f'{len(pts)} observations ({span}). No bid has moved it.'
                f'</p></div>')

    W, H, PAD = 560, 120, 8
    step = (W - 2 * PAD) / max(len(pts) - 1, 1)
    rng = (hi - lo) or 1
    xy = [(PAD + i * step, H - PAD - (p - lo) / rng * (H - 2 * PAD))
          for i, (_, p) in enumerate(pts)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{xy[0][0]:.1f},{H - PAD} " + line + f" {xy[-1][0]:.1f},{H - PAD}"
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5"/>' for x, y in xy)
    delta = last - first
    sign = "+" if delta >= 0 else "−"
    cls = "good" if delta > 0 else "crit" if delta < 0 else ""

    return (
        f'<div class="panel"><h3>Price history</h3>'
        f'<div class="hist-head">'
        f'<span><b>&euro;{eur(first)}</b><em>first seen</em></span>'
        f'<span><b>&euro;{eur(last)}</b><em>latest</em></span>'
        f'<span class="{cls}"><b>{sign}&euro;{eur(abs(delta))}</b><em>change</em></span>'
        f'<span><b>{len(pts)}</b><em>observations</em></span></div>'
        f'<svg class="hist" viewBox="0 0 {W} {H}" preserveAspectRatio="none" '
        f'role="img" aria-label="price over time">'
        f'<polygon points="{area}"/><polyline points="{line}"/>{dots}</svg>'
        f'<p class="note">{span} &middot; low &euro;{eur(lo)} &middot; '
        f'high &euro;{eur(hi)}</p></div>')


def eq_badge(line: dict) -> str:
    """The '=' twin of the 'i' badge: the arithmetic actually performed.

    The 'i' explains what a charge IS and where the rule comes from; that is
    not the same question as "how did you get this number from that one".
    Lines with no arithmetic behind them — a flat fee, a line that is
    deliberately NOT included — get no badge rather than a fake equation.
    """
    eq = line.get("eq")
    if not eq:
        return ""
    return (f'<span class="ci eq" tabindex="0" data-info="{html.escape(eq)}">'
            f'=</span>')


def cost_lines(r: dict) -> tuple[list[dict], float | None]:
    """The landed-cost stack for one lot, each line with its own explanation.

    Source-aware on purpose: the old stack charged VPauto's 200 EUR dossier
    fee to Alcopa lots and never added Alcopa's buyer's premium, which
    under-stated a frais-en-sus Alcopa lot by roughly 14% plus 180 EUR.
    """
    px = r["price"]
    src = r.get("source") or "vpauto"
    lines: list[dict] = []
    if px is None:
        if src != "alcopa":
            # 956 VPauto lots (52%) have no price yet, and this returned an
            # empty list — so the detail page rendered a "Cost to Chisinau"
            # heading above a completely blank table, with nothing saying why.
            # The Alcopa branch below already explained itself; this did not.
            lines.append({
                "k": "No starting price published yet", "v": None,
                "info": "VPauto lists a lot before its sale opens, with no "
                        "'Mise a prix' on the page. Nothing downstream can be "
                        "costed without a purchase price — excise and landed "
                        "cost both start from it. The lot is revisited "
                        "automatically and this fills in once VPauto "
                        "publishes a number."})
            return lines, None
        if src == "alcopa":
            lines.append({
                "k": "Starting price not set yet", "v": None,
                "info": "Alcopa has not published an opening price for this "
                        "lot ('Prix de depart bientot disponible'). Nothing "
                        "downstream can be costed until it does — excise and "
                        "landed cost need a purchase price. This lot is "
                        "re-checked automatically by `alcopa_scrape.py "
                        "refresh` and will fill in as soon as the price "
                        "appears."})
        return lines, None

    if src == "alcopa":
        fees = r.get("fees")
        lines.append({"k": "Hammer price", "v": px,
                      "info": "The bid shown on Alcopa (data-current-price). "
                              "While the auction is live this is the current "
                              "high bid; it is stripped from the page the "
                              "moment the lot sells."})
        if fees == "en_sus":
            pct = px * ALC_PREMIUM
            prem = max(pct, ALC_PREMIUM_MIN)
            which = ("the MINIMUM applies, because 14.40% of the hammer is "
                     f"only {pct:,.0f}" if prem > pct else "the percentage applies")
            lines.append({
                "k": "Buyer's premium (14.40%)", "v": prem,
                "eq": (f"max(hammer x 14.40%, {ALC_PREMIUM_MIN:,.0f} minimum)\n"
                       f"= max({px:,.0f} x 0.1440, {ALC_PREMIUM_MIN:,.0f})\n"
                       f"= max({pct:,.2f}, {ALC_PREMIUM_MIN:,.0f})\n"
                       f"= {prem:,.2f} EUR"),
                "info": "WHAT IT IS: the auction house's own commission. You "
                        "bid 3 400, you pay 3 400 to the seller AND a "
                        "percentage on top to Alcopa for running the sale. It "
                        "is never shown on the lot page — only in the CGV.\n"
                        "WHY HERE: this sale says 'Frais en sus' = fees NOT in "
                        "the hammer.\n"
                        f"= max(hammer x 14.40%, minimum 360)\n"
                        f"= max({px:,.0f} x 0.1440, 360)\n"
                        f"= max({pct:,.0f}, 360)  ->  {prem:,.0f}\n"
                        f"So {which}.\n"
                        "SOURCE: alcopa-auction.fr/cgv, read 2026-09-02: "
                        "'12% HT soit 14.40% TTC (avec un minimum de 300 EUR "
                        "HT / 360 EUR TTC)'."})
            lines.append({
                "k": "Sale fee (export)", "v": ALC_SALE_FEE_EXPORT,
                "info": "Fixed per-vehicle sale fee. The CGV gives a France "
                        "column and an Export column; you are export.\n"
                        f"= {ALC_SALE_FEE_EXPORT:.2f} EUR HT, charged as-is\n"
                        "NOT grossed up by VAT: the France rows quote both "
                        "figures ('116,67 HT soit 140 TTC') while every Export "
                        "row stops at HT, which is how a VAT-exempt export "
                        "line is written. A car sold WITH the CIRANO warranty "
                        "costs 212.50 HT instead.\n"
                        "SOURCE: alcopa-auction.fr/cgv, 'Frais de ventes "
                        "Export', read 2026-09-02."})
            lines.append({
                "k": "Remote bidding fee", "v": ALC_LIVE_FEE,
                "info": "CONDITIONAL, and it depends which screen you bid "
                        "from. Bidding in the room costs nothing extra; "
                        "bidding remotely does:\n"
                        f"  Alcopa's own LIVE tool: {ALC_LIVE_FEE:.0f} EUR "
                        "(37.50 excl. VAT)\n"
                        f"  via Interencheres: {ALC_INTERENCHERES_FEE:.0f} EUR "
                        "(66.67 excl. VAT)\n"
                        "The LIVE figure is used here because this lot carries "
                        "the ALCOPA|LIVE badge. If you end up bidding through "
                        "Interencheres instead, add 35 EUR.\n"
                        "SOURCE: official CGV PDF, section 2.1.1.3, read "
                        "2026-09-02."})
        elif fees == "inclus":
            lines.append({
                "k": "Web export sale fee", "v": ALC_WEB_EXPORT_FEE,
                "info": "WHAT HAPPENS HERE: this is a web sale, and the CGV "
                        "says the hammer is already fees-included: 'Tous nos "
                        "vehicules vendus en ligne pour la France sont frais "
                        "inclus. Le montant de la derniere enchere est le prix "
                        "final a payer.' So NO 14.40% premium.\n"
                        "BUT you are exporting, and the very next sentence "
                        "adds a fee for that: 'Les vehicules vendus en ligne "
                        "pour l'export, les frais de ventes ci-dessous sont "
                        "applicables : Frais de ventes Export 25 EUR HT'.\n"
                        f"= {ALC_WEB_EXPORT_FEE:.2f} EUR HT, charged as-is "
                        "(export lines are quoted HT only, so no 20% on top).\n"
                        "SOURCE: alcopa-auction.fr/cgv, read 2026-09-02."})
        else:
            lines.append({
                "k": "Auction fees", "v": None,
                "info": "UNKNOWN — this lot's page carried neither 'Frais "
                        "inclus' nor 'Frais en sus', so the fee basis could "
                        "not be read. If it turns out to be a salle sale, add "
                        "roughly 14.40% of the hammer (minimum 360) plus 170 "
                        "TTC. Treat this lot's landed cost as a floor, not a "
                        "number."})
    else:
        lines.append({"k": "Adjug&eacute; (fees incl.)", "v": px,
                      "info": "VPauto bids are carried fees-included: "
                              "'Les encheres sont portees frais de vente "
                              "inclus.' Nothing further is added for the "
                              "premium."})
        lines.append({"k": "Frais de dossier", "v": VP_DOSSIER,
                      "info": "166.67 EUR HT = 200 EUR TTC per lot. An EV sold "
                              "with a battery-health certificate is 285 TTC."})
        lines.append({"k": "Export outside EU", "v": VP_EXPORT,
                      "info": "100 EUR HT = 120 EUR TTC, charged for "
                              "collecting the export paperwork. Proof of "
                              "export is due within 90 days or French VAT "
                              "becomes payable."})

    # Skip the informational lines. An Alcopa lot whose fee basis could not be
    # read carries an "Auction fees / unknown" line with v=None, and summing it
    # raised TypeError — a 500 on the detail page of every such lot, silently,
    # because the card grid never calls this.
    goods = sum(l["v"] for l in lines if l["v"] is not None)
    freight = vp.SHIPPING_EUR
    lines.append({"k": "Freight FR&rarr;Chi&#537;in&#259;u", "v": freight,
                  "info": f"{freight:,.0f} EUR is an ESTIMATE, not a quote. No "
                          "published FR-Chisinau single-car price exists; "
                          "France-Romania is quoted from 750 EUR and Chisinau "
                          "adds ~600 km plus a non-EU border. Realistic band "
                          "850-1 300 EUR. A non-runner costs more and cannot "
                          "be driven home."})

    if r["excise"]:
        lines.append({"k": "Moldovan excise", "v": float(r["excise"]),
                      "eq": (f"grid lookup, not a formula we can show in full:\n"
                             f"displacement {r['cc'] or '?'} cm3\n"
                             f"x rate for {r['fuel'] or '?'} in that band\n"
                             f"x age coefficient (first reg {r.get('first_reg') or '?'})\n"
                             f"= {float(r['excise']):,.2f} EUR"),
                      "info": f"Excise = displacement x rate x age coefficient, "
                              f"from the published 2026 grid. This car: "
                              f"{r['cc'] or '?'} cm3, {r['fuel'] or '?'}. "
                              "Diesel jumps hard above 1500 cm3 (20.60 -> "
                              "50.81 lei/cm3) and every year of age adds ~30%. "
                              "Age counts from YEAR OF MANUFACTURE, not first "
                              "registration. Hybrid -25%, plug-in -50%, EV 0 "
                              "(mild/micro-hybrid get nothing)."})

    # customs value is the CIF basis: goods plus the cost of getting them here
    customs_value = goods + freight
    if customs_value <= 1000:
        proc = 4.0
        proc_eq = (f"customs value = {goods:,.0f} (goods) + {freight:,.0f} "
                   f"(freight) = {customs_value:,.0f} EUR\n"
                   f"{customs_value:,.0f} <= 1 000, so the flat fee applies\n"
                   f"= 4.00 EUR")
        proc_info = ("Flat 4 EUR applies to customs values between 100 and "
                     "1 000 EUR (Anexa 2, Legea 1380/1997).")
    else:
        proc = min(customs_value * 0.004, 1800.0)
        proc_eq = (f"customs value = {goods:,.0f} (goods) + {freight:,.0f} "
                   f"(freight) = {customs_value:,.0f} EUR\n"
                   f"= min({customs_value:,.0f} x 0.004, 1 800 cap)\n"
                   f"= min({customs_value * 0.004:,.2f}, 1 800)\n"
                   f"= {proc:,.2f} EUR")
        proc_info = (f"0.4% of the customs value ({customs_value:,.0f} EUR = "
                     f"purchase + freight), capped at 1 800 EUR. This is the "
                     "'taxa pentru proceduri vamale' and is NOT the import "
                     "duty — guides often conflate the two.")
    lines.append({"k": "Customs procedural fee (0.4%)", "v": proc,
                  "eq": proc_eq, "info": proc_info})

    if customs_value > LUX_THRESHOLD_EUR:
        lux = customs_value * 0.02
        lines.append({"k": "Luxury excise surcharge (2%)", "v": lux,
                      "eq": (f"{customs_value:,.0f} (customs value) x 2%\n"
                             f"= {lux:,.2f} EUR"),
                      "info": "An additional excise of 2% applies above "
                              "600 000 MDL (~30 000 EUR) of customs value, "
                              "rising to 10% above 1 800 000 MDL. Shown at "
                              "the entry rate; verify the exact band."})
    else:
        lux = 0.0

    # Round per line THEN sum, so the printed rows actually add up to the
    # printed total. Summing raw floats and rounding once left the table
    # looking like it could not do arithmetic.
    total = sum(round(l["v"]) for l in lines if l["v"] is not None)
    lines.append({"k": "Import duty", "v": None,
                  "info": "NOT INCLUDED — unverified. EU-origin goods with an "
                          "EUR.1 certificate pay 0% under DCFTA, but a "
                          "French-market car of Japanese or Korean make is "
                          "not EU-origin, and the MFN rate for HS 8703 was "
                          "not established. Could be a real, missing cost."})
    lines.append({"k": "VAT (20%)", "v": None,
                  "info": "NOT CHARGED for an individual importing for "
                          "personal use in 2026 — the exemption survived the "
                          "Law 121/2023 deadline. It DOES apply to commercial "
                          "import and to trucks, and drafts target 1 Jan 2027. "
                          "If customs treats you as a trader, add 20%."})
    return lines, total


def detail_html(r: dict, fr_mode: bool = False) -> str:
    photos = r["photos"]
    hero = f'<img class="hero" src="{html.escape(photos[0])}" alt="">' if photos else ""
    gal = "".join(f'<a href="{html.escape(p)}" target="_blank" rel="noopener">'
                  f'<img src="{html.escape(p)}" loading="lazy" alt=""></a>' for p in photos[1:])

    def row(k, v):
        return f"<tr><td>{k}</td><td>{v if v not in (None, '') else '&mdash;'}</td></tr>"

    # the enriched key/value block if we have it, else what the first pass caught
    if r["details"]:
        spec = "".join(
            row(html.escape(k if fr_mode else fr.label_en(k)),
                html.escape(v if fr_mode else fr.value_en(k, v)))
            for k, v in r["details"].items())
    else:
        # Alcopa's Caracteristiques table carries more than the VPauto set —
        # VIN, colour, body, CO2 and VAT status were all captured but never
        # shown. Gearbox arrives in French ("MANUELLE") and needs translating
        # like every other Alcopa string.
        gearbox = r["gearbox"] or ""
        if gearbox and not fr_mode:
            gearbox = fr.alcopa_en(gearbox) or gearbox
        spec = "".join([
            row("Registered", html.escape(r["first_reg"] or "")),
            row("Year", r["year"]),
            row("Mileage", f'{r["km"]:,} km'.replace(",", " ") if r["km"] else None),
            row("Displacement", f'{r["cc"]} cm&sup3;' if r["cc"] else None),
            row("Fuel", html.escape(r["fuel"] or "")),
            row("Gearbox", html.escape(gearbox)),
            row("Power", f'{r["power"]} hp' if r["power"] else None),
            row("Euro norm", r["euro"]),
            row("VIN", html.escape(r.get("vin") or "")),
            row("Colour", html.escape(r.get("colour") or "")),
            row("Body", html.escape(r.get("body") or "")),
            row("CO2", f'{r["co2"]} g/km' if r.get("co2") else None),
            row("VAT recoverable",
                ("yes" if r["tva"] else "no") if r["tva"] is not None else None),
            row("Location", html.escape(r["location"] or "")),
        ])

    warns = ""
    if r["warns"]:
        items = "".join(
            f'<div class="wn {sev}"><b>{sev.upper()}</b><span>{html.escape(txt)}</span></div>'
            for txt, sev in r["warns"])
        warns = f'<div class="panel"><h3>Warnings</h3><div class="warns">{items}</div></div>'

    docs = ""
    if r["ct_pdf"] or r["se_pdf"] or r["damage_img"]:
        links = ""
        if r["ct_pdf"]:
            links += (f'<a href="{html.escape(r["ct_pdf"])}" target="_blank" rel="noopener">'
                      f'&#128196; Contr&ocirc;le Technique (French MOT report &mdash; '
                      f'carries odometer readings)</a>')
        if r["se_pdf"]:
            links += (f'<a href="{html.escape(r["se_pdf"])}" target="_blank" rel="noopener">'
                      f'&#128203; Service history (Suivi d&rsquo;Entretien)</a>')
        dmg = ""
        if r["damage_img"]:
            dmg = (f'<p class="note" style="margin:14px 0 7px">Body condition map &mdash; '
                   f'<span style="color:#D9A54A">yellow</span> scuffs, '
                   f'<span style="color:#7FB3E0">light blue</span> quick repair, '
                   f'<span style="color:#3B82F6">blue</span> paint, '
                   f'<span style="color:#E0705F">red</span> bodywork+paint, '
                   f'black = replacement.</p>'
                   f'<img class="dmg" src="{html.escape(r["damage_img"])}" loading="lazy" alt="">')
        docs = f'<div class="panel"><h3>Documents &amp; condition</h3><div class="docs">{links}</div>{dmg}</div>'

    # Alcopa ships a live SVG with per-zone photos instead of VPauto's flat
    # damage image, so it gets its own clickable panel.
    dzone = damage_panel(r["id"]) if str(r["id"]).startswith("alcopa:") else ""

    equip = ""
    if r["equipment"]:
        items = "".join(f'<div>{html.escape(e if fr_mode else fr.equip_en(e))}</div>'
                        for e in r["equipment"])
        equip = (f'<div class="panel"><h3>Equipment &amp; options '
                 f'({len(r["equipment"])})</h3><div class="eq">{items}</div></div>')

    hist = history_panel(r["id"])
    lines, total = cost_lines(r)
    cost = "".join(
        f'<tr><td>{l["k"]}'
        f'<span class="ci" tabindex="0" data-info="{html.escape(l["info"])}">i</span>'
        f'{eq_badge(l)}'
        f'</td><td>{"&euro;" + format(l["v"], ",.0f").replace(",", " ") if l["v"] is not None else "&mdash;"}</td></tr>'
        for l in lines)
    tot = (f'<tr class="tot"><td>Landed in Chi&#537;in&#259;u</td>'
           f'<td>&euro;{total:,.0f}</td></tr>'.replace(",", " ")) if total else ""

    md = ""
    if r["md_price"]:
        # md_price and margin are INDEPENDENT: a lot can match an MD reference
        # cell while landed cost is still unknown (no price published yet, or
        # cc missing), leaving margin None. Assuming they arrive together
        # killed 88 lot pages outright — the connection dropped with no
        # response at all, which is worse than a 500 because nothing in the
        # browser explains it. card_html already guarded this; this did not.
        mcls = "good" if (r["margin"] or 0) > 0 else "crit"
        if r["margin"] is None:
            marg = "&mdash;"
            mcls = ""
        else:
            sign = "+" if r["margin"] >= 0 else "−"
            marg = f'{sign}&euro;{abs(r["margin"]):,.0f}'.replace(",", " ")
        md = f"""<div class="panel"><h3>Moldova resale</h3><table class="kv">
        {row("Matched cell", html.escape(r["md_cell"] or ""))}
        {row("Sold in window", f'{int(r["md_n"])} cars')}
        {row("Sell-through", f'{r["md_sell"]}%')}
        {row("MD median asking", f'&euro;{r["md_price"]:,.0f}'.replace(",", " "))}
        <tr class="tot"><td>Margin over landed</td>
        <td class="{mcls}">{marg}</td></tr></table>
        <p class="note">The Moldovan figure is the median <em>asking</em> price of sold listings,
        not a transaction price &mdash; 32.4% of sold ads changed price before selling, 71.8% of those
        downward. Treat this margin as an upper bound.</p></div>"""

    flags = ""
    obs = ""
    if r["obs"]:
        shown = r["obs"] if fr_mode else (r["obs_en"] or r["obs"])
        orig = ""
        if not fr_mode and r["obs_en"] and r["obs_en"] != r["obs"]:
            orig = (f'<p class="note" style="margin-top:11px"><em>Original French:</em><br>'
                    f'{html.escape(html.unescape(r["obs"]))}</p>')
        # the translator marks the serious bits with **...** - render that as emphasis
        body = re.sub(r"\*\*(.+?)\*\*",
                      r'<strong style="color:var(--crit)">\1</strong>',
                      html.escape(html.unescape(shown)))
        obs = (f'<div class="panel"><h3>Auctioneer notes</h3>'
               f'<p style="margin:0;font-size:13.5px">{body}</p>'
               f'{orig}</div>')

    disc = (f'{r["discount"]}% under the auctioneer&rsquo;s own cote of '
            f'&euro;{r["cote"]:,.0f}'.replace(",", " ")) if r["discount"] is not None else \
        "no cote captured for this lot"

    return f"""<header><div class="wrap">
 <div class="brand">Hammer<span>/</span>Chi&#537;in&#259;u</div>
 <a class="count" href="/">&larr; back to catalogue</a>
 <div class="langbar">
   <a class="{'' if fr_mode else 'on'}" href="/lot/{html.escape(r['id'])}">EN</a>
   <a class="{'on' if fr_mode else ''}" href="/lot/{html.escape(r['id'])}?lang=fr">FR original</a>
 </div></div></header>
<div class="wrap"><div class="det">
 <div>
  <h1 class="dt">{html.escape(r['title'] or '')}</h1>
  <div class="sub mono">{html.escape(r['id'])} · {disc}</div>
  {hero}<div class="gal">{gal}</div>
  {'' if photos else '<div class="empty">Detail page not fetched yet for this lot — '
   'only the listing card was captured.</div>'}
 </div>
 <div>
  {warns}
  <div class="panel"><h3>Cost to Chi&#537;in&#259;u</h3>
    <table class="kv">{cost}{tot}</table>
    <p class="note">Computed live through the scraper&rsquo;s verified 2026 excise grid.
    Freight is an estimate, not a quote &mdash; restart with
    <span class="mono">--shipping</span> to re-price every car.</p></div>
  <div class="panel"><h3>Vehicle</h3><table class="kv">{spec}</table></div>
  {hist}{md}{docs}{dzone}{obs}{flags}{equip}
  <a class="back" href="{html.escape(r['url'] or '#')}" target="_blank" rel="noopener">
    Open on {'alcopa-auction.fr' if str(r['id']).startswith('alcopa:') else 'vpauto.fr'} &rarr;</a>
 </div></div></div>"""


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def handle_one_request(self):
        # a browser closing a tab mid-response is normal, not an error worth printing
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/save":
            self._send(b"<h1>404</h1>", code=404)
            return
        lot = (urllib.parse.parse_qs(u.query).get("id", [""])[0] or "").strip()
        if not lot:
            self._send(b'{"error":"no id"}', "application/json", 400)
            return
        self._send(json.dumps({"id": lot, "saved": saved_toggle(lot)}).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        refresh_if_stale()

        if path == "/":
            rows = apply_filters(ROWS, q)
            self._send(page_shell(index_html(rows, q, len(ROWS)), "Hammer to Chisinau"))
        elif path == "/api/lots":
            rows = apply_filters(ROWS, q)
            self._send(json.dumps(rows, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")
        elif path.startswith("/lot/"):
            lid = path[5:]
            r = next((x for x in ROWS if x["id"] == lid), None)
            if not r:
                self._send(b"<h1>404</h1>", code=404)
            else:
                fr_mode = (q.get("lang", [""])[0] == "fr")
                self._send(page_shell(detail_html(r, fr_mode), r["title"] or lid))
        elif path.startswith("/api/lot/"):
            r = next((x for x in ROWS if x["id"] == path[9:]), None)
            self._send(json.dumps(r or {}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", 200 if r else 404)
        else:
            self._send(b"<h1>404</h1>", code=404)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from your phone on the LAN")
    ap.add_argument("--shipping", type=float, help="override freight EUR and re-price everything")
    args = ap.parse_args()

    if args.shipping:
        vp.SHIPPING_EUR = args.shipping

    load_md()
    ROWS.extend(build_rows())
    priced = sum(1 for r in ROWS if r["price"])
    withpx = sum(1 for r in ROWS if r["nphotos"])
    matched = sum(1 for r in ROWS if r["md_price"])
    print(f"  {len(ROWS):,} lots | {priced:,} priced | {withpx:,} with photos | "
          f"{matched:,} matched to a Moldovan cell")
    print(f"  freight assumed EUR {vp.SHIPPING_EUR:,.0f}  (unverified)")
    print(f"\n  http://localhost:{args.port}/")
    if args.host == "0.0.0.0":
        import socket
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"  http://{ip}:{args.port}/   <- same URL works on your phone")
        except Exception:
            pass
    print("\n  Ctrl-C to stop\n")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
