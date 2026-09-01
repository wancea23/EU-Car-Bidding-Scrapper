# EU Car Bidding Scrapper — Alcopa price watcher

Records what cars actually sell for at [alcopa-auction.fr](https://www.alcopa-auction.fr).

## Why this has to run on a schedule

A live Alcopa lot exposes `data-current-price` (the standing bid) and its sale
page exposes `data-ts` (the closing second). **The moment a lot sells, both are
deleted.** The sold page keeps its photos and specifications and shows no price
anywhere and no countdown — just a `VENDU` badge. Nothing recovers that number
afterwards, from any page.

So the final price only exists in the seconds before the hammer. Every lot in a
sale shares one closing timestamp, which means they all close together and have
to be polled in parallel; a sequential sweep would smear the capture across the
only minute that matters.

## How it gets in

The site sits behind an AWS WAF CAPTCHA — a 3×3 image grid ("choose all the
curtains"). A vision model reads the grid, the nine hidden accessibility
buttons register the selection, and solving it mints an `aws-waf-token` cookie.
That cookie is good for **exactly 300 seconds** (measured: alive at 287s, dead
at 307s), during which ordinary HTTP requests work normally. One browser solve
therefore buys a five-minute window of plain scraping, which is why the watcher
re-mints before each burst rather than per request.

## What runs

| Schedule (UTC) | What it does |
|---|---|
| `15 4 * * *` | Rebuilds `data/watchlist.json` from the **sale** pages |
| `*/20 6-19 * * *` | Polls anything closing soon, appends to `prices/<date>.jsonl` |

The watch list is built from sale pages rather than lot pages for two reasons:
a room sale's lots carry no clock of their own (it lives on the sale page), and
one sale page lists 20 lots, so ~70 requests cover a 1 400-lot sale instead of
1 400.

## Output

One JSON object per observation, appended to `prices/<date>.jsonl`:

```json
{"lot_id":"alcopa:1102539","url":"https://…","ends_ts":1788764400,
 "offset":-20,"observed_at":1788764380.4,"price":700,
 "state":"en_cours","fees":"en_sus"}
```

`offset` is seconds relative to the close, so the last row before `0` is the
best available estimate of the hammer price.

## Setup

One repository secret is required:

- **`GEMINI_API_KEY`** — an AI Studio key, used only to read the CAPTCHA grid.
  Roughly 1 900 tokens per solve.

## Fee note

`fees` records what the lot page says, and it changes the real cost a lot:

- `inclus` — web sale. The last bid is the final price; only a €25 export sale
  fee is added.
- `en_sus` — room / LIVE sale. Add the buyer's premium of **14.40%** (minimum
  €360), a **€141.67** export sale fee, and €45 if bidding through Alcopa LIVE
  (€80 via Interenchères).

Sourced from the official CGV PDF, read 2026-09-02. Export fees are quoted
excluding VAT and are charged as quoted — do not gross them up.
