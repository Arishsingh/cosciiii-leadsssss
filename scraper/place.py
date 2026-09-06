"""Geographic gate: keep only leads that are really in the place searched.

The registry is trustworthy about location — a MahaRERA row carries a real
district — but Google Maps is not. Asked for "builders in Marine Drive,
Mumbai" it answers with whatever it considers relevant, which routinely
includes Thane and Pune and occasionally another state entirely. Those
records are dropped here rather than filtered downstream, so a run only ever
contains the district the user typed.
"""

import re

# Old and new names for the same city. A registry row and a Maps card often
# disagree on which one they use, and they are the same place either way.
_ALIASES = {
    "MUMBAI": ("BOMBAY",),
    "BOMBAY": ("MUMBAI",),
    "BENGALURU": ("BANGALORE",),
    "BANGALORE": ("BENGALURU",),
    "GURUGRAM": ("GURGAON",),
    "GURGAON": ("GURUGRAM",),
    "KOLKATA": ("CALCUTTA",),
    "CALCUTTA": ("KOLKATA",),
    "CHENNAI": ("MADRAS",),
    "MADRAS": ("CHENNAI",),
    "VADODARA": ("BARODA",),
    "BARODA": ("VADODARA",),
    "PUNE": ("POONA",),
    "POONA": ("PUNE",),
}

# Distinct districts whose names contain another district's name. Without
# these, a plain substring/word match on "Mumbai" swallows every Navi Mumbai
# listing — a different district (Thane) on the other side of the creek.
_CONFUSABLE = {
    "MUMBAI": ("NAVI MUMBAI",),
    "BOMBAY": ("NAVI MUMBAI",),
    "DELHI": ("NEW DELHI",),
}

_PIN_RE = re.compile(r"\b\d{6}\b")

# Fields that can say where a record is. `full_address` is last and broadest;
# a Maps card frequently fills nothing else.
_PLACE_FIELDS = ("city", "district", "state", "full_address", "postal_code")


def _norm(value):
    """Uppercase, punctuation-free, single-spaced — comparable place text."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(value or "").upper())).strip()


class PlaceFilter:
    """Decides whether a record belongs to the searched district.

    `area` narrows further only when it is a pincode. A neighbourhood name is
    deliberately not enforced: addresses record "Nariman Point" or bare
    "Mumbai 400020" for the same Marine Drive office, so requiring the typed
    words would throw away real builders. The area still narrows the Maps
    query itself, which is where it does useful work.
    """

    def __init__(self, location, area=""):
        self.location = _norm(location)
        self.area = (area or "").strip()
        self.pincode = self.area if re.fullmatch(r"\d{6}", self.area) else ""
        wanted = [self.location] + list(_ALIASES.get(self.location, ()))
        self._wanted = [w for w in wanted if w]
        self._patterns = [re.compile(r"\b" + re.escape(w) + r"\b") for w in self._wanted]
        self._blocked = [
            re.compile(r"\b" + re.escape(c) + r"\b")
            for w in self._wanted for c in _CONFUSABLE.get(w, ())
        ]

    def allows(self, rec):
        """True when this record's own text places it in the searched district."""
        if not self._wanted:
            return True  # no location given — nothing to enforce
        haystack = _norm(" ".join(str(rec.get(f) or "") for f in _PLACE_FIELDS))
        if not haystack:
            # No location evidence at all. The registry has already vouched for
            # a record carrying a registration number; a bare Maps card has not.
            return bool((rec.get("rera_number") or "").strip())
        if self.pincode and self.pincode not in _PIN_RE.findall(haystack):
            return False
        # A confusable name is checked first: "Navi Mumbai, Maharashtra" also
        # contains "Mumbai", so the block has to win over the plain match.
        stripped = haystack
        for blocked in self._blocked:
            stripped = blocked.sub(" ", stripped)
        return any(p.search(stripped) for p in self._patterns)
