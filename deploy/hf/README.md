---
title: Alcopa Watcher
emoji: 🔨
colorFrom: gray
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

# Alcopa deadline watcher

Records what cars actually sell for at Alcopa. The site deletes a lot's price
the second it sells — the page keeps the photos and specs but shows no number
and no clock — so the only chance to capture it is the last seconds before the
sale closes. That has to run somewhere unattended; this is that somewhere.

## Setup

This Space needs two secrets (Settings → Variables and secrets):

| secret | why |
|---|---|
| `GEMINI_API_KEY` | reads the WAF's 3x3 captcha grid so a token can be minted |
| `HF_TOKEN` | write token, to push each day's capture to a dataset |
| `HF_DATASET` | e.g. `wancea23/alcopa-prices` — free Spaces have no durable disk |

Without the last two the captures still appear under `/files`, but a restart
takes them, and a missed close cannot be scraped again.

## Watch it

- `/` — preflight result, phase, captures, recent log
- `/health` — for the uptime ping

**Point UptimeRobot at `/health`.** A free Space pauses after 48h without
traffic, and a paused watcher is indistinguishable from a working one that
happens to capture nothing.

## The preflight matters

The status page shows the result of one request to the site's `robots.txt`:

- **405** — the AWS WAF captcha challenge. Normal; the solver handles it.
- **403** — CloudFront has banned this IP outright. Nothing can be done from
  this host; that is what GitHub Actions' Azure ranges return, and it is why
  this Space exists instead of a scheduled workflow.
