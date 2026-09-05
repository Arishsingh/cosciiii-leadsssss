"""NDJSON event emission and incremental CSV writing.

The scraper talks to the Electron app over stdout as newline-delimited JSON.
It also writes each lead to a timestamped CSV as it is found, so a cancelled or
crashed run still leaves a valid partial file.
"""

import csv
import json
import sys

from mapping import COLUMNS


class Emitter:
    """Emits events either as NDJSON (for Electron) or a human log (for the CLI)."""

    def __init__(self, json_mode):
        self.json_mode = json_mode

    def _emit(self, obj):
        if self.json_mode:
            sys.stdout.write(json.dumps(obj) + "\n")
        else:
            self._human(obj)
        sys.stdout.flush()

    def _human(self, obj):
        t = obj.get("type")
        if t == "status":
            sys.stdout.write(f"[{obj.get('source', '-')}] {obj.get('state')}: {obj.get('message', '')}\n")
        elif t == "progress":
            sys.stdout.write(f"  ...found {obj.get('found')}\n")
        elif t == "lead":
            d = obj["data"]
            sys.stdout.write(f"  + {d.get('name', '')}  |  {d.get('phone', '')}  |  {d.get('website', '') or 'NO WEBSITE'}\n")
        elif t == "stats":
            d = obj["data"]
            sys.stdout.write(
                f"  {d['total']} builders | {d['with_email']} with email "
                f"({d['email_rate']}%) | {d['verified_email']} verified | "
                f"{d['with_phone']} with phone | {d['with_rera']} RERA-registered\n")
        elif t == "done":
            sys.stdout.write(f"Done. {obj.get('total')} leads -> {obj.get('csv_path')}\n")
        elif t == "error":
            sys.stdout.write(f"ERROR: {obj.get('message')}\n")

    def status(self, source, state, message=""):
        self._emit({"type": "status", "source": source, "state": state, "message": message})

    def progress(self, found):
        self._emit({"type": "progress", "found": found})

    def lead(self, data):
        self._emit({"type": "lead", "data": data})

    def stats(self, data):
        """Fill-rate summary emitted once, just before `done`."""
        self._emit({"type": "stats", "data": data})

    def done(self, total, csv_path):
        self._emit({"type": "done", "total": total, "csv_path": csv_path})

    def error(self, message):
        self._emit({"type": "error", "message": message})


class CsvWriter:
    """Writes leads to a CSV incrementally, header first, flushing each row."""

    def __init__(self, path):
        self.path = path
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=COLUMNS)
        self._writer.writeheader()
        self._fh.flush()

    def write(self, lead):
        # Restrict to canonical columns so stray keys never break the row.
        self._writer.writerow({c: lead.get(c, "") for c in COLUMNS})
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
