#!/usr/bin/env python3
"""Real-estate builder lead scraper — orchestrates registry + Maps + enrichment.

Two modes:

    harvest   Bulk-load the MahaRERA promoter registry into SQLite. Slow, run
              occasionally. Turns every later search into a local query.
    search    Produce a ranked builder list for a location. Reads the cached
              registry, sweeps Google Maps for contacts, merges the two, fills
              in missing emails, scores and exports.

The pipeline, and why it is ordered this way:

    RERA registry (who is a real developer, how many projects)
        + Google Maps (phone, website, sometimes email)
        -> merge in db.py by RERA no. / phone / domain / fuzzy name+place
        -> enrich: crawl the site for a missing email, check MX
        -> filter out brokers, score, rank
        -> CSV

RERA is read first so that Maps records land on top of an already-identified
promoter rather than the other way round: that is what lets a Maps listing
inherit a registration number and stop looking like an anonymous agency.

With --json, stdout is newline-delimited JSON for the Electron app. The event
contract is `status`, `progress`, `lead`, `stats`, `done`, `error`.
"""

import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import get_apify_token
from db import LeadStore
from enrich import enrich_record
from events import CsvWriter, Emitter
from mapping import COLUMNS, map_record
from scoring import score_lead
from sources import apify
from vertical import looks_like_broker, search_queries

MAX_RESULTS = 100000

_STOP = {"flag": False}


def _handle_stop(signum, frame):
    _STOP["flag"] = True


def _stopping():
    return _STOP["flag"]


def _slug(text, fallback):
    text = (text or "").strip().lower()
    if not text:
        return fallback
    out = "".join(c if c.isalnum() else "-" for c in text)
    return "-".join(p for p in out.split("-") if p) or fallback


def _now_stamp():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _csv_path(output_dir, location, stamp):
    base = os.path.join(output_dir, f"builders_{stamp}_{_slug(location, 'location')}")
    path, n = base + ".csv", 2
    while os.path.exists(path):
        path, n = f"{base}_{n}.csv", n + 1
    return path


# ------------------------------------------------------------------ harvest

def run_harvest(args, emitter):
    """Bulk-load the MahaRERA registry into SQLite."""
    from sources import rera

    store = LeadStore(args.db)
    emitter.status("rera", "starting",
                   "harvesting the MahaRERA registry — this is a one-off bulk load")
    n = 0
    try:
        def on_page(page, count):
            emitter.progress(n)
            if page % 10 == 0:
                emitter.status("rera", "scraping", f"page {page} — {n} projects stored")

        for row in rera.harvest(max_pages=args.max_pages, should_stop=_stopping,
                                delay=args.delay, on_page=on_page):
            store.upsert_rera_project(row)
            n += 1
            if n % 50 == 0:
                store.commit()
                emitter.progress(n)
        store.commit()
        store.set_meta("rera_last_harvest", _now_stamp())
        store.set_meta("rera_projects", store.rera_count())
        total = store.rera_count()
        emitter.status("rera", "done",
                       f"registry now holds {total} projects "
                       f"({len(store.rera_districts())} districts)")
        emitter.done(n, "")
    except Exception as e:  # noqa: BLE001 — surface any failure as an event
        store.commit()
        emitter.error(f"RERA harvest failed after {n} projects: {e}")
        return 1
    finally:
        store.close()
    return 0


# ------------------------------------------------------------------- search

