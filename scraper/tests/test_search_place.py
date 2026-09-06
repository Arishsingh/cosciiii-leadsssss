"""End-to-end check that a search only returns the district that was typed.

Runs the real `run_search` against a throwaway database with the Maps source
stubbed out, so the whole pipeline — publish, broker filter, ranking, CSV —
is exercised without touching the network.

Run: python3 scraper/tests/test_search_place.py
"""

import csv
import glob
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import scraper  # noqa: E402


def place(title, address, city, state, phone="022 2269 1166"):
    """A raw Apify place record, in the shape map_record expects."""
    return {"title": title, "address": address, "city": city, "state": state,
            "phone": phone, "categoryName": "Real estate developer",
            "url": "https://maps.google.com/?cid=" + title.replace(" ", "")}


MAPS = [
    place("Lodha Group", "Marine Drive, Mumbai", "Mumbai", "Maharashtra"),
    place("Oberoi Realty", "Andheri East, Mumbai 400069", "Mumbai", "Maharashtra"),
    place("Rustomjee Builders", "Bandra West, Mumbai Suburban", "Mumbai Suburban", "Maharashtra"),
    # Everything below is somewhere else and must not survive.
    place("Kolte Patil Developers", "Baner, Pune 411045", "Pune", "Maharashtra"),
    place("Hiranandani Estate", "Ghodbunder Rd, Thane 400607", "Thane", "Maharashtra"),
    place("Paradise Group", "Vashi, Navi Mumbai 400703", "Navi Mumbai", "Maharashtra"),
    place("Prestige Constructions", "MG Road, Bengaluru 560001", "Bengaluru", "Karnataka"),
    place("DLF Builders", "Sector 42, Gurugram 122002", "Gurugram", "Haryana"),
]


class Collector:
    """Stands in for Emitter, keeping every event for assertions."""

    def __init__(self):
        self.leads, self.statuses, self.errors = [], [], []
        self.summary, self.total = None, None

    def status(self, source, state, message=""):
        self.statuses.append(message)

    def progress(self, found):
        pass

    def lead(self, data):
        self.leads.append(data)

    def stats(self, data):
        self.summary = data

    def done(self, total, csv_path):
        self.total, self.csv_path = total, csv_path

    def error(self, message):
        self.errors.append(message)


class SearchStaysInDistrict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._fetch, self._token = scraper.apify.fetch, scraper.get_apify_token
        scraper.apify.fetch = lambda queries, limit, token: list(MAPS)
        scraper.get_apify_token = lambda: "test-token"

    def tearDown(self):
        scraper.apify.fetch, scraper.get_apify_token = self._fetch, self._token

    def run_search(self, location, area="", max_results=50):
        args = types.SimpleNamespace(
            location=location, area=area, max_results=max_results,
            db=os.path.join(self.tmp, "leads.db"), output_dir=os.path.join(self.tmp, "out"),
            source="apify", no_rera=True, no_enrich=True, no_verify=True,
            enrich_workers=2, keep_brokers=False,
            only_with_email=False, only_verified_email=False,
        )
        emitter = Collector()
        self.assertEqual(scraper.run_search(args, emitter), 0)
        self.assertEqual(emitter.errors, [])
        return emitter

    def names(self, emitter):
        return {l["name"] for l in emitter.leads}

    def test_only_the_searched_district_is_streamed(self):
        e = self.run_search("mumbai")
        self.assertEqual(self.names(e),
                         {"Lodha Group", "Oberoi Realty", "Rustomjee Builders"})

    def test_neighbouring_and_other_state_places_are_reported_as_dropped(self):
        e = self.run_search("mumbai")
        self.assertTrue(any("outside mumbai" in s for s in e.statuses), e.statuses)

    def test_the_csv_matches_what_was_streamed(self):
        e = self.run_search("mumbai")
        path = glob.glob(os.path.join(self.tmp, "out", "*.csv"))[0]
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual({r["name"] for r in rows}, self.names(e))
        self.assertEqual(e.total, len(rows))
        self.assertEqual(e.summary["total"], len(rows))

    def test_the_exported_phone_has_no_trunk_zero(self):
        e = self.run_search("mumbai")
        self.assertTrue(all(l["phone"] == "22 2269 1166" for l in e.leads))

    def test_a_different_district_returns_its_own_places(self):
        self.assertEqual(self.names(self.run_search("pune")), {"Kolte Patil Developers"})
        self.assertEqual(self.names(self.run_search("thane")), {"Hiranandani Estate"})
        self.assertEqual(self.names(self.run_search("navi mumbai")), {"Paradise Group"})

    def test_a_search_in_another_state_never_returns_maharashtra(self):
        self.assertEqual(self.names(self.run_search("bengaluru")), {"Prestige Constructions"})
        self.assertEqual(self.names(self.run_search("gurgaon")), {"DLF Builders"})

    def test_a_pincode_area_pins_the_result_to_that_pincode(self):
        self.assertEqual(self.names(self.run_search("mumbai", "400069")), {"Oberoi Realty"})

    def test_a_named_area_still_returns_the_district(self):
        # The neighbourhood narrows the Maps query, not the district gate.
        self.assertEqual(len(self.run_search("mumbai", "marine drive").leads), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
