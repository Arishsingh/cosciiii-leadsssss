# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this repo is

A desktop app that turns **a location + a count** into ranked real-estate
**builder** leads (name, phone, email). It began as a generic Google Maps lead
scraper and was specialised for Indian real estate; the Maps machinery is still
the contact source, but a **MahaRERA registry cross-reference** is what makes
the output builders rather than brokers.

Read `README.md` first — it documents the data sources and the two MahaRERA
access quirks. The original n8n workflow (`google-maps-lead-gen.n8n.json`) is
kept only as reference for the original 15-column mapping.

## Commands

```bash
# scraper (venv at repo root is auto-detected by the app)
python3 -m venv venv && source venv/bin/activate
pip install -r scraper/requirements.txt && playwright install chromium

python scraper/scraper.py --mode harvest --max-pages 200          # bulk registry load
python scraper/scraper.py --mode search --location Pune --max-results 200
python scraper/scraper.py --mode search --location Pune --json    # NDJSON, as the app runs it

cd desktop && npm install && npm start
```

There is **no test suite or lint config.** Verify changes by running the scraper
with `--json` and checking the NDJSON stream plus the generated
`output/builders_*.csv`. Pure-logic checks (normalization, the dedupe cascade,
`rera.parse_page` against saved HTML, scoring) are the fastest way to validate
without hitting the network.

## Architecture

```
RERA registry (who is a real developer + project counts)
  + Google Maps via Apify/Playwright (phone, website, sometimes email)
  -> merge in db.py: RERA no. -> phone -> domain -> fuzzy name+place
  -> enrich.py: crawl the site for a missing email, check MX
  -> vertical.py broker filter -> scoring.py -> ranked CSV
```

Facts that span multiple files:

- **`scraper/mapping.py::COLUMNS` is the schema boundary, and the 22 columns are
  load-bearing in three places**: `mapping.py`, `events.py::CsvWriter`, and
  `desktop/main.js::COLUMNS` (the filtered-export writer). Change one, change
  all three, or the export CSV drifts from the run CSV. The first 15 keep their
  original order and meaning; the 7 real-estate fields are **appended, never
  inserted**.

- **`db.py::find_match` is the dedupe cascade, and its order encodes how much
  each signal proves.** Name matching is deliberately last and gated on
  pincode/district — ungated it produces false merges, because "Kumar Builders"
  exists in most districts. `_SHARED_HOSTS` exists because many builders list a
  portal (99acres, IndiaMART) as their website; matching on those would collapse
  unrelated firms into one row.

- **Merging never destroys data.** A blank incoming field leaves the stored one
  alone, and `email` only moves *up* the `EMAIL_CONFIDENCE_RANK` ladder — a
  freshly-guessed address must not overwrite a verified one.

- **RERA is read before Maps in `run_search`, deliberately.** Seeding the
  registry first means Maps records land on top of an already-identified
  promoter and inherit its registration number, rather than creating an
  anonymous duplicate that a later pass has to reconcile.

- **`sources/rera.py` encodes three verified portal quirks** — 1-indexed
  pagination with `page=0` aliasing to 1, GET filter params being ignored (only
  the Drupal POST filters, and its `form_build_id` is emitted **only on a
  session's first GET**, hence the cache), and the 1249-page cap. All three were
  confirmed against the live site; do not "simplify" them away.

- **Promoter email/phone are CAPTCHA-gated on MahaRERA and are not scraped.**
  If asked to add them, say no — bypassing that control is out of scope. The
  contact path is Maps + `enrich.py`.

- **Electron ↔ Python is stdout NDJSON, not a server.** `desktop/main.js` spawns
  `scraper/scraper.py --json` and parses newline-delimited JSON. The event
  shapes are the interface: `status`, `progress`, `lead`, `stats`, `done`,
  `error` (plus an `exit` main.js synthesizes). Keep the contract stable when
  editing either side.

- **The renderer is sandboxed**: `contextIsolation: true`, `nodeIntegration:
  false`, and a CSP that blocks remote code. Everything privileged crosses via
  `preload.js`'s `contextBridge`. The Apify token is never sent to the renderer
  — only a `hasToken` boolean. `main.js` shells out to `scraper/status.py` for
  registry stats because the main process has no SQLite driver.

- **`publish()` in `run_search` must never write to the database.** Scores are
  held in memory and persisted once at the end. This is a performance
  invariant, not a style choice: when it called `set_score` per row, a search
  running alongside a registry harvest took ~9.9s to show its first builder
  because the very first UI row blocked on the write lock. Write-free, it is
  ~0.11s under the same contention.

- **Streaming order is deliberate.** The registry publishes first (local,
  instant, biggest developers first), Maps republishes those same rows with
  contacts, and enrichment republishes again. Every row carries its company
  `id`; the renderer keys on it to update in place, so a promoter enriched
  later does not appear twice. Ranking happens once, on `done` — sorting live
  would make rows jump under the cursor.

- **Long-running writers commit incrementally.** sqlite3 holds a write
  transaction from the first INSERT until `commit()`, so both the harvest (per
  page) and the Maps sweep (every 10 records) commit as they go. Batching them
  larger reintroduces multi-second stalls for anything else touching the file.

- **Enrichment is concurrent** (`enrich.enrich_many`, default 10 workers) and
  does network only — SQLite writes stay on the calling thread, because a
  connection cannot be shared across threads. MX results are cached per domain.

- **A stop is cooperative**: SIGTERM sets `_STOP`, the loops exit, the CSV is
  flushed, and main.js escalates to killing the process *group* after 4s
  (the child is `detached` so Chromium dies with it).

## Conventions

- Comments explain **why**, not what — especially where behaviour looks odd but
  is compensating for a portal quirk. Those comments are load-bearing.
- Every network source degrades rather than aborts: one bad site, one
  unparseable card, or a failed Apify call must never end a run.
- Be honest in the UI about fill rates. The stats strip reports verified vs.
  total on purpose; do not replace it with a single flattering number.
