#!/usr/bin/env python3
# ========================================================================
# Project: OpenRFStack
# Author:  Brendan Michaud
# Year:    2026
# Part of OpenRFStack (https://github.com/OpenRFStack)
#
# Licensed under the Personal Use License.
# Do not use for commercial, organizational, or military purposes.
# Contact author for permission: https://github.com/OpenRFStack
# ========================================================================

"""
@file read_signals.py
@brief Query the SQLite signal detection database and display results.

Usage:
    ./read_signals.sh                         # all signals, sorted by freq
    ./read_signals.sh --recent 60             # last 60 minutes only
    ./read_signals.sh --sort time             # sort by first-seen time
    ./read_signals.sh --sort power            # sort by signal power
    ./read_signals.sh --sort hits             # sort by detection count
    ./read_signals.sh --classified            # only classified signals
    ./read_signals.sh --band fm               # filter by band
    ./read_signals.sh --freq 433              # filter near freq (MHz ±5)
    ./read_signals.sh --csv                   # output as CSV
    ./read_signals.sh --watch                 # refresh every 10s
    ./read_signals.sh --db /path/to.db        # custom DB
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DEFAULT_DB  = SCRIPT_DIR / "signals.db"

BANDS: dict[str, tuple[float, float]] = {
    "fm":      (87.5, 108.0),
    "air":     (108.0, 137.0),
    "vhf":     (137.0, 230.0),
    "uhf":     (230.0, 470.0),
    "70cm":    (430.0, 440.0),
    "ism433":  (430.0, 435.0),
    "dvbt":    (470.0, 790.0),
    "lte":     (470.0, 870.0),
    "800":     (790.0, 870.0),
    "gsm":     (870.0, 960.0),
    "900":     (870.0, 960.0),
    "ism915":  (902.0, 928.0),
}

SORT_MAP = {
    "freq":      "freq_hz ASC",
    "frequency": "freq_hz ASC",
    "time":      "first_seen ASC",
    "first":     "first_seen ASC",
    "last":      "last_seen DESC",
    "power":     "power_db DESC",
    "snr":       "snr_db DESC",
    "hits":      "hits DESC",
    "count":     "hits DESC",
    "mod":       "modulation ASC, freq_hz ASC",
    "modulation":"modulation ASC, freq_hz ASC",
}


def build_query(args) -> tuple[str, list]:
    """@brief Build the SELECT SQL and parameter list from parsed CLI arguments.
    @param args  argparse.Namespace with filter/sort attributes.
    @return      (sql_string, params_list) ready for sqlite3.execute().
    """
    where_parts = []
    params: list = []

    if args.recent:
        cutoff_iso = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00",
            time.gmtime(time.time() - args.recent * 60))
        where_parts.append("last_seen >= ?")
        params.append(cutoff_iso)

    if args.classified:
        where_parts.append("classified = 1")

    if args.freq is not None:
        lo, hi = args.freq - 5, args.freq + 5
        where_parts.append("freq_mhz BETWEEN ? AND ?")
        params += [lo, hi]

    if args.band:
        band = args.band.lower()
        if band not in BANDS:
            print(f"Unknown band: {band}. Choose from: {', '.join(BANDS)}", file=sys.stderr)
            sys.exit(1)
        lo, hi = BANDS[band]
        where_parts.append("freq_mhz BETWEEN ? AND ?")
        params += [lo, hi]

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    order = SORT_MAP.get(args.sort.lower(), "freq_hz ASC")

    sql = f"""
        SELECT
            freq_mhz,
            ROUND(bandwidth_hz / 1000.0, 1)           AS bw_khz,
            ROUND(snr_db, 1)                           AS snr_db,
            ROUND(power_db, 1)                         AS power_db,
            CASE
                WHEN modulation != '' THEN modulation
                WHEN classified = 0   THEN ''
                ELSE 'unclassified'
            END                                        AS modulation,
            CASE is_ofdm  WHEN 1 THEN 'OFDM '  ELSE '' END ||
            CASE is_burst WHEN 1 THEN 'burst '  ELSE '' END ||
            CASE is_fhss  WHEN 1 THEN 'FHSS'   ELSE '' END AS flags,
            first_seen,
            last_seen,
            hits
        FROM signals
        {where}
        ORDER BY {order}
    """
    return sql, params


def fmt_time(ts: str | None) -> str:
    """@brief Format an ISO 8601 timestamp for table display (strip timezone, append 'Z').
    @param ts  ISO string or None.
    @return    19-character UTC string ending in 'Z', or '—'.
    """
    if not ts:
        return "—"
    return ts.replace("T", " ").replace("+00:00", "Z")[:19] + "Z"


def fmt(v, width: int, default="—") -> str:
    """@brief Left-justify a value to a fixed column width, truncating if necessary.
    @param v       Value to format (converted to str).
    @param width   Target column width in characters.
    @param default String to use when v is None.
    @return        Left-justified string of exactly @p width characters.
    """
    s = str(v) if v is not None else default
    return s[:width].ljust(width)


def print_table(rows: list, db_path: str, args) -> None:
    """@brief Print query results as a formatted ASCII table to stdout.
    @param rows     Rows returned by sqlite3.execute().
    @param db_path  Database path shown in the footer line.
    @param args     Parsed CLI args (used to build the active-filter summary).
    """
    H = ["Freq (MHz)", "BW(kHz)", "SNR(dB)", "Modulation", "Flags", "First seen (UTC)", "Hits"]
    W = [11, 8, 8, 18, 10, 20, 4]

    header = "  " + "  ".join(h.ljust(w) for h, w in zip(H, W))
    sep    = "  " + "─" * (sum(W) + 2 * len(W))
    print()
    print(header)
    print(sep)

    for row in rows:
        freq, bw, snr, pwr, mod, flags, first, last, hits = row
        cols = [
            f"{freq:.3f}" if freq else "—",
            f"{bw:.1f}"   if bw   else "—",
            f"{snr:.1f}"  if snr  else "—",
            mod or "—",
            (flags or "").strip(),
            fmt_time(first),
            str(hits) if hits else "1",
        ]
        print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, W)))

    print()
    filters = []
    if args.recent:      filters.append(f"last {args.recent}min")
    if args.classified:  filters.append("classified only")
    if args.band:        filters.append(f"band={args.band}")
    if args.freq:        filters.append(f"≈{args.freq}MHz±5")

    filt_str = f"  [{', '.join(filters)}]" if filters else ""
    print(f"  {len(rows)} signal(s){filt_str}  ·  sorted by {args.sort}  ·  {db_path}")


def print_csv(rows: list) -> None:
    """@brief Write query results to stdout as RFC 4180 CSV.
    @param rows  Rows returned by sqlite3.execute().
    """
    w = csv.writer(sys.stdout)
    w.writerow(["freq_mhz","bw_khz","snr_db","power_db","modulation",
                "flags","first_seen","last_seen","hits"])
    for row in rows:
        w.writerow([
            f"{row[0]:.4f}" if row[0] else "",
            f"{row[1]:.1f}" if row[1] else "",
            f"{row[2]:.1f}" if row[2] else "",
            f"{row[3]:.1f}" if row[3] else "",
            row[4] or "",
            (row[5] or "").strip(),
            row[6] or "",
            row[7] or "",
            row[8] or 1,
        ])


def run_once(db_path: str, args) -> None:
    """@brief Execute one query and print the results.
    @param db_path  Path to the SQLite database.
    @param args     Parsed CLI arguments (filters, sort, output format).
    """
    if not Path(db_path).exists():
        print(f"No database at {db_path} — run scan.sh first")
        return

    db  = sqlite3.connect(db_path)
    sql, params = build_query(args)
    rows = db.execute(sql, params).fetchall()
    db.close()

    if args.csv:
        print_csv(rows)
    else:
        print_table(rows, db_path, args)


def main() -> None:
    """@brief Entry point: parse arguments and display signals from the database."""
    ap = argparse.ArgumentParser(
        description="Display signals from the SDR scan database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db",         default=str(DEFAULT_DB))
    ap.add_argument("--sort",       default="freq",
                    choices=list(SORT_MAP),
                    metavar="{freq|time|last|power|snr|hits|mod}")
    ap.add_argument("--recent",     type=float, metavar="MIN",
                    help="Only signals from the last N minutes")
    ap.add_argument("--classified", action="store_true",
                    help="Only show classified signals")
    ap.add_argument("--band",       metavar=f"{{{','.join(BANDS)}}}",
                    help="Filter by frequency band")
    ap.add_argument("--freq",       type=float, metavar="MHZ",
                    help="Filter near frequency (MHz ±5)")
    ap.add_argument("--csv",        action="store_true")
    ap.add_argument("--watch",      action="store_true",
                    help="Refresh every 10 seconds")
    args = ap.parse_args()

    if args.watch:
        try:
            while True:
                os.system("clear")
                print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()) +
                      "  [watch — Ctrl+C to exit]")
                run_once(args.db, args)
                time.sleep(10)
        except KeyboardInterrupt:
            print()
    else:
        run_once(args.db, args)


if __name__ == "__main__":
    main()

# ========================================================================
# End of file — OpenRFStack
# Subject to Personal Use License
# https://github.com/OpenRFStack
# ========================================================================
