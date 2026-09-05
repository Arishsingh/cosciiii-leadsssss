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
import threading
import time

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Pages worth trying, in the order they most often carry an address.
# Ordered by how often each carries contact details. The crawl stops as soon as
# it has what it needs, so the later paths are only paid for when the homepage
# came up empty.
CONTACT_PATHS = ["", "/contact", "/contact-us", "/contactus", "/about", "/about-us", "/reach-us"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Cloudflare's email obfuscation replaces the address with a hex blob; the page
# renders it in JavaScript. It is extremely common on small business sites, so
# without decoding it a large share of contact pages look like they have no
# email at all.
_CF_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')

# Addresses written to dodge naive scrapers: "info [at] example [dot] com".
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\s+at\s+)\s*"
    r"([A-Za-z0-9.\-]+)\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*([A-Za-z]{2,})",
    re.I,
)

# Links worth following when they appear on the homepage. Matching the actual
# anchors beats guessing fixed paths: sites use /contact-us, /reach-us,
# /get-in-touch, /enquiry and plenty else besides.
_CONTACT_HINT = re.compile(r"contact|about|reach|touch|enquir|inquir|connect|team", re.I)


def decode_cf_email(hex_blob):
    """Decode one Cloudflare-obfuscated address.

    The first byte is an XOR key applied to every following byte.
    """
    try:
        data = bytes.fromhex(hex_blob)
    except ValueError:
        return ""
    if len(data) < 2:
        return ""
    key = data[0]
    try:
        return "".join(chr(b ^ key) for b in data[1:])
    except ValueError:
        return ""

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

# MX answers are per-domain and stable, and many builders share a provider
# (gmail.com alone covers a large slice of Indian SME contacts). Caching turns
# one lookup per lead into one per distinct domain. Guarded because enrichment
# runs across a thread pool.
_MX_CACHE = {}
_MX_LOCK = threading.Lock()


def clean_emails(text, prefer_domain=""):
    """Pull plausible business emails out of raw page text/markup.

    Handles plain addresses, Cloudflare-obfuscated ones, and the
    "info [at] example [dot] com" dodge. When `prefer_domain` is given,
    addresses on that domain sort first — a builder's own
    `@kumarbuilders.com` beats a gmail address dropped in a footer credit, and
    both beat an agency's address.
    """
    text = text or ""
    candidates = list(_EMAIL_RE.findall(text))
    candidates += [decode_cf_email(h) for h in _CF_RE.findall(text)]
    candidates += [f"{a}@{b}.{c}" for a, b, c in _OBFUSCATED_RE.findall(text)]

    found = []
    for raw in candidates:
        if not raw:
            continue
        e = raw.strip().strip(".").lower()
        if "@" not in e:
            continue
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
    Results are cached per domain for the life of the process.
    """
    if not domain:
        return None
    with _MX_LOCK:
        if domain in _MX_CACHE:
            return _MX_CACHE[domain]
    result = _has_mx_uncached(domain, timeout)
    # Only cache a definite answer; a failed check should be retried later.
    if result is not None:
        with _MX_LOCK:
            _MX_CACHE[domain] = result
    return result


def _has_mx_uncached(domain, timeout=6):
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


def enrich_many(records, verify_mx=True, workers=10, on_result=None, should_stop=None):
    """Enrich records concurrently, yielding each as it completes.

    Enrichment is almost entirely network wait — several page fetches plus a DNS
    lookup per company — so running it serially made it the slowest stage of a
    search by a wide margin. A thread pool turns it into roughly one company's
    latency for the whole batch.

    Only the network happens here. Database writes stay with the caller on the
    main thread, because a SQLite connection may not be shared across threads.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not records:
        return

    def work(item):
        key, rec = item
        if should_stop and should_stop():
            return key, rec
        # requests.Session is not thread-safe, so each task gets its own.
        with requests.Session() as sess:
            try:
                enrich_record(rec, verify_mx=verify_mx, session=sess)
            except Exception:  # noqa: BLE001 — one bad site never fails the batch
                pass
        return key, rec

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, item) for item in records]
        for fut in as_completed(futures):
            if should_stop and should_stop():
                break
            try:
                key, rec = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if on_result:
                on_result(key, rec)
            yield key, rec


def crawl_site(website, timeout=8, max_pages=3, delay=0.0, session=None):
    """Fetch a company's site and return {"emails": [...], "linkedin": [...]}.

    One crawl serves both: the LinkedIn URL is almost always a footer social
    icon on the same pages that carry the email, so fetching twice would double
    the requests for nothing.

    Only a handful of likely pages are tried, and only same-host ones — this is
    a targeted contact lookup, not a site spider.
    """
    from normalize import find_linkedin, norm_domain

    domain = norm_domain(website)
    if not domain:
        return {"emails": [], "linkedin": []}
    s = session or requests.Session()
    s.headers.update({"User-Agent": UA})

    base = website if str(website).startswith(("http://", "https://")) else f"https://{domain}"
    base = base.rstrip("/")

    emails, links, tried = [], [], 0
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
        for u in find_linkedin(r.text):
            if u not in links:
                links.append(u)
        # The homepage usually carries both in the footer; stop once we have an
        # address on the company's own domain rather than crawling every path.
        if any(e.endswith("@" + domain) for e in emails) and links:
            break
        time.sleep(delay)
    return {"emails": emails, "linkedin": links}


def enrich_record(rec, verify_mx=True, session=None):
    """Fill in `email`/`email_confidence` on a record that lacks one.

    Records that already carry an email are left alone apart from grading its
    confidence, so an address Apify already found is never thrown away.
    """
    from normalize import norm_domain, norm_email, norm_linkedin

    existing = norm_email(rec.get("email"))
    existing_li = norm_linkedin(rec.get("linkedin"))
    website = rec.get("website") or ""
    domain = norm_domain(website)

    def grade(email):
        conf = "generic" if is_generic(email) else "site-crawl"
        if verify_mx and has_mx(email.partition("@")[2]) is True:
            conf = "verified"
        return conf

    # Nothing to crawl: keep whatever the record already had.
    if not domain:
        rec["email"] = existing
        rec["email_confidence"] = grade(existing) if existing else ""
        rec["linkedin"] = existing_li
        return rec

    # Crawl when either field is still missing — one fetch serves both.
    if existing and existing_li:
        rec["email"] = existing
        rec["email_confidence"] = grade(existing)
        rec["linkedin"] = existing_li
        return rec

    found = crawl_site(website, session=session)
    email = existing or (found["emails"][0] if found["emails"] else "")
    rec["email"] = email
    rec["email_confidence"] = grade(email) if email else ""
    rec["linkedin"] = existing_li or (found["linkedin"][0] if found["linkedin"] else "")
    return rec
