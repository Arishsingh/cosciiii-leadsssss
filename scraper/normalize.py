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
    return key_similarity(name_key(a), name_key(b))


def key_similarity(ka, kb, threshold=0.0):
    """Similarity between two *already normalized* name keys.

    Takes keys rather than raw names because callers usually have them stored
    already, and re-normalizing on every comparison dominated the cost of
    matching (hundreds of thousands of regex substitutions per run).

    `threshold` enables an exact early exit. SequenceMatcher's ratio is
    2*M/(len(a)+len(b)) with M <= min(len(a), len(b)), so when
    2*min/(len_a+len_b) is already below the threshold the pair cannot possibly
    reach it and the expensive comparison is skipped. This is a bound, not a
    heuristic: it never changes the outcome.
    """
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    if threshold:
        upper = 2.0 * min(len(ka), len(kb)) / (len(ka) + len(kb))
        if upper < threshold:
            return 0.0
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


# Company pages and personal profiles; `pub` is the legacy profile form.
_LINKEDIN_RE = re.compile(
    r"(?:https?:)?(?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(company|in|pub|school)/([A-Za-z0-9_\-%.]+)",
    re.I,
)


def norm_linkedin(value):
    """Return a canonical LinkedIn URL, or "".

    Accepts a bare handle, a messy URL with tracking parameters, or a country
    subdomain (`in.linkedin.com`), and returns one stable form so the same page
    found on two different sites doesn't look like two different links.
    """
    if not value:
        return ""
    v = str(value).strip()
    m = _LINKEDIN_RE.search(v)
    if m:
        kind, handle = m.group(1).lower(), m.group(2).rstrip("/.")
        if kind == "pub":
            kind = "in"
        return f"https://www.linkedin.com/{kind}/{handle}"
    # A bare "company/foo" or handle, as some directories store it.
    m = re.match(r"^(company|in)/([A-Za-z0-9_\-%.]+)$", v, re.I)
    if m:
        return f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2).rstrip('/.')}"
    return ""


def find_linkedin(text):
    """Return every canonical LinkedIn URL in a blob of text/markup, best first.

    Company pages are ranked above personal profiles: for B2B outreach the firm's
    page is the useful one, and a personal profile in a site footer is often the
    web designer's rather than the builder's.
    """
    out = []
    for kind, handle in _LINKEDIN_RE.findall(text or ""):
        url = norm_linkedin(f"https://www.linkedin.com/{kind}/{handle}")
        if url and url not in out:
            out.append(url)
    return sorted(out, key=lambda u: (0 if "/company/" in u else 1, len(u)))


def norm_city(value):
    """Uppercase, punctuation-free city/district name for coarse comparison."""
    if not value:
        return ""
    return re.sub(r"[^\w\s]", " ", str(value).upper()).strip()


def display_phone(value):
    """Strip the domestic trunk prefix from a number kept for display.

    Maps hands back numbers in national dialling form — "086513 57287",
    "022 2269 1166". The leading 0 is only how you dial the number from
    inside India, not part of the number itself, so it is dropped for
    display and export. Spacing is left exactly as the source wrote it.
    """
    v = str(value or "").strip()
    if not v.startswith("0"):
        return v
    return v.lstrip("0").lstrip()
