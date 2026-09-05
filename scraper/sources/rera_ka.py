"""Karnataka RERA (K-RERA) project registry.

Unlike MahaRERA, which paginates ten cards at a time, K-RERA's
`/viewAllProjects` ships the **entire registry in one ~6 MB page** — no CAPTCHA,
no token, no pagination. Roughly 9,900 projects and 6,700 distinct promoters in
a single request, which makes this by far the cheapest state to harvest.

The data is not in a table. The page emits repeated JavaScript `push()` calls
into four parallel arrays, and the row is the tuple at each index:

    applicationNameList  .push('ACK/KA/...')   acknowledgement number
    applicationNameList2 .push('PRM/KA/...')   registration number
    applicationNameList3 .push('AARADHYA GARDENS')        project
    applicationNameList4 .push('SAI SAMRUDHI CONSTRUCTIONS')  promoter

**No district is published in this listing**, and the subdistrict dropdown is
filled in by a later AJAX call. The registration number does embed office codes
(`PRM/KA/RERA/1251/310/...`) but there is no public mapping from those to
district names, so guessing one would invent data. Rows are therefore stored
with the state set and the district left empty, and location matching falls
back to the state. Per-project districts would require one detail POST per
project (~9,900 requests); that is a future enrichment, not a harvest.
"""

import html
import re

import requests

URL = "https://rera.karnataka.gov.in/viewAllProjects"
STATE = "KARNATAKA"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class KReraError(RuntimeError):
    pass


def _push_list(page, suffix):
    """Every value pushed into applicationNameList<suffix>, in page order."""
    pat = r"applicationNameList%s\s*\.push\('((?:[^'\\]|\\.)*)'\)" % suffix
    return [
        html.unescape(v).replace("\\'", "'").replace("\\\\", "\\").strip()
        for v in re.findall(pat, page)
    ]


def parse(page):
    """Parse the whole-registry page into project dicts. Pure — testable offline."""
    reg = _push_list(page, "2")
    project = _push_list(page, "3")
    promoter = _push_list(page, "4")
    # The four arrays are written in lockstep; if a page ever emits a partial
    # group, truncate to the shortest rather than pairing rows that don't belong
    # together.
    n = min(len(reg), len(project), len(promoter))
    out = []
    for i in range(n):
        if not reg[i] or not promoter[i]:
            continue
        out.append({
            "rera_number": reg[i],
            "project_name": project[i],
            "promoter_name": promoter[i],
            "state": STATE,
            "district": "",
            "division": "",
            "locality": "",
            "pincode": "",
            "latitude": "",
            "longitude": "",
            "last_modified": "",
            "project_id": "",
        })
    return out


def harvest(on_page=None, should_stop=None, timeout=180, **_ignored):
    """Yield every K-RERA project. One request; `on_page` fires once.

    Signature mirrors `rera.harvest` so the orchestrator can treat states
    uniformly, but the pagination arguments are meaningless here and ignored.
    """
    if should_stop and should_stop():
        return
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        r = session.get(URL, timeout=timeout)
    except requests.RequestException as e:
        raise KReraError(f"network error: {e}") from e
    if r.status_code != 200:
        raise KReraError(f"HTTP {r.status_code}")

    rows = parse(r.text)
    if not rows:
        raise KReraError("no projects parsed — page layout changed")
    for row in rows:
        if should_stop and should_stop():
            return
        yield row
    if on_page:
        on_page(1, len(rows))
