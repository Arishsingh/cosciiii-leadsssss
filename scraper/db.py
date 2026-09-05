"""SQLite store and the cross-source dedupe engine.

Why this exists: the original scraper kept an in-memory `seen` set that died
with the process, so a second run re-emitted every lead and no two sources
could ever be merged. Persisting to SQLite makes dedupe survive runs, lets the
MahaRERA registry be harvested once and reused, and turns a search into a local
query instead of a network crawl.

The merge rule is a **cascade of decreasing confidence** (see `find_match`):
an exact RERA number is proof; a shared phone is near-proof; a shared domain is
strong; a fuzzy name is only trusted when the geography already agrees.
"""

import json
import os
import sqlite3
import time

from normalize import (
    name_key,
    name_similarity,
    norm_city,
    norm_domain,
    norm_phone,
    norm_pincode,
)

# Above this, two names in the same pincode/district are treated as one firm.
# Tuned on real pairs: "Kumar Builders Pvt Ltd"/"Kumar Developers" scores 1.0
# after normalization, while "Kumar Builders"/"Rohan Builders" scores 0.2.
FUZZY_THRESHOLD = 0.88

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    name_key        TEXT,
    category        TEXT,
    full_address    TEXT,
    city            TEXT,
    district        TEXT,
    state           TEXT,
    postal_code     TEXT,
    phone           TEXT,
    phone_norm      TEXT,
    website         TEXT,
    domain          TEXT,
    email           TEXT,
    email_confidence TEXT,
    rating          REAL,
    reviews_count   INTEGER,
    opening_hours   TEXT,
    latitude        TEXT,
    longitude       TEXT,
    google_maps_url TEXT,
    rera_number     TEXT,
    promoter_name   TEXT,
    promoter_type   TEXT,
    active_projects INTEGER DEFAULT 0,
    lead_score      INTEGER DEFAULT 0,
    sources         TEXT,
    linkedin        TEXT,
    first_seen      TEXT,
    last_seen       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_rera  ON companies(rera_number) WHERE rera_number IS NOT NULL AND rera_number <> '';
CREATE INDEX IF NOT EXISTS idx_companies_phone  ON companies(phone_norm);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(domain);
CREATE INDEX IF NOT EXISTS idx_companies_namekey ON companies(name_key);

CREATE TABLE IF NOT EXISTS company_sources (
    company_id  INTEGER NOT NULL,
    source      TEXT NOT NULL,
    source_ref  TEXT,
    scraped_at  TEXT,
    PRIMARY KEY (company_id, source, source_ref)
);

-- The MahaRERA registry, harvested in bulk and reused across searches.
-- One row per registered *project*; promoters are aggregated from it.
CREATE TABLE IF NOT EXISTS rera_projects (
    rera_number   TEXT PRIMARY KEY,
    project_name  TEXT,
    promoter_name TEXT,
    name_key      TEXT,
    locality      TEXT,
    district      TEXT,
    division      TEXT,
    state         TEXT,
    pincode       TEXT,
    latitude      TEXT,
    longitude     TEXT,
    last_modified TEXT,
    project_id    TEXT,
    harvested_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_rera_namekey  ON rera_projects(name_key);
CREATE INDEX IF NOT EXISTS idx_rera_district ON rera_projects(district);

-- Bookkeeping so the UI can show how fresh each state's registry is.
CREATE TABLE IF NOT EXISTS harvest_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

# Columns that carry a value worth merging. Excludes identity/bookkeeping.
_MERGE_FIELDS = [
    "name", "category", "full_address", "city", "district", "state",
    "postal_code", "phone", "website", "email", "rating", "reviews_count",
    "opening_hours", "latitude", "longitude", "google_maps_url",
    "rera_number", "promoter_name", "promoter_type", "linkedin",
]

# Ranked worst→best so a stronger email never gets overwritten by a weaker one.
EMAIL_CONFIDENCE_RANK = {"": 0, "guess": 1, "generic": 2, "site-crawl": 3, "verified": 4}


def default_db_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "leads.db")


