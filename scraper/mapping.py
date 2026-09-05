"""Normalize raw place records into the 15 canonical lead columns.

This is the single schema boundary for the scraper: every record from any source
(Apify or the Playwright browser fallback) passes through `map_record` and comes
out with exactly the same columns as the n8n `Map Fields` node. Every field is
guarded so a missing value never breaks a row.
"""

# The canonical column order. Matches the n8n workflow's Map Fields node and the
# Google Sheet header row, so the CSV is a drop-in replacement for the n8n output.
COLUMNS = [
    "name",
    "category",
    "full_address",
    "city",
    "state",
    "postal_code",
    "phone",
    "website",
    "email",
    "rating",
    "reviews_count",
    "opening_hours",
    "latitude",
    "longitude",
    "google_maps_url",
    # --- real-estate additions -------------------------------------------
    # Appended, never inserted: the first 15 keep their original order so an
    # existing CSV or Google Sheet built on this schema still lines up.
    "rera_number",       # MahaRERA registration no. — proof this is a developer
    "promoter_name",     # the person/entity behind the firm, for salutations
    "promoter_type",     # Individual / Company / LLP — a size proxy
    "active_projects",   # registered project count, best size signal available
    "email_confidence",  # verified | site-crawl | generic | guess | ""
    "lead_score",        # 0-100, see scoring.py
    "sources",           # comma-joined provenance, e.g. "gmaps,rera"
]


def _first_email(rec):
    emails = rec.get("emails")
    if isinstance(emails, list) and emails:
        return emails[0] or ""
    return ""


def _category(rec):
    if rec.get("categoryName"):
        return rec["categoryName"]
    cats = rec.get("categories")
    if isinstance(cats, list) and cats:
        return ", ".join(str(c) for c in cats if c)
    return ""


def _opening_hours(rec):
    hours = rec.get("openingHours")
    if isinstance(hours, list) and hours:
        parts = []
        for h in hours:
            if isinstance(h, dict):
                parts.append(f"{h.get('day', '')}: {h.get('hours', '')}")
        return " | ".join(parts)
    return ""


def map_record(rec):
    """Map one raw Apify/browser place dict to the canonical lead dict.

    Mirrors the guarded expressions from the n8n Map Fields node one-for-one.
    """
    rec = rec or {}
    location = rec.get("location") if isinstance(rec.get("location"), dict) else {}

    return {
        "name": rec.get("title") or "",
        "category": _category(rec),
        "full_address": rec.get("address") or "",
        "city": rec.get("city") or "",
        "state": rec.get("state") or "",
        "postal_code": rec.get("postalCode") or "",
        "phone": rec.get("phone") or rec.get("phoneUnformatted") or "",
        "website": rec.get("website") or "",
        "email": _first_email(rec),
        "rating": rec.get("totalScore") if rec.get("totalScore") is not None else "",
        "reviews_count": rec.get("reviewsCount") if rec.get("reviewsCount") is not None else 0,
        "opening_hours": _opening_hours(rec),
        "latitude": location.get("lat", "") if location else "",
        "longitude": location.get("lng", "") if location else "",
        "google_maps_url": rec.get("url") or "",
        # Present on merged/RERA records; absent on a raw Maps record, in which
        # case they stay blank and are filled in later by the merge in db.py.
        "rera_number": rec.get("rera_number") or "",
        "promoter_name": rec.get("promoter_name") or "",
        "promoter_type": rec.get("promoter_type") or "",
        "active_projects": rec.get("active_projects") or 0,
        "email_confidence": rec.get("email_confidence") or "",
        "lead_score": rec.get("lead_score") or 0,
        "sources": rec.get("sources") or "",
    }


def dedupe_key(lead):
    """Dedup key matching n8n Remove Duplicates: name + full_address."""
    return (lead.get("name", "").strip().lower(), lead.get("full_address", "").strip().lower())