def _seed_from_registry(store, location, limit, emitter, seen_ids):
    """Insert cached RERA promoters for `location` as the spine of the search.

    Collects the resulting company ids directly rather than re-querying for
    them afterwards — `upsert` already returns the id it wrote to, including
    when it merged into a company an earlier source created.
    """
    rows = store.rera_promoters(district=location, limit=limit)
    if not rows:
        return 0
    emitter.status("rera", "scraping",
                   f"{len(rows)} registered promoters matched '{location}' in the local registry")
    n = 0
    for r in rows:
        rec = {
            "name": r["promoter_name"],
            "promoter_name": r["promoter_name"],
            "city": r["district"] or r["locality"],
            "district": r["district"],
            "state": r["state"],
            "postal_code": r["pincode"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
            "rera_number": r["rera_number"],
            "active_projects": r["active_projects"],
            "category": "Registered promoter (MahaRERA)",
        }
        cid, _ = store.upsert(rec, "rera", r["rera_number"])
        seen_ids.add(cid)
        n += 1
        if n % 100 == 0:
            store.commit()  # keep the write lock short-lived, as above
    store.commit()
    return n


def _sweep_maps(args, store, emitter, seen_ids):
    """Query Google Maps (Apify, else browser) and merge results into the store."""
    queries = search_queries(args.location)
    token = get_apify_token()
    mode = args.source
    got = 0

    def absorb(raw, source):
        nonlocal got
        lead = map_record(raw)
        if not (lead.get("name") or "").strip():
            return
        cid, _ = store.upsert(lead, source, lead.get("google_maps_url", ""))
        seen_ids.add(cid)
        got += 1
        if got % 10 == 0:
            # Commit as we go, not just at the end. Python's sqlite3 holds a
            # write transaction open from the first INSERT until commit(), so
            # batching the whole sweep into one transaction would lock the
            # database for minutes and block a concurrent registry harvest.
            store.commit()
            emitter.progress(len(seen_ids))

    used_apify = False
    if mode in ("auto", "apify") and not _stopping():
        if token:
            emitter.status("apify", "starting",
                           f"Apify sweep — {len(queries)} query phrasings for {args.location}")
            try:
                records = apify.fetch(queries, args.max_results, token)
                used_apify = True
                emitter.status("apify", "scraping", f"received {len(records)} raw places")
                for raw in records:
                    if _stopping():
                        break
                    absorb(raw, "gmaps")
            except apify.ApifyError as e:
                if mode == "apify":
                    emitter.error(f"Apify failed: {e}")
                else:
                    emitter.status("apify", "fallback", f"Apify failed ({e}); using browser")
        elif mode == "apify":
            emitter.error("Apify mode selected but no token is set — add one in Settings.")
        else:
            emitter.status("apify", "fallback", "no Apify token; using the browser scraper")

    want_browser = mode == "browser" or (mode == "auto" and (not used_apify or got == 0))
    if not _stopping() and want_browser:
        try:
            from sources import browser
            for q in queries:
                if _stopping():
                    break
                emitter.status("browser", "starting", q)
                for raw in browser.scrape(
                    q, args.max_results,
                    lambda st, msg: emitter.status("browser", st, msg),
                    should_stop=_stopping,
                ):
                    if _stopping():
                        break
                    absorb(raw, "gmaps")
        except ImportError:
            emitter.error("Playwright not installed — run: pip install -r scraper/requirements.txt "
                          "&& playwright install chromium")
        except Exception as e:  # noqa: BLE001
            emitter.error(f"browser scrape failed: {e}")

    store.commit()
    return got


def run_search(args, emitter):
    store = LeadStore(args.db)
    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = _csv_path(args.output_dir, args.location, _now_stamp())
    seen_ids = set()

    try:
        # 1. Registry spine — free, instant, and the source of the builder proof.
        if not args.no_rera:
            if store.rera_count() == 0:
                emitter.status("rera", "fallback",
                               "local RERA registry is empty — run a harvest to enable "
                               "RERA verification and project counts")
            else:
                _seed_from_registry(store, args.location, args.max_results * 3,
                                    emitter, seen_ids)
                emitter.progress(len(seen_ids))

        # 2. Contacts from Google Maps.
        if not _stopping():
            _sweep_maps(args, store, emitter, seen_ids)

        # 3. Materialize, filter, enrich, score.
        emitter.status("-", "scraping", f"merging and scoring {len(seen_ids)} candidates…")
        leads, dropped = [], 0
        for cid in seen_ids:
            row = store.get(cid)
            if row is None:
                continue
            rec = {c: row[c] for c in COLUMNS if c in row.keys()}
            if not args.keep_brokers and looks_like_broker(rec):
                dropped += 1
                continue
            leads.append((cid, rec))

        if dropped:
            emitter.status("-", "scraping",
                           f"filtered out {dropped} broker / supplier listings")

        # Enrichment is the slow, networked step, so it runs only on records
        # that lack an email and actually have a site to read.
        # Anything with a site but a gap in either contact field is worth a crawl;
        # one fetch fills both.
        need = [(cid, r) for cid, r in leads
                if (r.get("website") or "").strip()
                and not ((r.get("email") or "").strip() and (r.get("linkedin") or "").strip())]
        if need and not args.no_enrich:
            emitter.status("enrich", "starting",
                           f"looking for emails on {len(need)} company websites…")
            import requests as _rq
            sess = _rq.Session()
            for i, (cid, rec) in enumerate(need, 1):
                if _stopping():
                    break
                try:
                    enrich_record(rec, verify_mx=not args.no_verify, session=sess)
                except Exception:  # noqa: BLE001 — one bad site never aborts a run
                    continue
                if rec.get("email") or rec.get("linkedin"):
                    store.upsert({**rec, "name": rec["name"]}, "enrich")
                if i % 5 == 0:
                    store.commit()
                    emitter.status("enrich", "scraping", f"{i}/{len(need)} sites checked")
            store.commit()

        # Re-read so scores reflect everything the merge and enrichment wrote.
        final = []
        for cid, _ in leads:
            row = store.get(cid)
            if row is None:
                continue
            rec = {c: row[c] for c in COLUMNS if c in row.keys()}
            score, reasons = score_lead(rec)
            rec["lead_score"] = score
            store.set_score(cid, score)
            rec["_reasons"] = reasons
            final.append(rec)
        store.commit()

        # Best leads first — a capped export should return the top of the list,
        # not an arbitrary slice of it.
        final.sort(key=lambda r: (-r["lead_score"], r.get("name") or ""))
        if args.only_with_email:
            final = [r for r in final if (r.get("email") or "").strip()]
        if args.only_verified_email:
            final = [r for r in final if r.get("email_confidence") == "verified"]
        final = final[: args.max_results]

        writer = CsvWriter(csv_path)
        try:
            for rec in final:
                writer.write(rec)
                emitter.lead({k: v for k, v in rec.items() if k != "_reasons"})
        finally:
            writer.close()

        emitter.stats(_summarize(final))
        emitter.done(len(final), csv_path)
    except Exception as e:  # noqa: BLE001
        emitter.error(str(e))
        return 1
    finally:
        store.close()
    return 0


def _summarize(leads):
    """Counts the UI shows instead of a bare total — the fill rates are the story."""
    total = len(leads)
    with_email = sum(1 for r in leads if (r.get("email") or "").strip())
    verified = sum(1 for r in leads if r.get("email_confidence") == "verified")
    with_phone = sum(1 for r in leads if (r.get("phone") or "").strip())
    with_rera = sum(1 for r in leads if (r.get("rera_number") or "").strip())
    with_site = sum(1 for r in leads if (r.get("website") or "").strip())
    return {
        "total": total,
        "with_email": with_email,
        "verified_email": verified,
        "with_phone": with_phone,
        "with_rera": with_rera,
        "with_website": with_site,
        "phone_only": with_phone - with_email if with_phone > with_email else 0,
        "email_rate": round(100.0 * with_email / total, 1) if total else 0.0,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Real-estate builder lead scraper")
    p.add_argument("--mode", choices=["search", "harvest"], default="search")
    p.add_argument("--location", default="", help="City/district, e.g. 'Pune'")
    p.add_argument("--max-results", type=int, default=100)
    p.add_argument("--source", choices=["auto", "apify", "browser"], default="auto")
    p.add_argument("--db", default=None, help="SQLite path (default data/leads.db)")
    p.add_argument("--max-pages", type=int, default=None, help="harvest: page cap")
    p.add_argument("--delay", type=float, default=0.6, help="harvest: seconds between pages")
    p.add_argument("--no-rera", action="store_true", help="skip the RERA registry spine")
    p.add_argument("--no-enrich", action="store_true", help="skip website email discovery")
    p.add_argument("--no-verify", action="store_true", help="skip MX verification")
    p.add_argument("--keep-brokers", action="store_true", help="do not filter non-builders")
    p.add_argument("--only-with-email", action="store_true")
    p.add_argument("--only-verified-email", action="store_true")
    p.add_argument("--output-dir",
                   default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output"))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    args.max_results = max(1, min(args.max_results, MAX_RESULTS))
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    emitter = Emitter(args.json)

    if args.mode == "harvest":
        return run_harvest(args, emitter)
    if not args.location.strip():
        emitter.error("--location is required for a search")
        return 1
    return run_search(args, emitter)


if __name__ == "__main__":
    sys.exit(main())
