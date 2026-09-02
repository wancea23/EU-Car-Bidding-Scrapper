"""Turn alcopa_harvest.user.js into a one-click bookmarklet.

No Tampermonkey, no pasting code into a console: open the generated page once,
drag the button to the bookmarks bar, done. After that it is one click on any
Alcopa page you are already looking at.

    python build_bookmarklet.py     ->  data/alcopa_bookmarklet.html
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
SRC = ROOT / "alcopa_harvest.user.js"
OUT = ROOT / "data" / "alcopa_bookmarklet.html"


def build() -> tuple[str, int]:
    js = SRC.read_text(encoding="utf-8")
    # drop the UserScript metadata block; the rest runs as-is
    js = re.sub(r"//\s*==UserScript==.*?//\s*==/UserScript==", "", js, flags=re.S)
    # a bookmarklet re-runs on every click, so make the UI idempotent
    js = js.replace(
        "document.body.appendChild(box);",
        "var _old=document.getElementById('eca-box');if(_old)_old.remove();"
        "box.id='eca-box';document.body.appendChild(box);")
    href = "javascript:" + urllib.parse.quote(js.strip(), safe="")
    return href, len(href)


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Alcopa harvest — bookmarklet</title>
<style>
 body{{font:15px/1.6 system-ui,sans-serif;max-width:640px;margin:44px auto;padding:0 20px;
      background:#0f1216;color:#e6ebf1}}
 h1{{font-size:20px;margin:0 0 4px}} p{{color:#9fb0c0}} code{{color:#7fd1a0}}
 .btn{{display:inline-block;margin:22px 0;padding:13px 22px;background:#1f9d55;color:#fff;
      border-radius:10px;text-decoration:none;font-weight:600;cursor:grab}}
 ol{{color:#c7d3de}} li{{margin:7px 0}} .small{{font-size:13px;color:#7d8b99}}
</style>
<h1>Alcopa harvest</h1>
<p>Trage butonul de mai jos în bara de marcaje (Ctrl+Shift+B dacă nu e vizibilă).</p>
<a class="btn" href="{href}">🚗 Alcopa harvest</a>
<ol>
  <li>Intri pe <code>alcopa-auction.fr</code> și treci de verificare, normal.</li>
  <li>Pe pagina cu loturi, dai click pe marcaj. Apare panoul din dreapta-jos.</li>
  <li>Navighezi mai departe; pe fiecare pagină nouă, un click. Se adună tot.</li>
  <li><b>export JSON</b> → pui fișierul în <code>EU-Car-Auctions/data/saved/</code></li>
  <li><code>python vpauto_scrape.py --import-dir data/saved</code></li>
</ol>
<p class="small">Butonul <b>copy 1 card</b> îți copiază structura unui lot în clipboard —
trimite-mi-o și extragerea devine exactă în loc de euristică.<br>
Nu face nicio cerere către site: citește doar pagina deja încărcată în browserul tău.</p>
"""


if __name__ == "__main__":
    href, size = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(PAGE.format(href=href.replace('"', "&quot;")), encoding="utf-8")
    print(f"bookmarklet: {size:,} chars (Chrome accepts well over this)")
    print(f"open: {OUT}")
