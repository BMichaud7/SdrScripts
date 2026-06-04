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
@file replay_detections.py
@brief Replay unclassified signals from the SQLite DB back through the AMQP pipeline.

Reads unclassified (or all) signals from the DB, publishes them to rf.detections
one at a time with a configurable delay, and lets AnalysisApp classify them.
Best used when AcquisitionApp is NOT running (SCAN is stopped), so AnalysisApp
has exclusive SDR access.

Usage:
    # Replay top 50 strongest unclassified signals, 8s gap between each
    python3 replay_detections.py --db signals.db --count 50

    # Replay all unclassified signals, 10s gap
    python3 replay_detections.py --db signals.db --all --gap 10

    # Include already-classified signals (re-classify)
    python3 replay_detections.py --db signals.db --include-classified --count 100
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

try:
    import proton.reactor
    import proton.handlers
    import proton
except ModuleNotFoundError:
    extra = os.environ.get("PROTON_PATH", "/tmp/proton_pkg")
    sys.path.insert(0, extra)
    import proton.reactor
    import proton.handlers
    import proton


def open_db(path: str) -> sqlite3.Connection:
    """@brief Open a SQLite database with row_factory set to sqlite3.Row.
    @param path  Path to the @c .db file.
    @return      Open sqlite3.Connection.
    """
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def fetch_signals(db: sqlite3.Connection, count: int, include_classified: bool) -> list[dict]:
    """@brief Query signals for replay, ordered by power descending.
    @param db                   Open database connection.
    @param count                Maximum rows to return (0 = unlimited).
    @param include_classified   When False, only unclassified rows are returned.
    @return                     List of row dicts.
    """
    where = "" if include_classified else "WHERE classified = 0"
    limit = f"LIMIT {count}" if count > 0 else ""
    rows = db.execute(f"""
        SELECT id, freq_hz, bandwidth_hz, power_db, snr_db, timestamp_ms, scanner_id
        FROM signals
        {where}
        ORDER BY power_db DESC
        {limit}
    """).fetchall()
    return [dict(r) for r in rows]


class _Publisher(proton.handlers.MessagingHandler):
    """@brief Proton MessagingHandler that publishes replay detections one by one.

    Sends each signal as a JSON RF_DETECTION AMQP message to the configured topic,
    spacing them by @p gap_sec seconds to give AnalysisApp time to classify each one.
    Closes the connection after the last signal plus a 5-second drain window.
    """

    def __init__(self, broker: str, user: str, password: str,
                 signals: list[dict], gap_sec: float, topic: str):
        """@brief Construct the publisher.
        @param broker   AMQP broker URL.
        @param user     AMQP username.
        @param password AMQP password.
        @param signals  Rows from fetch_signals().
        @param gap_sec  Seconds to wait between each published signal.
        @param topic    AMQP topic address (e.g. "rf.detections").
        """
        super().__init__()
        self._broker   = broker
        self._user     = user
        self._password = password
        self._signals  = signals
        self._gap      = gap_sec
        self._topic    = topic
        self._idx      = 0
        self._sender   = None

    def on_start(self, ev):
        """@brief Connect to the broker and open a sender to the detection topic."""
        conn = ev.container.connect(
            self._broker, user=self._user, password=self._password,
            sasl_enabled=True, allowed_mechs="PLAIN",
        )
        self._sender = ev.container.create_sender(conn, self._topic)

    def on_sendable(self, ev):
        """@brief Send the next signal when the sender has credit, then schedule the gap timer."""
        if self._idx >= len(self._signals):
            ev.connection.close()
            return
        if ev.sender.credit < 1:
            return

        sig = self._signals[self._idx]
        body: dict = {
            "msg_type":                "RF_DETECTION",
            "schema_version":          "1.2",
            "scanner_id":              sig.get("scanner_id") or "replay",
            "center_freq_hz":          sig["freq_hz"],
            "bandwidth_hz":            sig["bandwidth_hz"] or 200_000,
            "power_db":                sig["power_db"] or -60.0,
            "timestamp_ms":            sig["timestamp_ms"] or int(time.time() * 1000),
            "snapshot_sample_rate_sps": 20_000_000,
        }
        if sig.get("snr_db") is not None:
            body["snr_db"] = sig["snr_db"]
        msg_body = json.dumps(body)

        m = proton.Message()
        m.body = msg_body
        m.content_type = "application/json"
        ev.sender.send(m)

        freq_mhz = sig["freq_hz"] / 1e6
        print(f"  [{self._idx+1}/{len(self._signals)}] "
              f"{freq_mhz:.3f} MHz  {sig['power_db']:.1f} dBm  "
              f"(waiting {self._gap:.0f}s for analysis…)")

        self._idx += 1
        if self._idx < len(self._signals):
            ev.container.schedule(self._gap, self)
        else:
            # Final signal sent — give AnalysisApp time to finish, then close
            ev.container.schedule(self._gap + 5, self)

    def on_timer_task(self, ev):
        """@brief Timer callback: fire the next send after the gap interval."""
        self.on_sendable(ev)

    def on_transport_error(self, ev):
        print(f"[replay] transport error: {ev.transport.error}")

    def on_connection_error(self, ev):
        print(f"[replay] connection error: {ev.connection.error}")


def main():
    """@brief Entry point: load signals from the DB and replay them through AMQP."""
    ap = argparse.ArgumentParser(description="Replay SDR signals for batch classification")
    ap.add_argument("--db",     default="signals.db")
    ap.add_argument("--broker", default="amqp://localhost:5672")
    ap.add_argument("--user",   default="sdr_ctrl")
    ap.add_argument("--pass",   default="sdr_hw_test", dest="password")
    ap.add_argument("--topic",  default="rf.detections")
    ap.add_argument("--count",  type=int, default=50,
                    help="Number of signals to replay (0 = all). Default: 50")
    ap.add_argument("--gap",    type=float, default=8.0,
                    help="Seconds between signals (allow time for AnalysisApp). Default: 8s")
    ap.add_argument("--all",    action="store_true",
                    help="Replay all signals regardless of --count")
    ap.add_argument("--include-classified", action="store_true",
                    help="Include already-classified signals")
    args = ap.parse_args()

    count = 0 if args.all else args.count

    db      = open_db(args.db)
    signals = fetch_signals(db, count, args.include_classified)
    db.close()

    if not signals:
        print("[replay] No signals found matching criteria.")
        return

    print(f"[replay] DB: {args.db}")
    print(f"[replay] Broker: {args.broker}")
    print(f"[replay] Replaying {len(signals)} signals  ({args.gap:.0f}s gap each)")
    print(f"[replay] Estimated time: {len(signals) * args.gap / 60:.1f} minutes")
    print(f"[replay] Publishing to: {args.topic}")
    print("")
    print("  NOTE: For best results, stop AcquisitionApp first so AnalysisApp")
    print("        has exclusive SDR access. signal_logger.py should be running")
    print("        to save classification results back to the DB.")
    print("")

    handler   = _Publisher(args.broker, args.user, args.password,
                            signals, args.gap, args.topic)
    container = proton.reactor.Container(handler)

    try:
        container.run()
    except KeyboardInterrupt:
        print(f"\n[replay] Interrupted after {handler._idx} signals.")

    print(f"\n[replay] Done — sent {handler._idx}/{len(signals)} signals.")


if __name__ == "__main__":
    main()

# ========================================================================
# End of file — OpenRFStack
# Subject to Personal Use License
# https://github.com/OpenRFStack
# ========================================================================
