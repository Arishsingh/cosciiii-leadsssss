<div align="center">

# CoSci Leads

**A location and a number, in. Ranked real-estate builders with name, phone and email, out.**

`name · phone · email (+confidence) · RERA no. · promoter · projects · score`

<sub>Electron desktop app · Python scraper · SQLite · no server</sub>

</div>

---

A plain Google Maps search for "builders in Pune" returns mostly brokers and
material suppliers. This cross-references the **state RERA promoter registry**,
so what comes out is registered developers — verified, sized by project count,
scored and ranked.

## Sources

| Source | Supplies | Cost |
|---|---|---|
| **MahaRERA** registry | registration no., promoter, district, **project count** | free |
| **K-RERA** registry | registration no., promoter, project count *(no district published)* | free |
| **Google Maps** — Apify actor, Playwright fallback | phone, website, address, rating, sometimes email | Apify usage |
| **Website crawl** (`enrich.py`) | the email, from the company's own contact page | free |

The registry verifies and sizes; Maps and the crawl supply contacts. Promoter
phone and email on MahaRERA sit behind a CAPTCHA, and this project does not
attempt to bypass it — that split is what makes the broker filter possible.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r scraper/requirements.txt
playwright install chromium          # browser fallback only

cd desktop && npm install && npm start
```

Add an Apify token in **Settings** for the fast Maps path. Without one the app
drives a real browser — fine for testing, against Google's ToS at volume.

## First run

1. **Registry → Harvest registry.** Once, ~2.5s/page; 200 pages ≈ 8 min, the
   full 1249 ≈ 50 min. Every later search then reads it locally, instantly.
2. Type a **district/city**, optionally an **area**, set a count, **Find builders**.

Both fields autocomplete from the registry. **Area** takes a taluka/locality
(`Haveli`, `Andheri`) or a 6-digit **pincode** (`411057`), and narrows both the
registry lookup and the Maps query. For Pune: 2,263 promoters district-wide,
1,338 in Haveli, 137 for pincode 411057.

Results stay **inside the district you typed** — Google's neighbouring-district
drift is dropped and reported, and Navi Mumbai never answers a Mumbai search.
A pincode area pins results to that pincode exactly.

Export **CSV** (all 23 columns) or **PDF** (a landscape report with stats header
and ranked table). Both export exactly what the filters currently show.

The stats strip reports honest fill rates, not one flattering total:

```
214 builders · 96 verified email · 137 any email · 203 phone · 88 RERA
```

> **A realistic email fill rate is 45–50%.** Anything claiming 90% is
> pattern-guessing without verification — that blacklists your sending domain.

## CLI

```bash
python scraper/scraper.py --mode harvest --state mh --max-pages 200   # ~1 hr
python scraper/scraper.py --mode harvest --state ka                   # ~20 s

python scraper/scraper.py --mode search --location Pune --max-results 200
python scraper/scraper.py --mode search --location Pune --area 411057
python scraper/scraper.py --mode search --location Thane --only-verified-email
```

Flags: `--source {auto,apify,browser}` · `--no-enrich` · `--no-rera` ·
`--keep-brokers` · `--only-with-email` · `--json` (NDJSON, as the app runs it).

Tests are plain `unittest`, no network:

```bash
python scraper/tests/test_place.py && python scraper/tests/test_search_place.py
```

## How records merge

Everything lands in SQLite (`data/leads.db`), de-duplicated by a cascade of
decreasing confidence (`db.py::find_match`):

| # | Signal | Why it is trusted this much |
|---|---|---|
| 1 | RERA registration no. | state-issued id — exact |
| 2 | Normalized phone | last 10 digits, `+91`/`0` stripped |
| 3 | Website domain | shared hosts (99acres, IndiaMART…) excluded |
| 4 | Fuzzy name **+ geography** | `M/s. Kumar Builders Pvt. Ltd.` → `KUMAR`, compared only within a pincode/district |

Name+address matching alone fails here: RERA writes `Survey No. 42/1B,
Hinjewadi Phase 2` where Maps writes `Hinjawadi Rd`. The cascade is what lets a
Maps listing inherit a registration number instead of looking like an anonymous
agency. Merged records carry their provenance in `sources`, e.g. `enrich,gmaps,rera`.

## Email confidence

Never assumed, always labelled.

| Value | Meaning |
|---|---|
| `verified` | on the company's own site **and** the domain has MX records |
| `site-crawl` | on the site, MX unconfirmed |
| `generic` | a role address (`info@`, `sales@`) |
| `guess` | pattern-inferred, unconfirmed — never sent by default |

## Lead score

`scoring.py`, 0–100 and explainable: verified email 30 · RERA registered 25 ·
phone 20 · 5+ projects 15 (2–4: 8) · website 10 · LinkedIn 8. A guessed email
scores nothing. Results rank by it, so a capped search returns the *best* N.

## Portal quirks

Verified against the live site, all in `sources/rera.py` — not to be "simplified" away:

- Pagination is 1-indexed; `page=0` aliases to `page=1`.
- GET filter params are **ignored** — filtering works only by POSTing the Drupal
  form with a fresh `form_build_id`, after which `?page=N` stays scoped to it.
- An unfiltered walk stops at **page 1249** (~12,490 projects).

## Sources tested and rejected

| Source | Result |
|---|---|
| 99acres, Zauba Corp | HTTP 403 |
| Justdial | 200, empty body (blocked) |
| IndiaMART, MagicBricks | 404 on category URLs |
| CREDAI Maharashtra | members area login-gated |
| Telangana RERA | CAPTCHA-gated |
| Goa RERA | only negative lists and aggregate counts |
| LinkedIn (direct) | needs proxies/throwaway accounts; not attempted |

LinkedIn URLs come instead from the builders' own sites during enrichment
(~32% hit rate). Lifting that means a paid search API.

## Legal note

Business contact details from public registries are defensible for B2B outreach
under the DPDP Act 2023; cold-calling Indian mobiles on the DND registry is not.
Every record stores `sources` and timestamps — prefer email, honour opt-outs,
don't resell the database.

## Layout

```
scraper/
  scraper.py      orchestrator — harvest and search modes
  db.py           SQLite store + the dedupe cascade
  place.py        district gate — keeps a search inside the place you typed
  normalize.py    phone/domain/name normalization (the basis of every merge)
  enrich.py       website email discovery + MX verification
  scoring.py      lead score
  vertical.py     search phrasings + broker filter
  mapping.py      the 23-column schema boundary
  sources/        rera.py · apify.py · browser.py
  tests/          pure-logic and stubbed end-to-end checks
desktop/          Electron app (spawns the scraper, reads NDJSON from stdout)
data/leads.db     SQLite (gitignored)
output/           timestamped CSVs (gitignored)
```

<sub>The original n8n workflow is kept at `google-maps-lead-gen.n8n.json` for reference.</sub>