class LeadStore:
    """Persistent company store with cascading cross-source dedupe."""

    def __init__(self, path=None):
        self.path = path or default_db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # A harvest can run for the best part of an hour while the app reads the
        # same file, so the connection has to tolerate contention rather than
        # fail on it. WAL lets readers proceed during a write; busy_timeout makes
        # a writer wait its turn instead of raising "database is locked"
        # immediately (SQLite's default is to give up at once).
        self.conn = sqlite3.connect(self.path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # Order matters: `journal_mode=WAL` itself needs a brief exclusive lock,
        # so busy_timeout has to be set FIRST or that pragma is the very thing
        # that raises "database is locked" when another process holds the file.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add columns introduced after a database was first created.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so new
        fields have to be ALTERed in or every older database breaks on the next
        read. Adding a column is cheap and idempotent here because the check is
        against the live table shape rather than a version number.
        """
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(companies)")}
        for col, decl in (("linkedin", "TEXT"),):
            if col in have:
                continue
            try:
                self.conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                # ALTER needs an exclusive lock, so it loses to a long-running
                # writer (a harvest, or a search mid-sweep). Opening the store
                # must not fail just because a migration could not run right
                # now: readers degrade gracefully on a missing column, and the
                # next open with a free database will add it.
                pass

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except sqlite3.Error:
            pass

    # ---------------------------------------------------------------- dedupe

    def find_match(self, rec):
        """Return the existing company row that `rec` refers to, or None.

        Tried in order of how much the evidence proves, stopping at the first
        hit. Each rung is deliberately narrower than a plain name comparison,
        which is what makes merging Maps data into RERA data safe.
        """
        cur = self.conn.cursor()

        # 1. RERA registration number — a state-issued identifier, so exact.
        rera = (rec.get("rera_number") or "").strip()
        if rera:
            row = cur.execute(
                "SELECT * FROM companies WHERE rera_number = ?", (rera,)
            ).fetchone()
            if row:
                return row

        # 2. Phone. Two businesses genuinely sharing a landline is rare enough
        #    to accept, and it is the single most reliable field Maps gives us.
        phone = norm_phone(rec.get("phone"))
        if phone:
            row = cur.execute(
                "SELECT * FROM companies WHERE phone_norm = ?", (phone,)
            ).fetchone()
            if row:
                return row

        # 3. Website domain. Guard against shared hosts (a builder listing a
        #    portal like 99acres as its "website" would merge everyone).
        domain = norm_domain(rec.get("website"))
        if domain and domain not in _SHARED_HOSTS:
            row = cur.execute(
                "SELECT * FROM companies WHERE domain = ?", (domain,)
            ).fetchone()
            if row:
                return row

        # 4. Fuzzy name, but only among companies already in the same place.
        #    Without the geography gate this rung produces false merges; with
        #    it, "Kumar Builders" in Pune stays distinct from one in Nagpur.
        key = name_key(rec.get("name"))
        if not key:
            return None
        pin = norm_pincode(rec.get("postal_code"))
        city = norm_city(rec.get("city") or rec.get("district"))
        if not pin and not city:
            return None

        # Exact normalized-name hits first, then near hits, both geo-gated.
        candidates = cur.execute(
            "SELECT * FROM companies WHERE name_key = ?", (key,)
        ).fetchall()
        if not candidates:
            # Widen only to names sharing a first token — keeps the scan small.
            first = key.split(" ")[0]
            candidates = cur.execute(
                "SELECT * FROM companies WHERE name_key LIKE ? LIMIT 400",
                (f"%{first}%",),
            ).fetchall()

        for row in candidates:
            if not _same_place(row, pin, city):
                continue
            if name_similarity(rec.get("name"), row["name"]) >= FUZZY_THRESHOLD:
                return row
        return None

    # ----------------------------------------------------------------- write

    def upsert(self, rec, source, source_ref=""):
        """Insert `rec`, or merge it into the company it matches.

        Returns (company_id, is_new). Merging never destroys a value: a blank
        incoming field leaves the stored one alone, and email only moves up the
        confidence ladder.
        """
        now = _now()
        match = self.find_match(rec)
        cur = self.conn.cursor()

        if match is None:
            data = {f: _clean(rec.get(f)) for f in _MERGE_FIELDS}
            data["name_key"] = name_key(rec.get("name"))
            data["phone_norm"] = norm_phone(rec.get("phone"))
            data["domain"] = norm_domain(rec.get("website"))
            data["email_confidence"] = rec.get("email_confidence") or (
                "site-crawl" if data.get("email") else ""
            )
            data["active_projects"] = int(rec.get("active_projects") or 0)
            data["sources"] = source
            data["first_seen"] = now
            data["last_seen"] = now
            cols = ", ".join(data)
            marks = ", ".join("?" for _ in data)
            cur.execute(
                f"INSERT INTO companies ({cols}) VALUES ({marks})", list(data.values())
            )
            cid = cur.lastrowid
            is_new = True
        else:
            cid = match["id"]
            updates = {}
            for f in _MERGE_FIELDS:
                incoming = _clean(rec.get(f))
                if incoming in ("", None):
                    continue
                if f == "email":
                    continue  # handled below, by confidence
                if not _clean(match[f]):
                    updates[f] = incoming
            # Email is the money field: only replace it with a better-evidenced
            # one, never merely a newer one.
            inc_email = _clean(rec.get("email"))
            if inc_email:
                inc_conf = rec.get("email_confidence") or "site-crawl"
                cur_conf = match["email_confidence"] or ""
                if EMAIL_CONFIDENCE_RANK.get(inc_conf, 0) > EMAIL_CONFIDENCE_RANK.get(cur_conf, 0):
                    updates["email"] = inc_email
                    updates["email_confidence"] = inc_conf
                elif not _clean(match["email"]):
                    updates["email"] = inc_email
                    updates["email_confidence"] = inc_conf
            # Project count is authoritative from RERA, so take the larger.
            inc_proj = int(rec.get("active_projects") or 0)
            if inc_proj > (match["active_projects"] or 0):
                updates["active_projects"] = inc_proj
            # Keep the derived keys in step with whatever we just accepted.
            if "name" in updates:
                updates["name_key"] = name_key(updates["name"])
            if "phone" in updates:
                updates["phone_norm"] = norm_phone(updates["phone"])
            if "website" in updates:
                updates["domain"] = norm_domain(updates["website"])
            srcs = set((match["sources"] or "").split(",")) - {""}
            srcs.add(source)
            updates["sources"] = ",".join(sorted(srcs))
            updates["last_seen"] = now

            sets = ", ".join(f"{k} = ?" for k in updates)
            cur.execute(
                f"UPDATE companies SET {sets} WHERE id = ?", list(updates.values()) + [cid]
            )
            is_new = False

        cur.execute(
            "INSERT OR REPLACE INTO company_sources (company_id, source, source_ref, scraped_at)"
            " VALUES (?, ?, ?, ?)",
            (cid, source, source_ref or "", now),
        )
        return cid, is_new

    def get(self, cid):
        return self.conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()

    def set_score(self, cid, score):
        self.conn.execute("UPDATE companies SET lead_score = ? WHERE id = ?", (score, cid))

    def commit(self):
        self.conn.commit()

    # ------------------------------------------------------------ rera cache

    def upsert_rera_project(self, p):
        """Store one MahaRERA project row (idempotent on registration number)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO rera_projects (rera_number, project_name, promoter_name,"
            " name_key, locality, district, division, state, pincode, latitude, longitude,"
            " last_modified, project_id, harvested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                p.get("rera_number", ""), p.get("project_name", ""), p.get("promoter_name", ""),
                name_key(p.get("promoter_name")), p.get("locality", ""), p.get("district", ""),
                p.get("division", ""), p.get("state", ""), p.get("pincode", ""),
                p.get("latitude", ""), p.get("longitude", ""), p.get("last_modified", ""),
                p.get("project_id", ""), _now(),
            ),
        )

    def rera_promoters(self, district=None, limit=None):
        """Promoters aggregated from the cached projects, busiest first.

        One row per promoter with their project count — the count is the single
        best available proxy for company size, and the ordering means a capped
        search returns the most substantial builders rather than an arbitrary
        slice.
        """
        sql = (
            "SELECT promoter_name, COUNT(*) AS active_projects,"
            " MAX(district) AS district, MAX(division) AS division, MAX(state) AS state,"
            " MAX(pincode) AS pincode, MAX(locality) AS locality,"
            " MAX(latitude) AS latitude, MAX(longitude) AS longitude,"
            " MIN(rera_number) AS rera_number, MAX(last_modified) AS last_modified"
            " FROM rera_projects WHERE promoter_name <> ''"
        )
        args = []
        if district:
            # State is included because some registries (Karnataka) publish no
            # district at all, so "Karnataka" or "Bengaluru" must still resolve.
            sql += (" AND (UPPER(district) LIKE ? OR UPPER(division) LIKE ?"
                    " OR UPPER(locality) LIKE ? OR UPPER(state) LIKE ?)")
            like = f"%{district.upper()}%"
            args += [like, like, like, like]
        sql += " GROUP BY name_key ORDER BY active_projects DESC, promoter_name"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql, args).fetchall()

        # A city in a registry that publishes no district finds nothing above.
        # Fall back to that city's state rather than returning an empty list,
        # since "Bengaluru" is what a user actually types.
        if not rows and district:
            state = _CITY_TO_STATE.get(district.strip().upper())
            if state:
                return self.rera_promoters(district=state, limit=limit)
        return rows

    def rera_states(self):
        rows = self.conn.execute(
            "SELECT state, COUNT(*) n FROM rera_projects WHERE state <> ''"
            " GROUP BY state ORDER BY n DESC"
        ).fetchall()
        return [(r["state"], r["n"]) for r in rows]

    def rera_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM rera_projects").fetchone()[0]

    def rera_districts(self):
        rows = self.conn.execute(
            "SELECT district, COUNT(*) n FROM rera_projects WHERE district <> ''"
            " GROUP BY district ORDER BY n DESC"
        ).fetchall()
        return [(r["district"], r["n"]) for r in rows]

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO harvest_meta (key, value, updated_at) VALUES (?,?,?)",
            (key, str(value), _now()),
        )
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM harvest_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


