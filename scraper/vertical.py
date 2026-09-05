"""Real-estate vertical: what to search for, and what counts as a builder.

Google Maps has no "property developer" category that maps cleanly onto Indian
builders. One firm lists itself as a Builder, the next as a Real Estate Agency,
the third as a General Contractor — so a single query finds a fraction of the
market and drags in a lot of noise either way.

Two knobs live here:

* `SEARCH_TEMPLATES` — the phrasings swept for a location.
* `looks_like_broker` — the filter that keeps the list useful. Maps is full of
  brokers, interior designers and material suppliers labelling themselves as
  builders; without this the export is mostly not builders.

A RERA registration always overrides the filter: the state has certified the
firm as a promoter, which outranks whatever it called itself on Maps.
"""

import re

SEARCH_TEMPLATES = [
    "builders in {location}",
    "real estate developers in {location}",
    "property developers in {location}",
    "construction companies in {location}",
    "new residential projects in {location}",
]

# Words that mark a listing as something other than a developer.
_BROKER_WORDS = [
    "broker", "brokerage", "property dealer", "property consultant",
    "real estate agent", "estate agent", "realtor", "property agent",
    "consultancy", "consultants", "advisory",
    "interior", "architect", "furnishing", "modular kitchen",
    "cement", "steel", "hardware", "sanitary", "tiles", "paint", "plywood",
    "building material", "packers", "movers", "loan", "finance", "mortgage",
    "valuer", "surveyor", "vastu", "legal", "lawyer", "society",
    "rental", "rent", "pg ", "hostel", "co-working", "coworking",
]
_BROKER_RE = re.compile("|".join(re.escape(w) for w in _BROKER_WORDS), re.I)

# Positive markers that a listing really is a developer.
_BUILDER_WORDS = [
    "builder", "developer", "construction", "infra", "realty", "projects",
    "housing", "homes", "estates", "landmark", "shelter", "township",
    "promoters", "buildcon", "buildtech", "constructions",
]
_BUILDER_RE = re.compile("|".join(re.escape(w) for w in _BUILDER_WORDS), re.I)


def search_queries(location, area=""):
    """Query strings for a location, narrowed to `area` when one is given.

    Google reads "builders in Wakad, Pune" as the neighbourhood search it is,
    which is far more targeted than sweeping the whole district and filtering
    afterwards.
    """
    where = f"{area.strip()}, {location}" if (area or "").strip() else location
    return [t.format(location=where) for t in SEARCH_TEMPLATES]


def looks_like_broker(rec):
    """True when a record should be dropped as not-a-builder.

    Errs toward keeping: a record is only rejected when a broker/supplier word
    appears and nothing suggests it develops property. Dropping a real builder
    costs more than carrying one broker that the user can eyeball.
    """
    if (rec.get("rera_number") or "").strip():
        return False  # state-certified promoter — the filter does not apply
    haystack = " ".join(
        str(rec.get(f) or "") for f in ("name", "category", "full_address")
    )
    if not _BROKER_RE.search(haystack):
        return False
    return not _BUILDER_RE.search(haystack)


def is_builder_signal(rec):
    """True when the name/category positively indicates a developer."""
    haystack = " ".join(str(rec.get(f) or "") for f in ("name", "category"))
    return bool(_BUILDER_RE.search(haystack))
