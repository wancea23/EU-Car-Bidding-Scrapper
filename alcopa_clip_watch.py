"""Watch the Windows clipboard for auction pages and import the lots.

No extension, no bookmarklet, no dragging anything. You browse Alcopa in your
own browser like a person, press Ctrl+A then Ctrl+C, and this picks the lots
out of the copied HTML and writes them into the same database as the VPauto
lots, with Moldovan excise and landed cost already computed.

Nothing here talks to alcopa-auction.fr. It only reads what your browser put
on your clipboard.

    python alcopa_clip_watch.py            # run it, then go browse and copy
    python alcopa_clip_watch.py --once     # parse whatever is on the clipboard now
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vpauto_scrape import (  # noqa: E402
    db, excise_eur, landed_eur, red_flags,
)

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
# no  after the currency: EUR sign is not a word char, so  never matches there
# a number is either grouped thousands (15 800) or a plain run — never
# an open [digits+spaces] class, which happily swallows "2023 42498" as one
NUM = r"(?:\d{1,3}(?:[ .  ]\d{3})+|\d{1,7})"
MONEY = re.compile(r"(" + NUM + r")\s*(?:€|EUR(?![a-z]))")
KM = re.compile(r"(" + NUM + r")\s*km(?![a-z])", re.I)
YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
CC = re.compile(r"\b(\d{3,4})\s*(?:cm3|cm³|cc)\b", re.I)
FUEL = re.compile(r"\b(diesel|essence|électrique|electrique|"
                  r"hybride(?:\s+rechargeable)?|gpl|gnv)\b", re.I)
EURO = re.compile(r"norme?\s*euro\s*:?\s*(\d[a-d]?)", re.I)
REG = re.compile(r"\b([0-3]?\d/[01]?\d/(?:19|20)\d{2})\b")
LABELS = [("adjuge", re.compile(r"adjug", re.I)),
          ("adjuge", re.compile(r"\bvendu\b", re.I)),
          ("en_cours", re.compile(r"ench[eè]re\s+en\s+cours|offre\s+actuelle", re.I)),
          ("mise_a_prix", re.compile(r"mise\s+[àa]\s+prix|prix\s+de\s+d[ée]part", re.I)),
          ("estimation", re.compile(r"estimation|\bcote\b", re.I))]


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent", "text")

    def __init__(self, tag: str, attrs: dict, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.kids: list = []
        self.text: list[str] = []

    def all_text(self) -> str:
        out = list(self.text)
        for k in self.kids:
            out.append(k.all_text())
        return re.sub(r"\s+", " ", " ".join(out)).strip()

    def find(self, tag: str) -> list:
        hits = [k for k in self.kids if k.tag == tag]
        for k in self.kids:
            hits += k.find(tag)
        return hits


class Tree(HTMLParser):
    """Just enough DOM to ask 'which is the smallest block holding one lot?'."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {}, None)
        self.cur = self.root
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        node = Node(tag, dict(attrs), self.cur)
        self.cur.kids.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        n = self.cur
        while n is not self.root:
            if n.tag == tag:
                self.cur = n.parent
                return
            n = n.parent

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.cur.text.append(data)


def _num(s) -> int | None:
    d = re.sub(r"\D", "", str(s))
    return int(d) if d else None


def qualifies(node: Node) -> bool:
    t = node.all_text()
    if not (20 < len(t) < 1500):
        return False
    return bool(MONEY.search(t)) and bool(KM.search(t) or YEAR.search(t))


def whole_card(node: Node, cap: int = 1500) -> Node:
    """Climb to the biggest still-card-sized ancestor.

    A listing card is often split: one half holds the model/year/km, the
    sibling holds "Adjuge 15800 EUR". Taking only the deepest qualifying node
    grabs one half and silently loses the sale state.
    """
    best = node
    cur = node.parent
    while cur is not None and cur.tag != "root":
        if len(cur.all_text()) >= cap:
            break
        best = cur
        cur = cur.parent
    return best


def collect(node: Node, out: list) -> None:
    """Deepest qualifying nodes only — a page-level div 'contains' every lot."""
    if qualifies(node) and not any(qualifies(k) for k in node.kids):
        card = whole_card(node)
        if card not in out:
            out.append(card)
        return
    for k in node.kids:
        collect(k, out)


def price_and_label(text: str) -> tuple[int | None, str]:
    """A card can carry several figures. Take the one that belongs to the
    strongest label present, in priority order, not merely the first on the
    card: an adjudicated lot still shows its opening price too."""
    for name, rx in LABELS:
        m = rx.search(text)
        if not m:
            continue
        money = MONEY.search(text, m.end())
        if money:
            return _num(money.group(1)), name
    money = MONEY.search(text)
    return (_num(money.group(1)) if money else None), "prix"


