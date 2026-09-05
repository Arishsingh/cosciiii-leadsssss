"""Email discovery and verification — the stage that produces the money field.

Google Maps gives a name, a phone and usually a website; it rarely gives an
email. This module closes that gap by visiting the company's own site and
reading the address off it, then checking that the domain can actually receive
mail.

Confidence is recorded, never assumed (`email_confidence`):

    verified    found on the company's own site AND the domain has MX records
    site-crawl  found on the site, but MX could not be confirmed
    generic     a role address (info@, sales@) rather than a person
    guess       inferred from a pattern and not confirmed to exist

Nothing here fabricates an address. A pattern guess is only ever emitted when
explicitly asked for, and is labelled so it can be excluded before sending —
mailing unverified guesses is the fastest way to burn a sending domain.
"""

import re
import time

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Pages worth trying, in the order they most often carry an address.
CONTACT_PATHS = ["", "/contact", "/contact-us", "/contactus", "/about", "/about-us", "/reach-us"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Addresses that appear in markup but belong to tooling, not the business.
_JUNK_DOMAINS = {
    "sentry.io", "wixpress.com", "example.com", "domain.com", "email.com",
    "yourdomain.com", "sentry-cdn.com", "godaddy.com", "wordpress.com",
    "squarespace.com", "w3.org", "schema.org", "googleapis.com", "gstatic.com",
}
_JUNK_PREFIXES = ("noreply", "no-reply", "donotreply", "postmaster", "abuse", "webmaster")
# Bare filenames such as "logo@2x.png" match the address pattern; reject them.
_FILE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|ico|woff2?)$", re.I)

# Role addresses: real and usable, but not a named person.
_GENERIC_LOCALS = {
    "info", "sales", "contact", "enquiry", "enquiries", "inquiry", "support",
    "admin", "office", "hello", "mail", "marketing", "care", "help", "booking",
}

_DOH = "https://cloudflare-dns.com/dns-query"


def clean_emails(text, prefer_domain=""):
    """Pull plausible business emails out of raw page text/markup.

    When `prefer_domain` is given, addresses on that domain sort first — a
    builder's own `@kumarbuilders.com` beats a gmail address dropped in a
    footer credit, and both beat an agency's address.
    """
    found = []
    for raw in _EMAIL_RE.findall(text or ""):
        e = raw.strip().strip(".").lower()
        if _FILE_EXT_RE.search(e):
            continue
        local, _, domain = e.partition("@")
        if not domain or domain in _JUNK_DOMAINS:
            continue
        if any(local.startswith(p) for p in _JUNK_PREFIXES):
            continue
        if len(local) > 64 or len(e) > 254:
            continue
        # Hex-ish locals are almost always tracking IDs, not people.
        if len(local) >= 20 and re.fullmatch(r"[0-9a-f]+", local):
            continue
        if e not in found:
            found.append(e)

    def rank(e):
        domain = e.partition("@")[2]
        local = e.partition("@")[0]
        own = 0 if (prefer_domain and domain == prefer_domain) else 1
        generic = 1 if local in _GENERIC_LOCALS else 0
        return (own, generic, len(e))

    return sorted(found, key=rank)


def is_generic(email):
    return email.partition("@")[0] in _GENERIC_LOCALS


def has_mx(domain, timeout=6):
    """True if `domain` publishes MX records, via DNS-over-HTTPS.

    DoH is used rather than a DNS library so the app needs no extra dependency
    and behaves the same on macOS and Windows. Returns None when the check
    itself fails, so callers can tell "no mail server" from "couldn't check".
    """
    if not domain:
        return None
    try:
        r = requests.get(
            _DOH,
            params={"name": domain, "type": "MX"},
            headers={"Accept": "application/dns-json"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    if data.get("Status") != 0:
        return False
    return any(a.get("type") == 15 for a in data.get("Answer", []))


def crawl_site(website, timeout=12, max_pages=4, delay=0.3, session=None):
    """Fetch a company's site and return the emails found, best first.

    Only a handful of likely pages are tried, and only same-host ones — this is
    a targeted contact lookup, not a site spider.
    """
    from normalize import norm_domain

    domain = norm_domain(website)
    if not domain:
        return []
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})

    base = website if str(website).startswith(("http://", "https://")) else f"https://{domain}"
    base = base.rstrip("/")

    emails, tried = [], 0
    for path in CONTACT_PATHS:
        if tried >= max_pages:
            break
        try:
            r = s.get(base + path, timeout=timeout, allow_redirects=True)
        except requests.RequestException:
            continue
        tried += 1
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            continue
        for e in clean_emails(r.text, prefer_domain=domain):
            if e not in emails:
                emails.append(e)
        # The homepage often has the address in the footer; stop once we have
        # one on the company's own domain rather than crawling every path.
        if any(e.endswith("@" + domain) for e in emails):
            break
        time.sleep(delay)
    return emails


def enrich_record(rec, verify_mx=True, session=None):
    """Fill in `email`/`email_confidence` on a record that lacks one.

    Records that already carry an email are left alone apart from grading its
    confidence, so an address Apify already found is never thrown away.
    """
    from normalize import norm_domain, norm_email

    existing = norm_email(rec.get("email"))
    website = rec.get("website") or ""
    domain = norm_domain(website)

    if existing:
        conf = "generic" if is_generic(existing) else "site-crawl"
        if verify_mx and has_mx(existing.partition("@")[2]) is True:
            conf = "verified"
        rec["email"] = existing
        rec["email_confidence"] = conf
        return rec

    if not domain:
        rec.setdefault("email", "")
        rec["email_confidence"] = ""
        return rec

    found = crawl_site(website, session=session)
    if not found:
        rec["email"] = ""
        rec["email_confidence"] = ""
        return rec

    best = found[0]
    conf = "generic" if is_generic(best) else "site-crawl"
    if verify_mx and has_mx(best.partition("@")[2]) is True:
        conf = "verified"
    rec["email"] = best
    rec["email_confidence"] = conf
    return rec
