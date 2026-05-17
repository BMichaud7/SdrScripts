#!/usr/bin/env python3
"""
signal_logger.py — AMQP → SQLite signal persistence.

Subscribes to rf.detections and rf.analysis, deduplicates within a rolling
60-second / 500 kHz window, and writes every unique signal to a SQLite DB.

Usage:
    python3 signal_logger.py [--db signals.db] [--broker amqp://localhost:5672]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow PYTHONPATH override for proton (e.g. /tmp/proton_pkg)
try:
    import proton
    import proton.handlers
    import proton.reactor
except ModuleNotFoundError:
    extra = os.environ.get("PROTON_PATH", "/tmp/proton_pkg")
    sys.path.insert(0, extra)
    import proton
    import proton.handlers
    import proton.reactor


DEDUP_HZ   = 100_000   # merge signals within 100 kHz (FM stations spaced 200 kHz apart)
DEDUP_SEC  = 600       # and within 10 minutes (covers multiple sweep passes)


# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    first_seen   TEXT    NOT NULL,          -- ISO-8601 UTC
    last_seen    TEXT    NOT NULL,
    timestamp_ms INTEGER NOT NULL,          -- first-detection epoch ms
    freq_hz      REAL    NOT NULL,
    freq_mhz     REAL    NOT NULL,
    bandwidth_hz REAL,
    power_db     REAL,                      -- from RF_DETECTION
    snr_db       REAL,                      -- from ANALYSIS_RESULT
    modulation   TEXT    DEFAULT '',        -- e.g. "FM_WB", "QAM16", "BPSK"
    mod_class    TEXT    DEFAULT '',        -- "analog" | "digital" | "unclassified"
    is_ofdm      INTEGER DEFAULT 0,
    is_burst     INTEGER DEFAULT 0,
    is_fhss      INTEGER DEFAULT 0,
    hits         INTEGER DEFAULT 1,         -- how many times re-detected
    classified   INTEGER DEFAULT 0,         -- 1 = AnalysisApp classified it
    scanner_id   TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_freq_time ON signals (freq_hz, timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_time      ON signals (timestamp_ms DESC);
"""


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


# ── AMQP handler ──────────────────────────────────────────────────────────────

