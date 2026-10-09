"""Fallback source: scrape Google Maps directly with Playwright.

Used when Apify is unavailable (no token, error, or zero results). Drives a real
Chromium browser, scrolls the results feed, opens each place, and yields raw
records shaped with the SAME keys the Apify actor uses, so they pass through the
shared mapping.py unchanged.

This path is inherently more fragile than Apify (Google Maps markup changes, and
it can hit consent walls / rate limits). Every field extraction is guarded; a
place that fails to parse is skipped rather than aborting the run. Email is not
available from Maps here (no scrapeContacts equivalent), so it is left blank.
"""

import re
import time

SEARCH_URL = "https://www.google.com/maps/search/{query}"

# Coords appear in a place URL two ways: the @lat,lng,zoom viewport chunk, and the
# !3d<lat>!4d<lng> place chunk. The latter is the actual place location and is
# present in the feed hrefs, so we prefer it.
_LATLNG_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
_DATA_LATLNG_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


def _parse_address_parts(address):
    """Best-effort city/state/postal from a free-form address.

    Handles the common '..., City, State POSTAL[, Country]' shape used by Google
    Maps (e.g. 'Ahmedabad, Gujarat 380015' or 'Austin, TX 78701'). Returns blanks
    when the trailing part doesn't look like that, rather than guessing wrong.
    """
    city = state = postal = ""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if not parts:
        return city, state, postal
    last = parts[-1]
    # Drop a trailing country if present (leaves 'State POSTAL' as the last part).
    if re.fullmatch(r"[A-Za-z ]+", last) and len(parts) >= 3:
        parts = parts[:-1]
        last = parts[-1]
    m = re.search(r"(\d[\d ]{3,7})$", last)
    if m:
        postal = m.group(1).replace(" ", "")
        state = last[: m.start()].strip()
        if len(parts) >= 2:
            city = parts[-2]
    return city, state, postal


