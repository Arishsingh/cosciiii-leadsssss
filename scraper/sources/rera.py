"""MahaRERA promoter registry — bulk harvest and targeted lookup.

**What is and isn't reachable.** MahaRERA has two public surfaces:

1. `maharera.maharashtra.gov.in/promoters-search-result` — a Drupal page that
   server-renders 10 result cards per page. No CAPTCHA, no token, no JS.
   This is what this module reads.
2. `maharerait.maharashtra.gov.in/public/project/view/<id>` — the project
   detail SPA. It is **CAPTCHA-gated**, and its backing API
   (`/api/maha-rera-public-view-project-registration-service/...`) answers 401
   without a token. Promoter **email and phone live behind that gate.**

So this module yields **no contact details, by design**. Its job is to answer
"is this a real registered developer, where do they build, and how many
projects do they have?" Contacts are joined in later from the Google Maps
sources, which do publish them. The CAPTCHA is a deliberate anti-automation
control and is not circumvented.

**Two access quirks, both load-bearing (verified against the live site):**

- Pagination is 1-indexed and `page=0` is an alias for `page=1`.
- The GET query parameters (`promoter_name`, `promoter_location`,
  `promoter_division`) are **ignored** — passing them returns the unfiltered
  list. Filtering only works by POSTing the Drupal form (`promoters_name`,
  with a fresh `form_build_id`), after which the filter is held in the session
  and ordinary `?page=N` GETs stay filtered.
- Unfiltered pagination is capped at **page 1249** (~12,490 projects). Deeper
  coverage requires partitioning the search by name via POST.
"""

import re
import time
from html import unescape

import requests

BASE = "https://maharera.maharashtra.gov.in/promoters-search-result"
FORM_ID = "promoters_search_page_form"

# The portal stops serving results past this page on an unfiltered walk.
MAX_UNFILTERED_PAGE = 1249

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CARD_SPLIT = re.compile(r'(?=<div class="row shadow p-3 mb-5 bg-body rounded")')
_RE_REG = re.compile(r'<p class="p-0">\s*#\s*([A-Z]\d{6,})\s*</p>')
_RE_PROJECT = re.compile(r'<h4 class="title4">\s*<strong>(.*?)</strong>', re.S)
_RE_PROMOTER = re.compile(r'<p class="darkBlue bold\s*">(.*?)</p>', re.S)
_RE_LATLNG = re.compile(r'maps/search/\?api=1&(?:amp;)?query=([\d.]+),([\d.]+)')
_RE_LOCALITY = re.compile(r'fa-location-dot"></em>\s*(.*?)\s*</a>', re.S)
_RE_PROJECT_ID = re.compile(r'/public/project/view/(\d+)')
_RE_FIELD = re.compile(r'<div class="greyColor">\s*([^<]+?)\s*</div>\s*<p>\s*(.*?)\s*</p>', re.S)
_RE_BUILD_ID = re.compile(r'name="form_build_id" value="([^"]+)"')


class ReraError(RuntimeError):
    pass


def _text(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_page(html):
    """Parse one results page into project dicts. Pure — testable offline."""
    out = []
    for card in _CARD_SPLIT.split(html):
        if "row shadow p-3" not in card:
            continue
        cut = card.find("<footer")
        if cut != -1:
            card = card[:cut]
        reg = _RE_REG.search(card)
        promoter = _RE_PROMOTER.search(card)
        if not reg or not promoter:
            continue
        fields = {_text(k).lower(): _text(v) for k, v in _RE_FIELD.findall(card)}
        latlng = _RE_LATLNG.search(card)
        project = _RE_PROJECT.search(card)
        locality = _RE_LOCALITY.search(card)
        pid = _RE_PROJECT_ID.search(card)
        out.append({
            "rera_number": reg.group(1),
            "project_name": _text(project.group(1)) if project else "",
            "promoter_name": _text(promoter.group(1)),
            "locality": _text(locality.group(1)) if locality else "",
            "district": fields.get("district", ""),
            "division": fields.get("division", ""),
            "state": fields.get("state", "") or "MAHARASHTRA",
            "pincode": fields.get("pincode", ""),
            "last_modified": fields.get("last modified", ""),
            "latitude": latlng.group(1) if latlng else "",
            "longitude": latlng.group(2) if latlng else "",
            "project_id": pid.group(1) if pid else "",
        })
    return out


class ReraSession:
    """A cookie-backed session against the MahaRERA search page.

    Holds the Drupal session that a filtered POST establishes, so subsequent
    page GETs stay scoped to that filter.
    """

    def __init__(self, delay=0.6, timeout=45):
        self.delay = delay
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE,
        })
        self._fbid = None

    def _get(self, **params):
        try:
            r = self.s.get(BASE, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise ReraError(f"network error: {e}") from e
        if r.status_code != 200:
            raise ReraError(f"HTTP {r.status_code}")
        return r.text

    def _form_build_id(self):
        """Return this session's Drupal form build id, fetching it once.

        Only the *first* GET of a session carries a `form_build_id`; once the
        session has state, Drupal serves the form without one. So it is fetched
        lazily and cached rather than re-read before every POST.
        """
        if self._fbid:
            return self._fbid
        m = _RE_BUILD_ID.search(self._get())
        if not m:
            raise ReraError("could not read form_build_id — page layout changed")
        self._fbid = m.group(1)
        return self._fbid

    def apply_name_filter(self, name):
        """POST the search form to scope this session to `name` (substring match)."""
        data = {
            "promoters_name": name,
            "promoter_state": "",
            "promoter_district": "",
            "op": "Search",
            "form_build_id": self._form_build_id(),
            "form_id": FORM_ID,
        }
        try:
            r = self.s.post(BASE, data=data, timeout=self.timeout)
        except requests.RequestException as e:
            raise ReraError(f"network error on search POST: {e}") from e
        if r.status_code != 200:
            raise ReraError(f"HTTP {r.status_code} on search POST")
        return parse_page(r.text)

    def page(self, n):
        return parse_page(self._get(page=n))


def _walk(session, first_rows, max_pages, should_stop, on_page):
    """Yield rows from page 1 onward, stopping at the end of the result set.

    The portal serves the *last valid page* rather than an empty one when you
    run past the end, so a page whose registration numbers exactly repeat the
    previous page's means the walk is finished.
    """
    seen_sig = None
    rows = first_rows
    page = 1
    while True:
        if should_stop and should_stop():
            return
        if not rows:
            return
        sig = tuple(r["rera_number"] for r in rows)
        if sig == seen_sig:
            return
        seen_sig = sig
        for r in rows:
            yield r
        if on_page:
            on_page(page, len(rows))
        page += 1
        if max_pages is not None and page > max_pages:
            return
        if page > MAX_UNFILTERED_PAGE:
            return
        time.sleep(session.delay)
        rows = session.page(page)


def harvest(max_pages=None, on_page=None, should_stop=None, delay=0.6):
    """Yield every project reachable by an unfiltered walk (up to the cap).

    ~12,490 projects at 10 per page. At the default delay this is roughly an
    hour, which is why it is a background bulk job cached in SQLite rather than
    something run per search.
    """
    sess = ReraSession(delay=delay)
    yield from _walk(sess, sess.page(1), max_pages, should_stop, on_page)


def search_by_name(name, max_pages=40, on_page=None, should_stop=None, delay=0.6):
    """Yield projects whose promoter name contains `name` (case-insensitive).

    Used both for targeted lookups and to partition a deep harvest past the
    unfiltered page cap.
    """
    sess = ReraSession(delay=delay)
    yield from _walk(sess, sess.apply_name_filter(name), max_pages, should_stop, on_page)