class _Handler(proton.handlers.MessagingHandler):

    def __init__(self, broker: str, creds: tuple[str, str],
                 db: sqlite3.Connection, lock: threading.Lock,
                 stop_ev: threading.Event):
        super().__init__()
        self._broker  = broker
        self._user, self._pw = creds
        self._db      = db
        self._lock    = lock
        self._stop_ev = stop_ev
        # In-memory dedup: freq_hz → (timestamp_ms, row_id)
        self._recent: dict[float, tuple[int, int]] = {}

    def on_start(self, ev):
        c = ev.container.connect(
            self._broker, user=self._user, password=self._pw,
            sasl_enabled=True, allowed_mechs="PLAIN",
        )
        ev.container.create_receiver(c, "rf.detections")
        ev.container.create_receiver(c, "rf.analysis")
        ev.container.schedule(5.0, self)

    def on_timer_task(self, ev):
        if self._stop_ev.is_set():
            ev.container.stop()
            return
        ev.container.schedule(5.0, self)
        self._flush_old()

    def on_message(self, ev):
        try:
            body = ev.message.body
            msg  = json.loads(body if isinstance(body, str) else body.decode())
        except Exception:
            return

        mtype = msg.get("msg_type", "")
        if mtype == "RF_DETECTION":
            self._handle_detection(msg)
        elif mtype == "ANALYSIS_RESULT":
            self._handle_analysis(msg)

    # ── Detection (from AcquisitionApp) ───────────────────────────────────────

    def _handle_detection(self, msg: dict) -> None:
        freq  = float(msg.get("center_freq_hz", 0))
        bw    = float(msg.get("bandwidth_hz", 0))
        power = float(msg.get("power_db", 0))
        sid   = str(msg.get("scanner_id", ""))
        ts_ms = int(msg.get("timestamp_ms", now_ms()))

        if freq <= 0 or bw <= 0 or not (-150 < power < 20):
            return

        row_id = self._find_recent(freq, ts_ms)
        iso    = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")

        with self._lock:
            if row_id:
                self._db.execute(
                    "UPDATE signals SET last_seen=?, hits=hits+1 WHERE id=?",
                    (iso, row_id),
                )
                self._db.commit()
            else:
                cur = self._db.execute(
                    """INSERT INTO signals
                       (first_seen, last_seen, timestamp_ms,
                        freq_hz, freq_mhz, bandwidth_hz, power_db, scanner_id)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (iso, iso, ts_ms, freq, freq / 1e6, bw, power, sid),
                )
                self._db.commit()
                row_id = cur.lastrowid
                self._recent[freq] = (ts_ms, row_id)

    # ── Analysis result (from AnalysisApp) ────────────────────────────────────

    def _handle_analysis(self, msg: dict) -> None:
        freq  = float(msg.get("center_freq_hz", 0))
        bw    = float(msg.get("bandwidth_hz", 0))
        snr   = float(msg.get("snr_db", 0))
        cls   = bool(msg.get("classified", False))
        ts_ms = int(msg.get("timestamp_ms", now_ms()))

        if freq <= 0:
            return

        # Build modulation string
        mod_str   = ""
        mod_class = "unclassified"
        m = msg.get("modulation", {})
        if m.get("analog"):
            mod_str   = m["analog"]
            mod_class = "analog"
        elif m.get("digital"):
            mod_str   = m["digital"]
            if m.get("m_ary", 0) > 1:
                mod_str += f"-{m['m_ary']}"
            mod_class = "digital"

        ch = msg.get("channel_structure", {})
        is_ofdm  = int(bool(m.get("is_ofdm") or ch.get("is_ofdm")))
        is_burst = int(bool(ch.get("is_burst")))
        is_fhss  = int(bool(ch.get("is_fhss")))
        iso      = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")

        row_id = self._find_recent(freq, ts_ms)

        with self._lock:
            if row_id:
                self._db.execute(
                    """UPDATE signals SET
                       last_seen=?, snr_db=?, bandwidth_hz=COALESCE(NULLIF(bandwidth_hz,0),?),
                       modulation=?, mod_class=?, is_ofdm=?, is_burst=?, is_fhss=?,
                       classified=?, hits=hits+1
                       WHERE id=?""",
                    (iso, snr, bw, mod_str, mod_class,
                     is_ofdm, is_burst, is_fhss, int(cls), row_id),
                )
            else:
                cur = self._db.execute(
                    """INSERT INTO signals
                       (first_seen, last_seen, timestamp_ms,
                        freq_hz, freq_mhz, bandwidth_hz, snr_db,
                        modulation, mod_class, is_ofdm, is_burst, is_fhss, classified)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (iso, iso, ts_ms, freq, freq / 1e6, bw, snr,
                     mod_str, mod_class, is_ofdm, is_burst, is_fhss, int(cls)),
                )
                row_id = cur.lastrowid
                self._recent[freq] = (ts_ms, row_id)

            self._db.commit()

    # ── Dedup helpers ─────────────────────────────────────────────────────────

    def _find_recent(self, freq: float, ts_ms: int) -> int | None:
        for known_freq, (known_ts, row_id) in self._recent.items():
            if abs(freq - known_freq) < DEDUP_HZ:
                if (ts_ms - known_ts) < DEDUP_SEC * 1000:
                    return row_id
        return None

    def _flush_old(self) -> None:
        cutoff = now_ms() - DEDUP_SEC * 1000
        self._recent = {
            f: (ts, rid) for f, (ts, rid) in self._recent.items()
            if ts >= cutoff
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Log SDR signals to SQLite")
    ap.add_argument("--db",     default="signals.db", help="SQLite database path")
    ap.add_argument("--broker", default="amqp://localhost:5672")
    ap.add_argument("--user",   default="sdr_ctrl")
    ap.add_argument("--pass",   default="sdr_hw_test", dest="password")
    args = ap.parse_args()

    db   = open_db(args.db)
    lock = threading.Lock()
    stop = threading.Event()

    print(f"[signal_logger] DB: {args.db}")
    print(f"[signal_logger] Broker: {args.broker}")
    print(f"[signal_logger] Dedup: {DEDUP_HZ/1e3:.0f} kHz / {DEDUP_SEC}s window")
    print(f"[signal_logger] Listening on rf.detections + rf.analysis  (Ctrl+C to stop)")

    handler   = _Handler(args.broker, (args.user, args.password), db, lock, stop)
    container = proton.reactor.Container(handler)

    t = threading.Thread(target=container.run, daemon=True)
    t.start()

    try:
        while t.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[signal_logger] Stopping …")
        stop.set()
        t.join(timeout=5)

    with lock:
        count = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    print(f"[signal_logger] Done — {count} signals in DB")
    db.close()


if __name__ == "__main__":
    main()
