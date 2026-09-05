"""Normalization helpers — the foundation of cross-source deduplication.

Every merge decision in db.py is made on the *normalized* forms produced here,
never on raw strings. RERA writes "Survey No. 42/1B, Hinjewadi Phase 2, Pune"
where Google Maps writes "Hinjawadi Rd, Pune" for the same firm, so raw address
or raw-name comparison finds nothing. These functions strip everything that
varies between sources and keep only what identifies the company.
"""

import re
from difflib import SequenceMatcher

# Legal-form and industry words that appear or vanish depending on the source.
# "Lodha Developers Pvt Ltd" on RERA is "Lodha Group" on Maps — dropping these
# is what lets the two match. Order matters: longest forms first so that
# "PRIVATE LIMITED" is consumed before "LIMITED" can match half of it.
_NOISE_WORDS = [
    "PRIVATE LIMITED", "PVT LTD", "PVT. LTD.", "PRIVATE LTD", "P LTD",
    "LIMITED", "LTD", "LLP", "LLC", "INC",
    "AND COMPANY", "AND CO", "& CO", "COMPANY",
    "DEVELOPERS", "DEVELOPER", "DEVELOPMENT", "DEVELOPMENTS",
    "BUILDERS", "BUILDER", "BUILDCON", "BUILDTECH", "BUILDWELL",
    "CONSTRUCTIONS", "CONSTRUCTION", "CONSTRUCTIONS CO",
    "INFRASTRUCTURE", "INFRASTRUCTURES", "INFRA", "INFRATECH",
    "PROPERTIES", "PROPERTY", "PROJECTS", "PROJECT",
    "REALTY", "REALTORS", "REALTOR", "REAL ESTATE", "ESTATES", "ESTATE",
    "ENTERPRISES", "ENTERPRISE", "ASSOCIATES", "ASSOCIATION",
    "CORPORATION", "CORP", "VENTURES", "VENTURE",
    "GROUP", "HOUSING", "HOMES", "LANDMARKS", "SHELTERS", "SPACES",
]

_LEGAL_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _NOISE_WORDS) + r")\b")


def norm_phone(value):
    """Reduce an Indian phone number to its bare 10 digits, or "" if unusable.

    Sources write the same number as "+91 98765 43210", "098765-43210" and
    "9876543210". Only the trailing 10 digits are stable, and a valid Indian
    mobile starts 6-9 — anything else (landline fragments, truncated junk) is
    rejected rather than stored, because a wrong phone key merges two unrelated
    companies into one row.
    """
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return ""
    # Drop country/trunk prefixes so all three spellings collapse to one key.
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    digits = digits.lstrip("0")
    if len(digits) < 10:
        return ""
    digits = digits[-10:]
    return digits if digits[0] in "6789" else ""


def norm_domain(value):
    """Extract the bare registrable host from a URL or email, lowercased.

    Used as a dedupe key one rung below phone: two records sharing a website
    are the same business even when their names and addresses disagree.
    """
    if not value:
        return ""
    v = str(value).strip().lower()
    if "@" in v and "/" not in v:  # an email address — take the domain half
        v = v.rsplit("@", 1)[-1]
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    v = v.split(":")[0]  # strip any port
    if v.startswith("www."):
        v = v[4:]
    # Reject anything that isn't plausibly a hostname.
    return v if re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", v) else ""


def norm_name(value):
    """Collapse a company name to its distinctive core for fuzzy matching.

    Uppercases, strips punctuation and legal/industry filler, and squeezes
    whitespace, so "M/s. Kumar Builders Pvt. Ltd." and "KUMAR DEVELOPERS"
    both reduce to "KUMAR".
    """
    if not value:
        return ""
    v = str(value).upper()
    v = re.sub(r"\bM/?S\.?\b", " ", v)          # the ubiquitous "M/s." prefix
    v = re.sub(r"[^\w\s]", " ", v)               # punctuation -> space
    v = re.sub(r"\s+", " ", v).strip()
    v = _LEGAL_RE.sub(" ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


def name_key(value):
    """Order-independent form of a normalized name.

    "SHREE GANESH" and "GANESH SHREE" are the same firm listed two ways, so the
    tokens are sorted before joining.
    """
    return " ".join(sorted(norm_name(value).split()))


def name_similarity(a, b):
    """0.0-1.0 similarity between two company names, order-independent."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    return SequenceMatcher(None, ka, kb).ratio()


def norm_pincode(value):
    """Return a 6-digit Indian PIN, or "" — used to gate fuzzy name matches."""
    if not value:
        return ""
    m = re.search(r"\b(\d{6})\b", str(value))
    return m.group(1) if m else ""


def norm_email(value):
    """Lowercase and validate an email, returning "" if it isn't one."""
    if not value:
        return ""
    v = str(value).strip().lower()
    return v if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", v) else ""


def norm_city(value):
    """Uppercase, punctuation-free city/district name for coarse comparison."""
    if not value:
        return ""
    return re.sub(r"[^\w\s]", " ", str(value).upper()).strip()
