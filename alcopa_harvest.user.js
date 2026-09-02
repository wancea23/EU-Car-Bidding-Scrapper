// ==UserScript==
// @name         Alcopa harvest -> EU-Car-Auctions
// @namespace    eu-car-auctions
// @version      1.0
// @description  Reads the lots already rendered on the page you are looking at, accumulates them across pages, exports JSON for vpauto_scrape.py --import-dir
// @match        https://www.alcopa-auction.fr/*
// @match        https://alcopa-auction.fr/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * You browse. This reads.
 *
 * It issues no requests of its own — it only parses the DOM your browser has
 * already rendered for you, and remembers what it saw so you can walk through
 * result pages normally and export everything at the end.
 *
 * The card selectors are deliberately generic because Alcopa's markup has not
 * been mapped. Use the "copy 1 card" button once and send that HTML over, and
 * the extraction can be made exact instead of heuristic.
 */
(function () {
  'use strict';

  const KEY = 'eu_car_auctions_alcopa';
  const MONEY = /(\d[\d\s.  ]{2,})\s*(?:€|EUR)/;
  const KM = /(\d[\d\s.  ]{2,})\s*km\b/i;
  const YEAR = /\b((?:19|20)\d{2})\b/;
  const CC = /\b(\d{3,4})\s*(?:cm3|cm³|cc)\b/i;
  const FUEL = /\b(diesel|essence|électrique|electrique|hybride(?:\s+rechargeable)?|gpl|gnv)\b/i;
  const EURO = /norme?\s*euro\s*:?\s*(\d[a-d]?)/i;
  // which of the money figures on a card it is
  const LABELS = [
    ['adjuge', /adjug/i],
    ['vendu', /\bvendu\b/i],
    ['en_cours', /ench[eè]re\s+en\s+cours|encherissez|offre\s+actuelle/i],
    ['mise_a_prix', /mise\s+[àa]\s+prix|prix\s+de\s+d[ée]part/i],
    ['estimation', /estimation|\bcote\b/i],
  ];

  const num = (s) => parseInt(String(s).replace(/[^\d]/g, ''), 10) || null;
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; } };
  const save = (o) => { try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) { /* quota */ } };

  function qualifies(el) {
    const t = el.innerText || '';
    if (t.length < 20 || t.length > 1500) return false;
    return MONEY.test(t) && (KM.test(t) || YEAR.test(t));
  }

  /** Smallest elements that look like a lot card (no qualifying child). */
  function cards() {
    const all = Array.from(document.querySelectorAll('article, li, tr, div'));
    return all.filter((el) => qualifies(el)
      && !Array.from(el.children).some((c) => qualifies(c)));
  }

  function labelFor(text) {
    for (const [name, re] of LABELS) if (re.test(text)) return name;
    return 'prix';
  }

  function extract(el) {
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const a = el.querySelector('a[href]') || el.closest('a[href]');
    const heading = el.querySelector('h1,h2,h3,h4,strong,b');
    const money = text.match(MONEY);
    const km = text.match(KM);
    const yr = text.match(YEAR);
    const cc = text.match(CC);
    const fuel = text.match(FUEL);
    const euro = text.match(EURO);
    return {
      source: 'alcopa',
      url: a ? a.href : location.href,
      title: (heading ? heading.innerText : text.slice(0, 90)).replace(/\s+/g, ' ').trim(),
      price: money ? num(money[1]) : null,
      price_label: labelFor(text),
      km: km ? num(km[1]) : null,
      year: yr ? parseInt(yr[1], 10) : null,
      cc: cc ? parseInt(cc[1], 10) : null,
      fuel: fuel ? fuel[1] : null,
      euro_norm: euro ? euro[1] : null,
      photos: Array.from(el.querySelectorAll('img'))
        .map((i) => i.currentSrc || i.src).filter((s) => s && !s.startsWith('data:')),
      raw_text: text.slice(0, 500),
      seen_at: new Date().toISOString(),
      page: location.href,
    };
  }

  function harvest() {
    const store = load();
    let added = 0;
    for (const el of cards()) {
      const row = extract(el);
      const id = row.url && row.url !== location.href
        ? row.url
        : `${row.title}|${row.km}|${row.price}`;
      // a later pass can only improve a row: keep the one with a real price
      if (!store[id] || (!store[id].price && row.price)
          || (store[id].price_label !== 'adjuge' && row.price_label === 'adjuge')) {
        store[id] = row;
        added++;
      }
    }
    save(store);
    return { added, total: Object.keys(store).length };
  }

  function download() {
    const rows = Object.values(load());
    if (!rows.length) return alert('nothing captured yet');
    const blob = new Blob([JSON.stringify(rows, null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `alcopa_${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  // ---------------------------------------------------------------- UI
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:2147483647;'
    + 'background:#14181d;color:#e8edf2;font:12px/1.45 system-ui,sans-serif;'
    + 'padding:10px 12px;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.45);'
    + 'min-width:184px';
  const mk = (label, fn, bg) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.style.cssText = `margin:3px 4px 0 0;padding:4px 9px;border:0;border-radius:6px;
      cursor:pointer;font:11px system-ui,sans-serif;background:${bg};color:#fff`;
    b.onclick = fn;
    return b;
  };
  const count = document.createElement('div');
  count.style.cssText = 'font-weight:600;margin-bottom:2px';
  box.appendChild(count);

  function refresh(msg) {
    const n = Object.keys(load()).length;
    count.textContent = `Alcopa: ${n} loturi` + (msg ? ` (${msg})` : '');
  }

  box.appendChild(mk('scan', () => { const r = harvest(); refresh(`+${r.added}`); }, '#2d6cdf'));
  box.appendChild(mk('export JSON', download, '#1f9d55'));
  box.appendChild(mk('copy 1 card', () => {
    const c = cards()[0];
    if (!c) return alert('no card found on this page');
    navigator.clipboard.writeText(c.outerHTML.slice(0, 20000));
    refresh('card copied');
  }, '#8a5cf6'));
  box.appendChild(mk('reset', () => {
    if (confirm('clear everything captured?')) { localStorage.removeItem(KEY); refresh('cleared'); }
  }, '#8a3b3b'));

  document.body.appendChild(box);
  refresh();

  // auto-scan this page, then again on SPA navigation / late renders
  setTimeout(() => { const r = harvest(); refresh(`+${r.added}`); }, 900);
  let last = location.href;
  setInterval(() => {
    if (location.href !== last) {
      last = location.href;
      setTimeout(() => { const r = harvest(); refresh(`+${r.added}`); }, 1200);
    }
  }, 1500);
})();
