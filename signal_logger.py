#!/usr/bin/env python3
"""
signal_logger.py — AMQP → SQLite / PostgreSQL signal persistence.

Subscribes to rf.detections and rf.analysis, deduplicates within a rolling
window, and writes every unique signal to a database.

Usage:
    # SQLite (local dev / hw-test):
    python3 signal_logger.py [--db signals.db] [--broker amqp://localhost:5672]

    # PostgreSQL (production / k3s):
    python3 signal_logger.py --pg-host localhost --pg-user sdr --pg-pass secret
"""
from __future__ import annotations

import argparse
import json
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


DEDUP_HZ   = 100_000   # merge signals within 100 kHz
DEDUP_SEC  = 600       # and within 10 minutes


# ── Database backends ─────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    freq_hz      REAL    NOT NULL,
    freq_mhz     REAL    NOT NULL,
    bandwidth_hz REAL,
    power_db     REAL,
    snr_db       REAL,
    modulation   TEXT    DEFAULT '',
    mod_class    TEXT    DEFAULT '',
    is_ofdm      INTEGER DEFAULT 0,
    is_burst     INTEGER DEFAULT 0,
    is_fhss      INTEGER DEFAULT 0,
    hits         INTEGER DEFAULT 1,
    classified   INTEGER DEFAULT 0,
    scanner_id   TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_freq_time ON signals (freq_hz, timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_time      ON signals (timestamp_ms DESC);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           BIGSERIAL       PRIMARY KEY,
    first_seen   TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ     NOT NULL DEFAULT now(),
    timestamp_ms BIGINT          NOT NULL,
    freq_hz      DOUBLE PRECISION NOT NULL,
    freq_mhz     DOUBLE PRECISION NOT NULL,
    bandwidth_hz DOUBLE PRECISION,
    power_db     REAL,
    snr_db       REAL,
    modulation   TEXT            DEFAULT '',
    mod_class    TEXT            DEFAULT '',
    is_ofdm      BOOLEAN         DEFAULT false,
    is_burst     BOOLEAN         DEFAULT false,
    is_fhss      BOOLEAN         DEFAULT false,
    hits         INTEGER         NOT NULL DEFAULT 1,
    classified   BOOLEAN         NOT NULL DEFAULT false,
    scanner_id   TEXT            DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signals_freq ON signals (freq_hz, timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (timestamp_ms DESC);
"""


class _SqliteBackend:
    def __init__(self, path: str):
        self._db   = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SQLITE_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()

    def insert(self, row: dict) -> int:
        iso = row["iso"]
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO signals (first_seen,last_seen,timestamp_ms,"
                "freq_hz,freq_mhz,bandwidth_hz,power_db,scanner_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (iso, iso, row["ts_ms"], row["freq_hz"], row["freq_mhz"],
                 row.get("bw"), row.get("power_db"), row.get("scanner_id", "")))
            self._db.commit()
            return cur.lastrowid

    def update_hit(self, row_id: int, iso: str):
        with self._lock:
            self._db.execute(
                "UPDATE signals SET last_seen=?,hits=hits+1 WHERE id=?", (iso, row_id))
            self._db.commit()

    def update_analysis(self, row_id: int, row: dict):
        iso = row["iso"]
        with self._lock:
            self._db.execute(
                "UPDATE signals SET last_seen=?,snr_db=?,"
                "bandwidth_hz=COALESCE(NULLIF(bandwidth_hz,0),?),"
                "modulation=?,mod_class=?,is_ofdm=?,is_burst=?,is_fhss=?,"
                "classified=?,hits=hits+1 WHERE id=?",
                (iso, row.get("snr_db"), row.get("bw"),
                 row.get("mod", ""), row.get("mod_class", "unclassified"),
                 int(row.get("is_ofdm", False)), int(row.get("is_burst", False)),
                 int(row.get("is_fhss", False)), int(row.get("classified", False)),
                 row_id))
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    def close(self):
        self._db.close()


class _PgBackend:
    def __init__(self, host: str, port: int, dbname: str, user: str, password: str):
        try:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
        except ImportError:
            print("[signal_logger] ERROR: psycopg2 not installed. "
                  "pip install psycopg2-binary", file=sys.stderr)
            sys.exit(1)
        dsn = f"host={host} port={port} dbname={dbname} user={user} password={password}"
        self._conn  = psycopg2.connect(dsn)
        self._conn.autocommit = False
        self._lock  = threading.Lock()
        with self._conn.cursor() as cur:
            cur.execute(_PG_SCHEMA)
        self._conn.commit()

    def _exec(self, sql: str, params=()):
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                result = cur.fetchone() if cur.description else None
            self._conn.commit()
            return result

    def insert(self, row: dict) -> int:
        iso = row["iso"]
        result = self._exec(
            "INSERT INTO signals (first_seen,last_seen,timestamp_ms,"
            "freq_hz,freq_mhz,bandwidth_hz,power_db,scanner_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (iso, iso, row["ts_ms"], row["freq_hz"], row["freq_mhz"],
             row.get("bw"), row.get("power_db"), row.get("scanner_id", "")))
        return result[0]

    def update_hit(self, row_id: int, iso: str):
        self._exec(
            "UPDATE signals SET last_seen=%s,hits=hits+1 WHERE id=%s", (iso, row_id))

    def update_analysis(self, row_id: int, row: dict):
        self._exec(
            "UPDATE signals SET last_seen=%s,snr_db=%s,"
            "bandwidth_hz=COALESCE(NULLIF(bandwidth_hz,0),%s),"
            "modulation=%s,mod_class=%s,is_ofdm=%s,is_burst=%s,is_fhss=%s,"
            "classified=%s,hits=hits+1 WHERE id=%s",
            (row["iso"], row.get("snr_db"), row.get("bw"),
             row.get("mod", ""), row.get("mod_class", "unclassified"),
             row.get("is_ofdm", False), row.get("is_burst", False),
             row.get("is_fhss", False), row.get("classified", False),
             row_id))

    def count(self) -> int:
        result = self._exec("SELECT COUNT(*) FROM signals")
        return result[0] if result else 0

    def close(self):
        self._conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


# ── AMQP handler ──────────────────────────────────────────────────────────────

class _Handler(proton.handlers.MessagingHandler):

    def __init__(self, broker: str, creds: tuple[str, str],
                 backend, stop_ev: threading.Event):
        super().__init__()
        self._broker  = broker
        self._user, self._pw = creds
        self._backend = backend
        self._stop_ev = stop_ev
        # In-memory dedup: _buckets[bucket] = (freq_hz, timestamp_ms, row_id)
        self._buckets: dict[int, tuple[float, int, int]] = {}

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
        iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")

        if row_id:
            self._backend.update_hit(row_id, iso)
        else:
            row_id = self._backend.insert({
                "iso": iso, "ts_ms": ts_ms,
                "freq_hz": freq, "freq_mhz": freq / 1e6,
                "bw": bw, "power_db": power, "scanner_id": sid,
            })
            self._store_recent(freq, ts_ms, row_id)

    # ── Analysis result (from AnalysisApp) ────────────────────────────────────

    def _handle_analysis(self, msg: dict) -> None:
        freq  = float(msg.get("center_freq_hz", 0))
        bw    = float(msg.get("bandwidth_hz", 0))
        snr   = float(msg.get("snr_db", 0))
        cls   = bool(msg.get("classified", False))
        ts_ms = int(msg.get("timestamp_ms", now_ms()))

        if freq <= 0:
            return

        mod_str, mod_class = "", "unclassified"
        m = msg.get("modulation", {})
        if m.get("analog"):
            mod_str, mod_class = m["analog"], "analog"
        elif m.get("digital"):
            mod_str = m["digital"] + (f"-{m['m_ary']}" if m.get("m_ary", 0) > 1 else "")
            mod_class = "digital"

        ch = msg.get("channel_structure", {})
        iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        row_id = self._find_recent(freq, ts_ms)

        if row_id:
            self._backend.update_analysis(row_id, {
                "iso": iso, "snr_db": snr, "bw": bw,
                "mod": mod_str, "mod_class": mod_class,
                "is_ofdm": bool(m.get("is_ofdm") or ch.get("is_ofdm")),
                "is_burst": bool(ch.get("is_burst")),
                "is_fhss":  bool(ch.get("is_fhss")),
                "classified": cls,
            })
        # Discard analysis without a matching detection (stale/out-of-range freq).

    # ── Dedup helpers ─────────────────────────────────────────────────────────

    def _bucket(self, freq: float) -> int:
        return int(freq / DEDUP_HZ)

    def _find_recent(self, freq: float, ts_ms: int) -> int | None:
        b = self._bucket(freq)
        for candidate in (b - 1, b, b + 1):
            entry = self._buckets.get(candidate)
            if entry is None:
                continue
            ef, ets, erid = entry
            if abs(freq - ef) < DEDUP_HZ and (ts_ms - ets) < DEDUP_SEC * 1000:
                return erid
        return None

    def _store_recent(self, freq: float, ts_ms: int, row_id: int) -> None:
        self._buckets[self._bucket(freq)] = (freq, ts_ms, row_id)

    def _flush_old(self) -> None:
        cutoff = now_ms() - DEDUP_SEC * 1000
        self._buckets = {
            b: (f, ts, rid)
            for b, (f, ts, rid) in self._buckets.items()
            if ts >= cutoff
        }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Log SDR signals to SQLite or PostgreSQL")
    # AMQP
    ap.add_argument("--broker",   default="amqp://localhost:5672")
    ap.add_argument("--user",     default="sdr_ctrl")
    ap.add_argument("--pass",     default="sdr_hw_test", dest="password")
    # SQLite backend (default when no --pg-host given)
    ap.add_argument("--db",       default="signals.db", help="SQLite path (default backend)")
    # PostgreSQL backend
    ap.add_argument("--pg-host",  default=os.environ.get("PG_HOST", ""))
    ap.add_argument("--pg-port",  type=int, default=int(os.environ.get("PG_PORT", "5432")))
    ap.add_argument("--pg-db",    default=os.environ.get("PG_DB",   "sdr_scanner"))
    ap.add_argument("--pg-user",  default=os.environ.get("PG_USER", "sdr"))
    ap.add_argument("--pg-pass",  default=os.environ.get("PG_PASS", ""))
    args = ap.parse_args()

    if args.pg_host:
        backend = _PgBackend(args.pg_host, args.pg_port, args.pg_db,
                             args.pg_user, args.pg_pass)
        print(f"[signal_logger] Backend: PostgreSQL {args.pg_host}/{args.pg_db}")
    else:
        backend = _SqliteBackend(args.db)
        print(f"[signal_logger] Backend: SQLite {args.db}")

    stop = threading.Event()
    print(f"[signal_logger] Broker: {args.broker}")
    print(f"[signal_logger] Dedup: {DEDUP_HZ/1e3:.0f} kHz / {DEDUP_SEC}s window")
    print(f"[signal_logger] Listening on rf.detections + rf.analysis  (Ctrl+C to stop)")

    handler   = _Handler(args.broker, (args.user, args.password), backend, stop)
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

    print(f"[signal_logger] Done — {backend.count()} signals in DB")
    backend.close()


if __name__ == "__main__":
    main()