def parse_clip(html: str) -> list[dict]:
    # PowerShell hands back the CF_HTML wrapper; the real markup starts here
    if "StartFragment" in html:
        html = html.split("<!--StartFragment-->", 1)[-1].split("<!--EndFragment-->")[0]
    tree = Tree()
    tree.feed(html)
    nodes: list[Node] = []
    collect(tree.root, nodes)
    rows = []
    for n in nodes:
        text = n.all_text()
        price, label = price_and_label(text)
        if price is None:
            continue
        km, yr, cc = KM.search(text), YEAR.search(text), CC.search(text)
        fuel, euro, reg = FUEL.search(text), EURO.search(text), REG.search(text)
        links = [a.attrs.get("href") for a in n.find("a") if a.attrs.get("href")]
        if not links and n.parent:
            links = [a.attrs.get("href") for a in n.parent.find("a") if a.attrs.get("href")]
        imgs = [i.attrs.get("src") for i in n.find("img") if i.attrs.get("src")]
        heads = [h.all_text() for h in
                 (n.find("h1") + n.find("h2") + n.find("h3") + n.find("strong"))
                 if h.all_text()]
        # the first heading is usually just the marque; the model is the long one
        head = max(heads, key=len) if heads else text[:90]
        if heads and len(head) < 12:
            head = " ".join(dict.fromkeys(heads))
        rows.append({
            "url": links[0] if links else None,
            "title": head[:160],
            "price": price,
            "price_label": label,
            "km": _num(km.group(1)) if km else None,
            "year": int(yr.group(1)) if yr else None,
            "cc": int(cc.group(1)) if cc else None,
            "fuel": fuel.group(1) if fuel else None,
            "euro_norm": euro.group(1) if euro else None,
            "first_reg": reg.group(1) if reg else None,
            "photos": [i for i in imgs if i and not i.startswith("data:")],
            "raw_text": text[:500],
        })
    return rows


def store(rows: list[dict], source: str = "alcopa") -> int:
    con = db()
    STATE = {"adjuge": "adjuge", "en_cours": "en_cours", "mise_a_prix": "mise_a_prix"}
    n = 0
    for r in rows:
        key = r["url"] or f"{r['title']}|{r['km']}|{r['price']}"
        lot_id = f"{source}:{hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest()[:16]}"
        first_reg = r["first_reg"] or (f"01/07/{r['year']}" if r["year"] else None)
        state = STATE.get(r["price_label"])
        price = r["price"]
        row = {
            "lot_id": lot_id, "url": r["url"], "title": r["title"], "source": source,
            "first_reg": first_reg, "card_year": r["year"],
            "km": r["km"], "card_km": r["km"], "cc": r["cc"], "fuel": r["fuel"],
            "euro_norm": r["euro_norm"], "sale_state": state,
            "sale_price": price if state == "adjuge" else None,
            "current_bid": price if state == "en_cours" else None,
            "mise_a_prix": price if state == "mise_a_prix" else None,
            "cote": price if r["price_label"] == "estimation" else None,
            "observations": r["raw_text"],
            "photos": "\n".join(r["photos"]), "photo_count": len(r["photos"]),
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
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


def clipboard_html() -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$h = Get-Clipboard -TextFormatType Html -ErrorAction SilentlyContinue; "
             "if ($h) { [Console]::Out.Write($h -join \"`n\") }"],
            capture_output=True, timeout=25)
        return out.stdout.decode("utf-8", "replace")
    except Exception:                                     # noqa: BLE001
        return ""


def handle(html: str, source: str) -> int:
    rows = parse_clip(html)
    if not rows:
        # keep the evidence: a clipboard is gone the moment anything else is
        # copied, and without the markup the parser cannot be fixed
        dump = Path(__file__).parent / "data" / "saved" / \
            f"clip_{time.strftime('%Y%m%d_%H%M%S')}.html"
        try:
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(html, encoding="utf-8")
            say(f"  0 loturi — HTML salvat pentru analiza: {dump.name}")
        except OSError as exc:
            say(f"  0 loturi, si n-am putut salva HTML-ul: {exc}")
        return 0
    n = store(rows, source)
    priced = [r for r in rows if r["price"]]
    with_hammer = [r for r in rows if r["price_label"] == "adjuge"]
    say(f"  + {n} loturi  ({len(priced)} cu pret, {len(with_hammer)} adjudecate)")
    for r in rows[:6]:
        land = landed_eur(r["price"], r["cc"], r["fuel"] or "",
                          r["first_reg"] or (f"01/07/{r['year']}" if r["year"] else None)) \
            if r["price"] else None
        print(f"      {r['title'][:40]:42.42s} {str(r['price_label']):11s} "
              f"{r['price'] or 0:>7,} {r['km'] or 0:>7,}km "
              f"{r['cc'] or 0:>5}cc landed={land or 0:>8,.0f}")
    if len(rows) > 6:
        print(f"      ... si inca {len(rows) - 6}")
    return n


LOG = Path(__file__).parent / "data" / "clip_watch.log"


def say(msg: str = "") -> None:
    """Print AND append to a file.

    Python buffers stdout when it is not a terminal, so a backgrounded run
    looks stone dead even while it works. Line buffering plus a log on disk
    means there is always something to look at.
    """
    print(msg, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:                                     # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="parse the clipboard once and exit")
    ap.add_argument("--source", default="alcopa", help="tag rows with this source name")
    ap.add_argument("--interval", type=float, default=1.5)
    args = ap.parse_args()

    if args.once:
        html = clipboard_html()
        if not html.strip():
            print("clipboard nu contine HTML — pe pagina Alcopa: Ctrl+A apoi Ctrl+C")
            return
        if not handle(html, args.source):
            print("n-am gasit loturi in ce e copiat")
        return

    say(f"PORNIT (pid {os.getpid()}) — pe pagina Alcopa: Ctrl+A apoi Ctrl+C")
    say(f"jurnal: {LOG}")
    last = ""
    total = 0
    beat = time.time()
    try:
        while True:
            html = clipboard_html()
            digest = hashlib.sha1(html.encode("utf-8", "replace")).hexdigest()
            if html.strip() and digest != last:
                last = digest
                say(f"clipboard nou ({len(html):,} chars)")
                total += handle(html, args.source)
                say(f"  total in baza: {total}")
                beat = time.time()
            elif time.time() - beat > 30:
                # a silent process looks like a dead one
                beat = time.time()
                say(f"astept... ({total} loturi pana acum)")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        say(f"stop. {total} loturi importate. Ruleaza: python vpauto_scrape.py --report")


if __name__ == "__main__":
    main()
