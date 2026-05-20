#!/usr/bin/env python3
"""
@file clear_signals.py
@brief Clear or trim the SQLite signal detection database.

Usage:
    ./clear_signals.sh                      # clear all records (keep file)
    ./clear_signals.sh --all                # delete the DB file entirely
    ./clear_signals.sh --older-than 24      # delete records older than N hours
    ./clear_signals.sh --yes                # skip confirmation prompt
    ./clear_signals.sh --db /path/to.db     # custom DB
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_DB  = SCRIPT_DIR / "signals.db"


def main() -> None:
    """@brief Entry point: parse arguments and clear/trim the database with user confirmation."""
    ap = argparse.ArgumentParser(description="Clear the SDR signal database")
    ap.add_argument("--db",          default=str(DEFAULT_DB))
    ap.add_argument("--all",         action="store_true",
                    help="Delete the database file entirely")
    ap.add_argument("--older-than",  type=float, metavar="HOURS",
                    dest="older_hours",
                    help="Delete records older than N hours")
    ap.add_argument("--yes", "-y",   action="store_true",
                    help="Skip confirmation prompt")
    args = ap.parse_args()

    db_path = Path(args.db)

    if not db_path.exists():
        print(f"No database at {db_path}")
        return

    db    = sqlite3.connect(str(db_path))
    count = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    print(f"Database : {db_path}")
    print(f"Records  : {count}")

    if args.all:
        action = f"DELETE THE FILE {db_path}"
    elif args.older_hours is not None:
        cutoff_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00",
            time.gmtime(time.time() - args.older_hours * 3600))
        old = db.execute(
            "SELECT COUNT(*) FROM signals WHERE last_seen < ?", (cutoff_iso,)
        ).fetchone()[0]
        action = f"delete {old} records older than {args.older_hours:.0f}h"
    else:
        action = f"delete ALL {count} records (keep file)"

    print(f"Action   : {action}")

    if not args.yes:
        ans = input("Proceed? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            db.close()
            return

    if args.all:
        db.close()
        db_path.unlink()
        print(f"Deleted {db_path}")
    elif args.older_hours is not None:
        db.execute("DELETE FROM signals WHERE last_seen < ?", (cutoff_iso,))
        db.commit()
        db.execute("VACUUM")
        remaining = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        db.close()
        print(f"Deleted {old} records — {remaining} remain")
    else:
        db.execute("DELETE FROM signals")
        db.commit()
        db.execute("VACUUM")
        db.close()
        print(f"Cleared — {count} records deleted")


if __name__ == "__main__":
    main()
