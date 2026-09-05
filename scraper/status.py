#!/usr/bin/env python3
"""Print registry status as one JSON line, for the desktop app to read.

The Electron main process has no SQLite driver and shouldn't grow a native
dependency just to show a status chip, so it shells out to this instead.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import LeadStore  # noqa: E402


def main():
    try:
        store = LeadStore(sys.argv[1] if len(sys.argv) > 1 else None)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 0
    try:
        districts = store.rera_districts()
        total_companies = store.conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        print(json.dumps({
            "ok": True,
            "reraProjects": store.rera_count(),
            "reraDistricts": [{"name": d, "count": n} for d, n in districts[:40]],
            "reraStates": [{"name": st, "count": n} for st, n in store.rera_states()],
            "lastHarvest": store.get_meta("rera_last_harvest", "") or "",
            "companies": total_companies,
        }))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
