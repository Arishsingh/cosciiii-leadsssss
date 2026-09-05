# Builder Leads — Maharashtra

Turn **a location and a number** into a ranked, de-duplicated list of real-estate
builders with **name, phone and email**, as a desktop app.

Built on top of the original Google Maps lead scraper, but specialised for
Indian real estate: it cross-references the **MahaRERA promoter registry** so the
output is actual registered developers rather than the brokers and material
suppliers that dominate a plain Maps search.

```
output: name · phone · email (+confidence) · RERA no. · promoter · projects · score
```

---

## How it gets the data

Three sources, merged into one company record:

| Source | Supplies | Cost |
|---|---|---|
| **MahaRERA registry** (Maharashtra) | registration no., promoter name, district, **project count** | free |
| **K-RERA registry** (Karnataka) | registration no., promoter name, **project count** (no district published) | free |
| **Google Maps** (Apify actor, or Playwright fallback) | phone, website, address, rating, sometimes email | Apify usage |
| **Website crawl** (`enrich.py`) | the email, from the company's own contact page | free |

### What MahaRERA does and does not give you

MahaRERA's search page (`/promoters-search-result`) is freely readable: no
CAPTCHA, no token, server-rendered. It supplies the registration number,
promoter name, district and project list.

It does **not** supply promoter phone or email. Those live on the project detail
pages at `maharerait.maharashtra.gov.in`, which are **CAPTCHA-gated**, and whose
backing API returns `401` without a token. That gate is an intentional
anti-automation control and this project does not attempt to bypass it.

So the registry's job is *verification and sizing*, not contacts. Contacts come
from Maps and the website crawl. In practice that is the better split anyway —
it is what makes the broker filter possible.

Two portal quirks the harvester has to handle (both verified against the live
site, both in `sources/rera.py`):

- Pagination is 1-indexed, and `page=0` is an alias for `page=1`.
- The GET filter parameters are **ignored**. Filtering only works by POSTing the
  Drupal form with a fresh `form_build_id`; the filter is then held in the
  session and ordinary `?page=N` GETs stay scoped to it.
- An unfiltered walk stops at **page 1249** (~12,490 projects). Deeper coverage
  needs name-partitioned POST searches.

---

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r scraper/requirements.txt
playwright install chromium          # only needed for the browser fallback

cd desktop && npm install && npm start
```

Add an Apify token in **Settings** for the fast, reliable Maps path. Without
one the app falls back to driving a real browser against Google Maps, which is
slower and against Google's ToS at volume — fine for testing, not for
production use.

---

## First run

1. Open **registry** in the top bar and press **Harvest registry**.
   ~2.5s per page; 200 pages ≈ 8 minutes, the full 1249 ≈ 50 minutes.
   Do this once — every later search then reads it locally and returns instantly.
2. Type a location and a count, press **Find builders**.

The stats strip reports the honest fill rates rather than just a total:

```
214 builders · 96 verified email · 137 any email · 203 phone · 88 RERA
```

**A realistic email fill rate is 45–50%.** Anything claiming 90% is pattern-guessing
without verification, which will get your sending domain blacklisted.

---

## CLI

```bash
# bulk-load a state registry
python scraper/scraper.py --mode harvest --state mh --max-pages 200   # Maharashtra, ~1hr
python scraper/scraper.py --mode harvest --state ka                   # Karnataka, ~20s

# search
python scraper/scraper.py --mode search --location Pune --max-results 200
python scraper/scraper.py --mode search --location Thane --only-verified-email
```

Useful flags: `--source {auto,apify,browser}`, `--no-enrich`, `--no-rera`,
`--keep-brokers`, `--only-with-email`, `--json`.

---

## How records are merged

Everything lands in SQLite (`data/leads.db`) and is de-duplicated by a
**cascade of decreasing confidence** (`db.py::find_match`):

1. **RERA registration number** — a state-issued id, so exact.
2. **Normalized phone** — last 10 digits, `+91`/`0` stripped.
3. **Website domain** — with shared hosts (99acres, IndiaMART, Facebook…) excluded,
   since many builders list a portal as "their" site.
4. **Fuzzy name, gated on geography** — names are stripped of legal and industry
   filler (`M/s. Kumar Builders Pvt. Ltd.` → `KUMAR`) and only compared within
   the same pincode or district.

Plain name+address matching does not work here: RERA writes
`Survey No. 42/1B, Hinjewadi Phase 2` where Maps writes `Hinjawadi Rd`. The
cascade is what lets a Maps listing inherit a registration number and stop
looking like an anonymous agency.

A merged record shows its provenance in `sources`, e.g. `enrich,gmaps,rera`.

## Email confidence

Never assumed, always labelled:

| Value | Meaning |
|---|---|
| `verified` | found on the company's own site **and** the domain has MX records |
| `site-crawl` | found on the site, MX unconfirmed |
| `generic` | a role address (`info@`, `sales@`) rather than a person |
| `guess` | inferred from a pattern, not confirmed — never sent by default |

## Lead score

`scoring.py`, 0–100 and explainable: verified email 30 · phone 20 ·
RERA registered 25 · 5+ projects 15 (2–4: 8) · website 10.
Results are ranked by it, so a capped search returns the *best* N, not an
arbitrary N.

---

## Sources tested and rejected

Checked directly rather than assumed, so they are not worth re-testing:

| Source | Result |
|---|---|
| 99acres, Zauba Corp | HTTP 403 |
| Justdial | 200 with an empty body (blocked) |
| IndiaMART, MagicBricks | 404 on category URLs |
| CREDAI Maharashtra chapters | members area is login-gated |
| Telangana RERA | search form is CAPTCHA-gated |
| Goa RERA | publishes only negative lists (cancelled/withdrawn) and aggregate counts |
| LinkedIn (direct) | needs proxies/throwaway accounts to evade blocking; not attempted |

LinkedIn URLs are instead read from the builders' own websites during the
enrichment crawl (~32% hit rate). Lifting that materially would mean a paid
search API (SerpAPI/Bing/Google CSE) querying
`site:linkedin.com/company "<builder>"`.

## Legal note

Business contact details from public registries are defensible for B2B outreach
under the DPDP Act 2023, but cold-calling Indian mobile numbers on the DND
registry is not. Every record stores its `sources` and timestamps; prefer email
over cold calls, honour opt-outs, and don't resell the database.

---

## Layout

```
scraper/
  scraper.py      orchestrator — harvest and search modes
  db.py           SQLite store + the dedupe cascade
  normalize.py    phone/domain/name normalization (the basis of every merge)
  enrich.py       website email discovery + MX verification
  scoring.py      lead score
  vertical.py     real-estate search phrasings + broker filter
  mapping.py      the 22-column schema boundary
  sources/
    rera.py       MahaRERA registry
    apify.py      Google Maps via Apify
    browser.py    Google Maps via Playwright (fallback)
desktop/          Electron app (spawns the scraper, reads NDJSON from stdout)
data/leads.db     SQLite (gitignored)
output/           timestamped CSVs (gitignored)
```

The original Google Maps n8n workflow is kept at
`google-maps-lead-gen.n8n.json` for reference.
