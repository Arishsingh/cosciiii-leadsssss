"""Pure-logic checks for the geographic gate and the phone display form.

Run: python3 scraper/tests/test_place.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from normalize import display_phone, norm_phone  # noqa: E402
from place import PlaceFilter  # noqa: E402


def maps(**kw):
    """A Maps-shaped record: no registration number to vouch for it."""
    rec = {"name": "X Builders", "city": "", "district": "", "state": "",
           "full_address": "", "postal_code": "", "rera_number": ""}
    rec.update(kw)
    return rec


class PlaceGate(unittest.TestCase):
    def setUp(self):
        self.mumbai = PlaceFilter("mumbai", "marine drive")

    def test_keeps_the_searched_city(self):
        self.assertTrue(self.mumbai.allows(maps(city="Mumbai", state="Maharashtra")))
        self.assertTrue(self.mumbai.allows(
            maps(full_address="Nariman Point, Mumbai, Maharashtra 400021")))

    def test_keeps_a_district_spelling_of_the_same_city(self):
        self.assertTrue(self.mumbai.allows(maps(district="Mumbai Suburban")))
        self.assertTrue(self.mumbai.allows(maps(city="Bombay")))

    def test_drops_another_district_in_the_same_state(self):
        self.assertFalse(self.mumbai.allows(maps(city="Pune", state="Maharashtra")))
        self.assertFalse(self.mumbai.allows(maps(district="Thane", state="Maharashtra")))

    def test_drops_another_state(self):
        self.assertFalse(self.mumbai.allows(maps(city="Bengaluru", state="Karnataka")))
        self.assertFalse(self.mumbai.allows(
            maps(full_address="MG Road, Gurugram, Haryana 122002")))

    def test_navi_mumbai_is_not_mumbai(self):
        self.assertFalse(self.mumbai.allows(maps(city="Navi Mumbai", state="Maharashtra")))
        self.assertFalse(self.mumbai.allows(
            maps(full_address="Palm Beach Rd, Navi Mumbai, Maharashtra 400614")))

    def test_navi_mumbai_search_keeps_navi_mumbai(self):
        navi = PlaceFilter("navi mumbai")
        self.assertTrue(navi.allows(maps(city="Navi Mumbai")))
        self.assertFalse(navi.allows(maps(city="Mumbai")))

    def test_substring_is_not_a_match(self):
        # "Punecity"/"Mumbaipur" style noise must not pass as the real name.
        self.assertFalse(PlaceFilter("pune").allows(maps(city="Punegaon")))

    def test_punctuation_and_case_do_not_matter(self):
        self.assertTrue(PlaceFilter("Pune").allows(maps(full_address="Wakad, pune-411057")))

    def test_unlocated_record_needs_registry_backing(self):
        self.assertFalse(self.mumbai.allows(maps()))
        self.assertTrue(self.mumbai.allows(maps(rera_number="P51800000001")))

    def test_pincode_area_is_enforced_exactly(self):
        pin = PlaceFilter("mumbai", "400021")
        self.assertTrue(pin.allows(maps(full_address="Nariman Point, Mumbai 400021")))
        self.assertFalse(pin.allows(maps(full_address="Andheri, Mumbai 400053")))

    def test_named_area_does_not_over_filter(self):
        # Marine Drive addresses are written many ways; the district is the gate.
        self.assertTrue(self.mumbai.allows(
            maps(full_address="Churchgate, Mumbai, Maharashtra 400020")))

    def test_no_location_means_no_gate(self):
        self.assertTrue(PlaceFilter("").allows(maps(city="Anywhere")))

    def test_city_alias_search_term(self):
        self.assertTrue(PlaceFilter("bombay").allows(maps(city="Mumbai")))
        self.assertTrue(PlaceFilter("bangalore").allows(maps(city="Bengaluru")))
        self.assertFalse(PlaceFilter("bombay").allows(maps(city="Navi Mumbai")))


class PhoneDisplay(unittest.TestCase):
    def test_trunk_prefix_is_dropped(self):
        self.assertEqual(display_phone("086513 57287"), "86513 57287")
        self.assertEqual(display_phone("022 2269 1166"), "22 2269 1166")

    def test_other_forms_are_left_alone(self):
        self.assertEqual(display_phone("+91 98765 43210"), "+91 98765 43210")
        self.assertEqual(display_phone("9876543210"), "9876543210")
        self.assertEqual(display_phone(""), "")
        self.assertEqual(display_phone(None), "")

    def test_it_is_idempotent(self):
        # publish() republishes the same record as contacts arrive.
        once = display_phone("086513 57287")
        self.assertEqual(display_phone(once), once)

    def test_dedupe_key_is_unaffected(self):
        self.assertEqual(norm_phone("086513 57287"), norm_phone("86513 57287"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
