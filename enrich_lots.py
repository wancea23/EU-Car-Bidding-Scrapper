#!/usr/bin/env python3
"""Second pass over VPauto lot pages: everything the first parser skipped.

    python enrich_lots.py              # enrich lots that already have detail (142)
    python enrich_lots.py --all        # every lot with a URL (slow: ~3s each)
    python enrich_lots.py --limit 20

The original parse_lot() took the headline fields. The lot page also publishes:
  * the full Informations generales / Caracteristiques techniques key-value blocks
    (colour, Crit'Air, CO2, doors, dimensions, gearbox speeds, seats on the V5...)
  * the Equipements/Options list
  * a rendered body-damage diagram  <key>_ET.jpg
  * the controle technique PDF      <key>_CT.pdf   <- odometer history
  * the service history PDF         <key>_SE.pdf

Stored as JSON on the existing row; nothing already captured is overwritten.
Resumable - a lot with details_json already set is skipped unless --redo.
"""
from __future__ import annotations

import argparse
import html as ihtml
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "vpauto.db"

_spec = importlib.util.spec_from_file_location("vp", HERE / "vpauto_scrape.py")
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)

LI = re.compile(r"<li>\s*<span>([^<]+?)\s*:\s*</span>\s*(.*?)\s*</li>", re.S)
PLAIN_LI = re.compile(r"<li>\s*([^<>]+?)\s*</li>", re.S)
BLOCK = re.compile(r'<ul class="liste0(4|5)">(.*?)</ul>', re.S)


def _txt(s: str) -> str:
    s = re.sub(r"<span class=\"bubule\".*?</span></span>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", ihtml.unescape(s)).strip()


def parse_extra(page: str) -> dict:
    """Pull every liste04 key/value pair, the liste05 equipment list and the assets."""
    fields: dict[str, str] = {}
    equipment: list[str] = []

    for kind, body in BLOCK.findall(page):
        if kind == "4":
            for label, value in LI.findall(body):
                label, value = _txt(label), _txt(value)
                if label and value and label not in fields:
                    fields[label] = value
        else:
            for item in PLAIN_LI.findall(body):
                item = _txt(item)
                if item and item not in equipment:
                    equipment.append(item)

    out: dict = {"fields": fields, "equipment": equipment}

    m = re.search(r'https://cdn\.vpauto\.fr/d/([A-Za-z0-9_-]+)_ET\.jpg', page)
    if m:
        key = m.group(1)
        out["damage_img"] = f"https://cdn.vpauto.fr/d/{key}_ET.jpg"
        out["doc_key"] = key
    for tag, name in (("CT", "ct_pdf"), ("SE", "se_pdf")):
        m = re.search(rf'(https://cdn\.vpauto\.fr/d/[A-Za-z0-9_-]+_{tag}\.pdf)', page)
        if m:
            out[name] = m.group(1)
    return out


def ensure_columns(con: sqlite3.Connection) -> None:
    have = {r[1] for r in con.execute("PRAGMA table_info(lots)")}
    for col in ("details_json", "equipment_json", "damage_img", "ct_pdf", "se_pdf"):
        if col not in have:
            con.execute(f"ALTER TABLE lots ADD COLUMN {col} TEXT")
    con.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="every lot with a URL, not just detailed ones")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true", help="re-fetch lots already enriched")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    ensure_columns(con)

    where = "url IS NOT NULL"
    if not args.all:
        where += " AND photos IS NOT NULL AND photos != ''"
    if not args.redo:
        where += " AND (details_json IS NULL OR details_json = '')"
    rows = con.execute(f"SELECT lot_id, url, photos FROM lots WHERE {where}").fetchall()
    if args.limit:
        rows = rows[:args.limit]

    print(f"{len(rows)} lots to enrich  (~{len(rows) * 3 // 60} min at 2-4s each)\n")
    ok = fail = core = 0
    for i, (lot_id, url, have_photos) in enumerate(rows, 1):
        try:
            page = vp.fetch(url)
        except Exception as exc:                                  # noqa: BLE001
            print(f"  [{i}/{len(rows)}] !! {lot_id}: {exc}")
            fail += 1
            vp.nap()
            continue

        # lots that were never opened have no photos/title/cc - run the original
        # parser too, so this pass fills the catalogue rather than only decorating it
        if not have_photos:
            try:
                row = vp.parse_lot(page, url)
                row.pop("lot_id", None)
                # NEVER let the detail page blank a value the listing card owns:
                # a sold lot's page drops `mise a prix` entirely, and writing that
                # NULL back destroys the only price we have. Merge, don't replace.
                for owned in ("sale_state", "sale_price", "current_bid",
                              "mise_a_prix", "card_year", "card_km", "last_seen"):
                    row.pop(owned, None)
                cols = [k for k in row if row[k] is not None]
                if cols:
                    con.execute(
                        f"UPDATE lots SET {','.join(c + '=?' for c in cols)} WHERE lot_id=?",
                        [row[c] for c in cols] + [lot_id])
                core += 1
            except Exception as exc:                              # noqa: BLE001
                print(f"      core parse failed for {lot_id}: {exc}")

        extra = parse_extra(page)
        con.execute(
            "UPDATE lots SET details_json=?, equipment_json=?, damage_img=?, ct_pdf=?, se_pdf=? "
            "WHERE lot_id=?",
            (json.dumps(extra["fields"], ensure_ascii=False),
             json.dumps(extra["equipment"], ensure_ascii=False),
             extra.get("damage_img"), extra.get("ct_pdf"), extra.get("se_pdf"), lot_id))
        con.commit()
        ok += 1
        flags = "".join([
            "D" if extra.get("damage_img") else "-",
            "C" if extra.get("ct_pdf") else "-",
            "S" if extra.get("se_pdf") else "-"])
        print(f"  [{i}/{len(rows)}] {lot_id}  {len(extra['fields']):2d} fields  "
              f"{len(extra['equipment']):2d} options  [{flags}]", flush=True)
        vp.nap()

    print(f"\ndone: {ok} enriched ({core} newly given photos/specs), {fail} failed")


if __name__ == "__main__":
    main()
