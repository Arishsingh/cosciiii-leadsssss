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
from enrich import enrich_many
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
                                delay=args.delay, on_page=on_page,
                                start_page=args.start_page):
            store.upsert_rera_project(row)
            n += 1
            # Commit every page rather than every fifth. A harvest runs for the
            # best part of an hour, and sqlite3 holds a write transaction open
            # until commit(), so a longer batch means any search running
            # alongside it waits seconds for its first write.
            if n % 10 == 0:
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
        store.set_meta("rera_projects", store.rera_count())
        resume = args.start_page + (n // 10)
        emitter.error(f"RERA harvest stopped after {n} projects: {e} "
                      f"— resume with --start-page {resume}")
        return 1
    finally:
        store.close()
    return 0


# ------------------------------------------------------------------- search

def _seed_from_registry(store, location, limit, emitter, publish):
    """Insert cached RERA promoters for `location`, publishing each immediately.

    This is the fast path and the reason a search feels instant: the registry is
    local, so names, registration numbers and project counts are available in
    well under a second. They are streamed straight out rather than held back
    until the network stages finish. Promoters arrive busiest-first, so the
    largest developers are on screen first.
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
        publish(cid)
        n += 1
        if n % 100 == 0:
            store.commit()  # keep the write lock short-lived
    store.commit()
    return n


def _sweep_maps(args, store, emitter, publish):
    """Query Google Maps (Apify, else browser), publishing each place as it lands."""
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
        got += 1
        if got % 10 == 0:
            # Commit as we go: sqlite3 holds a write transaction open from the
            # first INSERT until commit(), so one big transaction would lock the
            # database for the whole sweep and block a concurrent harvest.
            store.commit()
        publish(cid)

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
    """Produce a ranked builder list, streaming results as they become known.

    Ordering of the stages is deliberate. The registry is read first because it
    is local and instant, so the user sees real builder names immediately rather
    than watching a spinner through the network stages. Maps records then land
    on top of those already-identified promoters and inherit their registration
    numbers. Enrichment runs last and concurrently, republishing rows in place
    as contacts arrive.
    """
    store = LeadStore(args.db)
    os.makedirs(args.output_dir, exist_ok=True)
    csv_path = _csv_path(args.output_dir, args.location, _now_stamp())

    live = {}      # company id -> latest record, the single source of truth
    dropped = {"n": 0}

    def snapshot(cid):
        row = store.get(cid)
        if row is None:
            return None
        return {c: row[c] for c in COLUMNS if c in row.keys()}

    def publish(cid, rec=None):
        """Score a company and stream it to the UI, replacing any earlier copy.

        Every row carries its company id so the renderer updates in place: a
        Maps sweep enriching a promoter already on screen must not append a
        second row for the same firm.

        Deliberately does no database write. Scores are held in memory and
        persisted once at the end: making the UI wait on a write lock meant that
        while a registry harvest was running, the first row took ten seconds to
        appear instead of a sixth of one.
        """
        rec = rec or snapshot(cid)
        if rec is None:
            return
        if not args.keep_brokers and looks_like_broker(rec):
            if cid not in live:
                dropped["n"] += 1
            return
        score, _ = score_lead(rec)
        rec["lead_score"] = score
        rec["id"] = cid
        live[cid] = rec
        emitter.lead(rec)

    try:
        # 1. Registry spine — instant, and the source of the builder proof.
        if not args.no_rera:
            if store.rera_count() == 0:
                emitter.status("rera", "fallback",
                               "local RERA registry is empty — run a harvest to enable "
                               "RERA verification and project counts")
            else:
                _seed_from_registry(store, args.location, args.max_results * 3,
                                    emitter, publish)
                emitter.progress(len(live))

        # 2. Contacts from Google Maps.
        if not _stopping():
            _sweep_maps(args, store, emitter, publish)
            emitter.progress(len(live))

        if dropped["n"]:
            emitter.status("-", "scraping",
                           f"filtered out {dropped['n']} broker / supplier listings")

        # 3. Enrichment: concurrent, and republishing rows in place as it goes.
        need = [(cid, dict(rec)) for cid, rec in live.items()
                if (rec.get("website") or "").strip()
                and not ((rec.get("email") or "").strip() and (rec.get("linkedin") or "").strip())]
        if need and not args.no_enrich:
            emitter.status("enrich", "starting",
                           f"looking for emails and LinkedIn on {len(need)} sites…")
            done = 0
            for cid, rec in enrich_many(need, verify_mx=not args.no_verify,
                                        workers=args.enrich_workers,
                                        should_stop=_stopping):
                done += 1
                if rec.get("email") or rec.get("linkedin"):
                    store.upsert(rec, "enrich")
                    publish(cid)
                if done % 10 == 0:
                    store.commit()
                    emitter.status("enrich", "scraping", f"{done}/{len(need)} sites checked")
            store.commit()

        # 4. Rank and export. Best first, so a capped export is the top N.
        # Persist the scores now, in one transaction, rather than once per row
        # during streaming.
        for cid, rec in live.items():
            store.set_score(cid, rec["lead_score"])
        store.commit()

        final = sorted(live.values(), key=lambda r: (-r["lead_score"], r.get("name") or ""))
        if args.only_with_email:
            final = [r for r in final if (r.get("email") or "").strip()]
        if args.only_verified_email:
            final = [r for r in final if r.get("email_confidence") == "verified"]
        final = final[: args.max_results]

        writer = CsvWriter(csv_path)
        try:
            for rec in final:
                writer.write(rec)
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
    p.add_argument("--start-page", type=int, default=1,
                   help="harvest: resume from this page instead of the first")
    p.add_argument("--no-rera", action="store_true", help="skip the RERA registry spine")
    p.add_argument("--no-enrich", action="store_true", help="skip website email discovery")
    p.add_argument("--no-verify", action="store_true", help="skip MX verification")
    p.add_argument("--enrich-workers", type=int, default=10,
                   help="concurrent website crawls during enrichment")
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