def _accept_consent(page):
    """Best-effort dismissal of Google's cookie/consent interstitial."""
    for label in ("Accept all", "Reject all", "I agree", "Accept the use of cookies"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() > 0:
                btn.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def _collect_place_urls(page, feed, max_results, should_stop, on_status=None):
    """Scroll the results feed, harvesting place links until we have enough, the
    list ends, or growth truly stalls.

    Harvesting happens AFTER each scroll so staleness reflects real growth — the
    previous version compared counts without re-harvesting in between, so it
    always looked stale and quit after ~6 scrolls, capping results far below the
    requested max.
    """
    seen = []
    seen_set = set()

    def harvest():
        anchors = feed.locator('a[href*="/maps/place/"]')
        for i in range(anchors.count()):
            try:
                href = anchors.nth(i).get_attribute("href")
            except Exception:
                href = None
            if href and href not in seen_set:
                seen_set.add(href)
                seen.append(href)

    harvest()  # results already rendered on first paint
    stale_rounds = 0
    # Allow several no-growth rounds before giving up — Google loads in bursts and
    # a slow batch shouldn't be mistaken for the end of the list.
    while len(seen) < max_results and stale_rounds < 8:
        if should_stop():
            break
        before = len(seen)
        # Jump to the bottom of the feed to trigger Google's lazy loading.
        try:
            feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(2000)
        harvest()
        if on_status and len(seen) != before:
            on_status("scraping", f"collecting places… {len(seen)} so far")
        # Stop early if Google says we've reached the end.
        try:
            if page.get_by_text(re.compile(r"reached the end of the list|you've reached the end", re.I)).count() > 0:
                break
        except Exception:
            pass
        stale_rounds = stale_rounds + 1 if len(seen) == before else 0
    return seen[:max_results]


def _text_or_empty(locator):
    try:
        if locator.count() > 0:
            return (locator.first.inner_text(timeout=1500) or "").strip()
    except Exception:
        pass
    return ""


def _attr_or_empty(locator, attr):
    try:
        if locator.count() > 0:
            return (locator.first.get_attribute(attr) or "").strip()
    except Exception:
        pass
    return ""


def _parse_place(page, url):
    """Open a place URL and extract a raw record (Apify key shape)."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)
    return _read_place(page, url)


def _read_place(page, url):
    """Extract a raw record from the place panel already open in `page`."""
    rec = {}
    rec["url"] = page.url

    # name: the main H1 heading of the place panel.
    rec["title"] = _text_or_empty(page.locator("h1"))

    # category: button under the rating with the place's primary type.
    rec["categoryName"] = _text_or_empty(page.locator('button[jsaction*="category"]'))

    # address / phone / website via the stable data-item-id attributes.
    rec["address"] = _attr_or_empty(page.locator('button[data-item-id="address"]'), "aria-label").replace("Address: ", "")
    rec["website"] = _attr_or_empty(page.locator('a[data-item-id="authority"]'), "href")
    phone = _attr_or_empty(page.locator('button[data-item-id^="phone"]'), "aria-label")
    rec["phone"] = phone.replace("Phone: ", "") if phone else ""

    # Split city/state/postal out of the address so those columns aren't blank.
    city, state, postal = _parse_address_parts(rec["address"])
    rec["city"], rec["state"], rec["postalCode"] = city, state, postal

    # rating + reviews from the aria-labelled stars block, e.g. "4.3 stars 128 reviews".
    stars = _attr_or_empty(page.locator('span[role="img"][aria-label*="stars"]'), "aria-label")
    m_rating = re.search(r"([\d.]+)\s+stars", stars)
    m_reviews = re.search(r"([\d,]+)\s+review", stars)
    rec["totalScore"] = float(m_rating.group(1)) if m_rating else None
    rec["reviewsCount"] = int(m_reviews.group(1).replace(",", "")) if m_reviews else None

    # coords: prefer the !3d!4d place chunk (in the feed href), fall back to @lat,lng.
    m = _DATA_LATLNG_RE.search(url) or _DATA_LATLNG_RE.search(page.url) or _LATLNG_RE.search(page.url)
    if m:
        rec["location"] = {"lat": float(m.group(1)), "lng": float(m.group(2))}

    return rec


def scrape(search_string, max_results, on_status=None, should_stop=None):
    """Yield raw place records for the search string, one at a time.

    on_status(state, message) is an optional callback for progress/status lines.
    should_stop() is polled in the long loops so a Stop (SIGTERM) cancels quickly
    instead of waiting for the whole feed to scroll or a place page to load.
    """
    # Imported lazily so the Apify-only path never requires playwright installed.
    from playwright.sync_api import sync_playwright

    if should_stop is None:
        should_stop = lambda: False  # noqa: E731 - trivial default

    def status(state, message=""):
        if on_status:
            on_status(state, message)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                )
            )
            query = search_string.replace(" ", "+")
            page.goto(SEARCH_URL.format(query=query), wait_until="domcontentloaded", timeout=45000)
            _accept_consent(page)

            try:
                feed = page.locator('div[role="feed"]')
                feed.wait_for(timeout=15000)
            except Exception:
                status("error", "results feed did not load (possible consent wall or block)")
                return

            urls = _collect_place_urls(page, feed, max_results, should_stop, on_status=status)
            if should_stop():
                return
            status("scraping", f"found {len(urls)} places, opening each for details")

            for url in urls:
                if should_stop():
                    break
                try:
                    rec = _parse_place(page, url)
                    if rec.get("title"):
                        yield rec
                except Exception:
                    # skip a place that fails to parse; keep going.
                    continue
                time.sleep(0.4)
        finally:
            browser.close()


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def _find_one(page, name, place):
    """Search Maps for one named firm and return its raw record, or None.

    A specific name usually lands straight on the place panel. When Google
    answers with a list instead, it is unsure, and the list is mostly other
    firms sharing a first word — so only an exact name match is taken from it.
    """
    from normalize import same_firm

    page.goto(SEARCH_URL.format(query=f"{name} {place}".replace(" ", "+")),
              wait_until="domcontentloaded", timeout=30000)
    _accept_consent(page)
    try:
        page.wait_for_selector('div[role="feed"], button[data-item-id="address"]',
                               timeout=10000)
    except Exception:
        return None
    page.wait_for_timeout(800)

    if page.locator('div[role="feed"]').count() == 0:
        rec = _read_place(page, page.url)
        return rec if same_firm(name, rec.get("title"), place) else None

    cards = page.locator('div[role="feed"] a[href*="/maps/place/"]')
    for i in range(min(cards.count(), 6)):
        label = _attr_or_empty(cards.nth(i), "aria-label")
        if same_firm(name, label, place, strict=True):
            href = _attr_or_empty(cards.nth(i), "href")
            return _parse_place(page, href) if href else None
    return None


def find_places(targets, workers=3, should_stop=None):
    """Look up named firms on Maps, yielding (key, raw record or None).

    `targets` is a list of (key, name, place). Each worker thread runs its own
    Playwright instance, because the sync API is bound to the thread that
    started it. Only the browsing happens off-thread: results come back through
    a queue so the caller can keep its SQLite writes on one connection.
    """
    import queue
    import threading

    from playwright.sync_api import sync_playwright

    if should_stop is None:
        should_stop = lambda: False  # noqa: E731 - trivial default

    todo = queue.Queue()
    for t in targets:
        todo.put(t)
    out = queue.Queue()
    _DONE = object()

    def work():
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=_USER_AGENT)
                    while not should_stop():
                        try:
                            key, name, place = todo.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            out.put((key, _find_one(page, name, place)))
                        except Exception:
                            # One slow or odd result must not end the lookup.
                            out.put((key, None))
                finally:
                    browser.close()
        except Exception:
            pass
        finally:
            out.put(_DONE)

    threads = [threading.Thread(target=work, daemon=True)
               for _ in range(max(1, min(workers, len(targets))))]
    for t in threads:
        t.start()
    running = len(threads)
    while running:
        item = out.get()
        if item is _DONE:
            running -= 1
        else:
            yield item