# Portals and aggregators that many different builders list as "their" site.
# Matching on these would collapse unrelated companies into one row.
_SHARED_HOSTS = {
    "99acres.com", "magicbricks.com", "housing.com", "commonfloor.com",
    "indiamart.com", "justdial.com", "sulekha.com", "facebook.com",
    "instagram.com", "linkedin.com", "google.com", "sites.google.com",
    "wixsite.com", "blogspot.com", "wordpress.com", "business.site",
}


# Cities whose registry publishes no district, so a city search has to resolve
# to the state. Only needed for registries like K-RERA that omit district
# entirely — Maharashtra rows carry a real district and never reach this.
_CITY_TO_STATE = {
    "BENGALURU": "KARNATAKA", "BANGALORE": "KARNATAKA", "BLR": "KARNATAKA",
    "MYSURU": "KARNATAKA", "MYSORE": "KARNATAKA", "MANGALURU": "KARNATAKA",
    "MANGALORE": "KARNATAKA", "HUBLI": "KARNATAKA", "HUBBALLI": "KARNATAKA",
    "BELAGAVI": "KARNATAKA", "BELGAUM": "KARNATAKA", "DHARWAD": "KARNATAKA",
    "TUMAKURU": "KARNATAKA", "SHIVAMOGGA": "KARNATAKA",
}


def _same_place(row, pin, city):
    """True when a stored row is geographically consistent with the incoming one."""
    row_pin = norm_pincode(row["postal_code"])
    if pin and row_pin:
        return pin == row_pin
    row_city = norm_city(row["city"] or row["district"])
    if city and row_city:
        # Substring either way: "PUNE" should match "PUNE (RURAL)".
        return city in row_city or row_city in city
    return False


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return v


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
