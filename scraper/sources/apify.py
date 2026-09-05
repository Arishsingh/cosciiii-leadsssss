"""Primary source: the Apify actor compass/crawler-google-places.

Mirrors the n8n 'Apify - Google Places Scraper' node: a synchronous call to
run-sync-get-dataset-items with the same body and auth, returning one raw record
per business.
"""

import requests

ACTOR_URL = (
    "https://api.apify.com/v2/acts/"
    "compass~crawler-google-places/run-sync-get-dataset-items"
)
TIMEOUT_SECONDS = 300  # same 300000ms as the n8n node


class ApifyError(Exception):
    pass


def fetch(search_string, max_results, token):
    """Run the actor synchronously and return a list of raw place records.

    `search_string` may be a single query or a list of them. Several phrasings
    are needed for a vertical like real estate, where one firm is listed as a
    "builder", another as a "property developer" and a third as a "construction
    company" — a single query silently misses most of the market.

    Raises ApifyError on any transport/HTTP failure so the orchestrator can fall
    back to the browser scraper.
    """
    queries = [search_string] if isinstance(search_string, str) else list(search_string)
    # The actor caps per *search*, so divide the budget across the phrasings
    # rather than multiplying the bill by the number of queries.
    per_search = max(1, -(-max_results // max(1, len(queries))))
    body = {
        "searchStringsArray": queries,
        "maxCrawledPlacesPerSearch": per_search,
        "language": "en",
        "scrapeContacts": True,
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.post(ACTOR_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise ApifyError(f"request failed: {e}") from e

    if resp.status_code >= 400:
        raise ApifyError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise ApifyError(f"invalid JSON response: {e}") from e

    # run-sync-get-dataset-items returns the array of items directly.
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        data = data["data"]
    if not isinstance(data, list):
        raise ApifyError("unexpected response shape (expected a list of items)")

    return data
