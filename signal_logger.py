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
@file signal_logger.py
@brief AMQP subscriber that persists RF detections and analysis results to a database.

Subscribes to the @c rf.detections and @c rf.analysis AMQP topics, deduplicates
signals within a rolling 100 kHz / 10-minute window, and writes every unique
signal to SQLite (default) and/or PostgreSQL.

Two backend classes implement the same interface:
- @ref _SqliteBackend — thread-safe WAL-mode SQLite; no extra dependencies.
- @ref _PgBackend — PostgreSQL via psycopg2; requires `pip install psycopg2-binary`.
- @ref _DualBackend — fan-out wrapper: writes to both simultaneously.

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


DEDUP_HZ   = 10_000    # signals within 10 kHz treated as same frequency
DEDUP_SEC  = 600       # dedup window: 10 minutes


# ── Database backends ─────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    first_seen      TEXT    NOT NULL,
    last_seen       TEXT    NOT NULL,
    freq_hz         REAL    NOT NULL,
    freq_mhz        REAL    NOT NULL,
    bandwidth_hz    REAL,
    power_db        REAL,
    snr_db          REAL,
    scanner_id      TEXT    NOT NULL DEFAULT '',
    channel         INTEGER NOT NULL DEFAULT 0,
    modulation      TEXT    NOT NULL DEFAULT '',
    mod_class       TEXT    NOT NULL DEFAULT '',
    is_ofdm         INTEGER NOT NULL DEFAULT 0,
    is_burst        INTEGER NOT NULL DEFAULT 0,
    is_fhss         INTEGER NOT NULL DEFAULT 0,
    symbol_rate_sps REAL,
    bit_rate_bps    REAL,
    hypothesis      TEXT    NOT NULL DEFAULT '',
    hyp_category    TEXT    NOT NULL DEFAULT '',
    hyp_confidence  REAL             DEFAULT 0,
    classified      INTEGER NOT NULL DEFAULT 0,
    rule_confidence REAL             DEFAULT 0,
    onnx_used       INTEGER NOT NULL DEFAULT 0,
    onnx_confidence REAL             DEFAULT 0,
    fast_path       INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT    NOT NULL DEFAULT '',
    hits            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_signals_freq ON signals (freq_hz);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_signals_mod  ON signals (modulation);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL        PRIMARY KEY,
    first_seen      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    freq_hz         DOUBLE PRECISION NOT NULL,
    freq_mhz        DOUBLE PRECISION NOT NULL,
    bandwidth_hz    DOUBLE PRECISION,
    power_db        REAL,
    snr_db          REAL,
    scanner_id      TEXT             NOT NULL DEFAULT '',
    channel         SMALLINT         NOT NULL DEFAULT 0,
    modulation      TEXT             NOT NULL DEFAULT '',
    mod_class       TEXT             NOT NULL DEFAULT '',
    is_ofdm         BOOLEAN          NOT NULL DEFAULT false,
    is_burst        BOOLEAN          NOT NULL DEFAULT false,
    is_fhss         BOOLEAN          NOT NULL DEFAULT false,
    symbol_rate_sps DOUBLE PRECISION,
    bit_rate_bps    DOUBLE PRECISION,
    hypothesis      TEXT             NOT NULL DEFAULT '',
    hyp_category    TEXT             NOT NULL DEFAULT '',
    hyp_confidence  REAL                      DEFAULT 0,
    classified      BOOLEAN          NOT NULL DEFAULT false,
    rule_confidence REAL                      DEFAULT 0,
    onnx_used       BOOLEAN          NOT NULL DEFAULT false,
    onnx_confidence REAL                      DEFAULT 0,
    fast_path       BOOLEAN          NOT NULL DEFAULT false,
    reject_reason   TEXT             NOT NULL DEFAULT '',
    hits            INTEGER          NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_signals_freq ON signals (freq_hz);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals (last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_signals_mod  ON signals (modulation);
"""


class _SqliteBackend:
    """@brief Thread-safe SQLite signal persistence backend.

    Uses WAL journal mode for concurrent reads during writes.
    All public methods acquire a threading.Lock internally — safe to call
    from the AMQP handler thread without additional synchronisation.
    """

    def __init__(self, path: str):
        """@brief Open (or create) the SQLite database and apply the schema.

        Detects an incompatible old schema (signals table with timestamp_ms NOT NULL)
        and recreates the table automatically, printing a warning.

        @param path  Path to the @c .db file.
        """
        self._db   = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # Detect old schema: timestamp_ms column with NOT NULL (no default)
        cols = {r[1]: r[3] for r in self._db.execute("PRAGMA table_info(signals)").fetchall()}
        if "timestamp_ms" in cols and cols["timestamp_ms"] == 1:  # notnull=1
            print("[signal_logger] WARNING: old signals schema detected — recreating table "
                  "(existing rows will be lost)", file=sys.stderr)
            self._db.execute("DROP TABLE IF EXISTS signals")
            self._db.commit()
        self._db.executescript(_SQLITE_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()

    def insert(self, row: dict) -> int:
        """@brief Insert a new unclassified signal row (detection, no modulation yet).
        @param row  Dict with keys: iso, freq_hz, freq_mhz, bw, power_db, scanner_id.
        @return     Auto-increment row ID of the inserted record.
        """
        iso = row["iso"]
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO signals (first_seen,last_seen,"
                "freq_hz,freq_mhz,bandwidth_hz,power_db,scanner_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (iso, iso, row["freq_hz"], row["freq_mhz"],
                 row.get("bw"), row.get("power_db"), row.get("scanner_id", "")))
            self._db.commit()
            return cur.lastrowid

    def insert_classified(self, row: dict) -> int:
        """@brief Insert a new classified signal row directly (no prior detection row).
        @param row  Dict with all signal fields including modulation and hypothesis.
        @return     Auto-increment row ID of the inserted record.
        """
        iso = row["iso"]
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO signals "
                "(first_seen,last_seen,freq_hz,freq_mhz,bandwidth_hz,snr_db,scanner_id,"
                " modulation,mod_class,is_ofdm,is_burst,is_fhss,"
                " hypothesis,hyp_category,hyp_confidence,classified,hits) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (iso, iso, row["freq_hz"], row["freq_mhz"], row.get("bw"),
                 row.get("snr_db"), row.get("scanner_id", ""),
                 row.get("mod", ""), row.get("mod_class", ""),
                 int(row.get("is_ofdm", False)), int(row.get("is_burst", False)),
                 int(row.get("is_fhss", False)),
                 row.get("hypothesis", ""), row.get("hyp_category", ""),
                 row.get("hyp_confidence", 0),
                 int(row.get("classified", False))))
            self._db.commit()
            return cur.lastrowid

    def update_hit(self, row_id: int, iso: str):
        """@brief Increment the hit counter for an existing signal.
        @param row_id  Row ID to update.
        @param iso     ISO 8601 timestamp for last_seen.
        """
        with self._lock:
            self._db.execute(
                "UPDATE signals SET last_seen=?,hits=hits+1 WHERE id=?", (iso, row_id))
            self._db.commit()

    def update_analysis(self, row_id: int, row: dict):
        """@brief Write classification results from an ANALYSIS_RESULT message.
        @param row_id  Row ID of the matching detection.
        @param row     Dict with modulation, hypothesis, snr_db, bw, classified, etc.
        """
        iso = row["iso"]
        with self._lock:
            self._db.execute(
                "UPDATE signals SET last_seen=?,snr_db=?,"
                "bandwidth_hz=COALESCE(NULLIF(bandwidth_hz,0),?),"
                "modulation=?,mod_class=?,is_ofdm=?,is_burst=?,is_fhss=?,"
                "hypothesis=?,hyp_category=?,hyp_confidence=?,"
                "classified=?,hits=hits+1 WHERE id=?",
                (iso, row.get("snr_db"), row.get("bw"),
                 row.get("mod", ""), row.get("mod_class", "unclassified"),
                 int(row.get("is_ofdm", False)), int(row.get("is_burst", False)),
                 int(row.get("is_fhss", False)),
                 row.get("hypothesis", ""), row.get("hyp_category", ""),
                 row.get("hyp_confidence", 0),
                 int(row.get("classified", False)),
                 row_id))
            self._db.commit()

    def count(self) -> int:
        """@brief Return the total number of rows in the signals table."""
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]

    def close(self):
        self._db.close()


class _PgBackend:
    """@brief PostgreSQL signal persistence backend via psycopg2.

    Raises RuntimeError at construction if psycopg2 is not installed.
    All public methods are thread-safe (internal Lock + autocommit=False).
    """

    def __init__(self, host: str, port: int, dbname: str, user: str, password: str):
        """@brief Connect to PostgreSQL and apply the schema.
        @param host     Database host.
        @param port     Database port.
        @param dbname   Database name.
        @param user     Database user.
        @param password Database password.
        @throws RuntimeError if psycopg2 is not installed.
        """
        try:
            import psycopg2
        except ImportError:
            raise RuntimeError(
                "psycopg2 not installed — run: pip install psycopg2-binary"
            )
        dsn = f"host={host} port={port} dbname={dbname} user={user} password={password}"
        self._conn  = psycopg2.connect(dsn)
        self._conn.autocommit = False
        self._lock  = threading.Lock()
        with self._conn.cursor() as cur:
            cur.execute(_PG_SCHEMA)
        self._conn.commit()

    def _exec(self, sql: str, params=()):
        """@brief Execute SQL and return the first row (or None).  Thread-safe.
        @param sql     SQL statement.
        @param params  Positional parameters.
        @return        First row as a tuple, or None.
        """
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                result = cur.fetchone() if cur.description else None
            self._conn.commit()
            return result

    def insert(self, row: dict) -> int:
        """@brief Insert a new unclassified signal row."""
        iso = row["iso"]
        result = self._exec(
            "INSERT INTO signals (first_seen,last_seen,"
            "freq_hz,freq_mhz,bandwidth_hz,power_db,scanner_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (iso, iso, row["freq_hz"], row["freq_mhz"],
             row.get("bw"), row.get("power_db"), row.get("scanner_id", "")))
        return result[0]

    def insert_classified(self, row: dict) -> int:
        """@brief Insert a new classified signal row directly."""
        iso = row["iso"]
        result = self._exec(
            "INSERT INTO signals "
            "(first_seen,last_seen,freq_hz,freq_mhz,bandwidth_hz,snr_db,scanner_id,"
            " modulation,mod_class,is_ofdm,is_burst,is_fhss,"
            " hypothesis,hyp_category,hyp_confidence,classified,hits) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING id",
            (iso, iso, row["freq_hz"], row["freq_mhz"], row.get("bw"),
             row.get("snr_db"), row.get("scanner_id", ""),
             row.get("mod", ""), row.get("mod_class", ""),
             row.get("is_ofdm", False), row.get("is_burst", False),
             row.get("is_fhss", False),
             row.get("hypothesis", ""), row.get("hyp_category", ""),
             row.get("hyp_confidence", 0), row.get("classified", False)))
        return result[0]

    def update_hit(self, row_id: int, iso: str):
        """@brief Increment hit counter and update last_seen."""
        self._exec(
            "UPDATE signals SET last_seen=%s,hits=hits+1 WHERE id=%s", (iso, row_id))

    def update_analysis(self, row_id: int, row: dict):
        """@brief Write classification results onto an existing row."""
        self._exec(
            "UPDATE signals SET last_seen=%s,snr_db=%s,"
            "bandwidth_hz=COALESCE(NULLIF(bandwidth_hz,0),%s),"
            "modulation=%s,mod_class=%s,is_ofdm=%s,is_burst=%s,is_fhss=%s,"
            "hypothesis=%s,hyp_category=%s,hyp_confidence=%s,"
            "classified=%s,hits=hits+1 WHERE id=%s",
            (row["iso"], row.get("snr_db"), row.get("bw"),
             row.get("mod", ""), row.get("mod_class", "unclassified"),
             row.get("is_ofdm", False), row.get("is_burst", False),
             row.get("is_fhss", False),
             row.get("hypothesis", ""), row.get("hyp_category", ""),
             row.get("hyp_confidence", 0),
             row.get("classified", False),
             row_id))

    def count(self) -> int:
        result = self._exec("SELECT COUNT(*) FROM signals")
        return result[0] if result else 0

    def close(self):
        self._conn.close()


class _DualBackend:
    """@brief Fan-out backend: writes to two backends simultaneously.

    The primary backend (PostgreSQL) drives the return values.
    Errors from the secondary (SQLite) are printed to stderr but do not
    propagate — the primary write still succeeds.
    """

    def __init__(self, primary, secondary):
        """@brief Construct with a primary and secondary backend.
        @param primary    Primary backend (_PgBackend).  Return values come from here.
        @param secondary  Secondary backend (_SqliteBackend).  Errors are non-fatal.
        """
        self._p = primary
        self._s = secondary

    def insert(self, row: dict) -> int:
        try:
            self._s.insert(row)
        except Exception as e:
            print(f"[signal_logger] secondary insert error: {e}", file=sys.stderr)
        return self._p.insert(row)

    def insert_classified(self, row: dict) -> int:
        try:
            self._s.insert_classified(row)
        except Exception as e:
            print(f"[signal_logger] secondary insert_classified error: {e}", file=sys.stderr)
        return self._p.insert_classified(row)

    def update_hit(self, row_id: int, iso: str):
        try:
            self._s.update_hit(row_id, iso)
        except Exception as e:
            print(f"[signal_logger] secondary update_hit error: {e}", file=sys.stderr)
        self._p.update_hit(row_id, iso)

    def update_analysis(self, row_id: int, row: dict):
        try:
            self._s.update_analysis(row_id, row)
        except Exception as e:
            print(f"[signal_logger] secondary update_analysis error: {e}", file=sys.stderr)
        self._p.update_analysis(row_id, row)

    def count(self) -> int:
        return self._p.count()

    def close(self):
        self._p.close()
        try:
            self._s.close()
        except Exception:
            pass


def now_iso() -> str:
    """@brief Return the current UTC time as an ISO 8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ms() -> int:
    """@brief Return the current UTC time as Unix epoch milliseconds."""
    return int(time.time() * 1000)


# ── AMQP handler ──────────────────────────────────────────────────────────────

class _Handler(proton.handlers.MessagingHandler):
    """@brief Proton MessagingHandler — subscribes to rf.detections + rf.analysis.

    Maintains an in-memory dedup table keyed by 100 kHz frequency buckets.
    Each bucket stores (freq_hz, timestamp_ms, row_id); entries older than
    DEDUP_SEC are evicted on a 5-second timer.
    """

    def __init__(self, broker: str, creds: tuple[str, str],
                 backend, stop_ev: threading.Event):
        """@brief Construct the handler.
        @param broker   AMQP broker URL.
        @param creds    (username, password) tuple.
        @param backend  Database backend (_SqliteBackend, _PgBackend, or _DualBackend).
        @param stop_ev  threading.Event — set to request graceful shutdown.
        """
        super().__init__()
        self._broker  = broker
        self._user, self._pw = creds
        self._backend = backend
        self._stop_ev = stop_ev
        # Detection dedup: _det[(freq_bucket, bw_bucket)] = (freq_hz, bw_hz, ts_ms, row_id)
        self._det: dict[tuple, tuple] = {}
        # Analysis dedup: _ana[(freq_bucket, modulation)] = (freq_hz, ts_ms, row_id)
        # Different modulation at same freq → different entry → separate row.
        self._ana: dict[tuple, tuple] = {}

    def on_start(self, ev):
        """@brief Connect to the broker and subscribe to both detection topics."""
        c = ev.container.connect(
            self._broker, user=self._user, password=self._pw,
            sasl_enabled=True, allowed_mechs="PLAIN",
        )
        ev.container.create_receiver(c, "rf.detections")
        ev.container.create_receiver(c, "rf.analysis")
        ev.container.schedule(5.0, self)

    def on_timer_task(self, ev):
        """@brief Periodic tick: check stop event and evict stale dedup entries."""
        if self._stop_ev.is_set():
            ev.container.stop()
            return
        ev.container.schedule(5.0, self)
        self._flush_old()

    def on_message(self, ev):
        """@brief Dispatch an inbound AMQP message to the appropriate handler."""
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
        """@brief Persist one RF_DETECTION message (insert or hit-increment).
        @param msg  Decoded JSON body of an RF_DETECTION AMQP message.
        """
        freq  = float(msg.get("center_freq_hz", 0))
        bw    = float(msg.get("bandwidth_hz", 0))
        power = float(msg.get("power_db", 0))
        sid   = str(msg.get("scanner_id", ""))
        ts_ms = int(msg.get("timestamp_ms", now_ms()))

        if freq <= 0 or bw <= 0 or not (-150 < power < 20):
            return

        iso    = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        row_id = self._find_det(freq, bw, ts_ms)

        if row_id:
            self._backend.update_hit(row_id, iso)
        else:
            row_id = self._backend.insert({
                "iso": iso, "freq_hz": freq, "freq_mhz": freq / 1e6,
                "bw": bw, "power_db": power, "scanner_id": sid,
            })
            self._store_det(freq, bw, ts_ms, row_id)

    # ── Analysis result (from AnalysisApp) ────────────────────────────────────

    def _handle_analysis(self, msg: dict) -> None:
        """@brief Write modulation classification from an ANALYSIS_RESULT message.

        Identity logic mirrors AnalysisApp::persistResult():
        - Same (freq, modulation) → update existing row (last_seen, hypothesis, etc.)
        - Same freq, different modulation → new row (different signal type)
        - No prior detection row → insert classified row directly

        @param msg  Decoded JSON body of an ANALYSIS_RESULT AMQP message.
        """
        freq  = float(msg.get("center_freq_hz", 0))
        bw    = float(msg.get("bandwidth_hz", 0))
        snr   = float(msg.get("snr_db", 0))
        cls   = bool(msg.get("classified", False))
        ts_ms = int(msg.get("timestamp_ms", now_ms()))
        sid   = str(msg.get("scanner_id", ""))

        if freq <= 0:
            return

        m = msg.get("modulation", {})
        mod_str, mod_class = "", "unclassified"
        if m.get("analog"):
            mod_str, mod_class = m["analog"], "analog"
        elif m.get("digital"):
            mod_str = m["digital"] + (f"-{m['m_ary']}" if m.get("m_ary", 0) > 1 else "")
            mod_class = "digital"

        ch  = msg.get("channel_structure", {})
        hyp = msg.get("hypotheses", [{}])[0] if msg.get("hypotheses") else {}
        iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")

        analysis_row = {
            "iso": iso, "snr_db": snr, "bw": bw if bw > 0 else None,
            "mod": mod_str, "mod_class": mod_class,
            "is_ofdm":  bool(m.get("is_ofdm")  or ch.get("is_ofdm")),
            "is_burst": bool(ch.get("is_burst")),
            "is_fhss":  bool(ch.get("is_fhss")),
            "hypothesis":     hyp.get("system", ""),
            "hyp_category":   hyp.get("category", ""),
            "hyp_confidence": hyp.get("confidence", 0),
            "classified": cls,
        }

        # Step 1: same (freq, modulation) seen before → update last_seen
        row_id = self._find_ana(freq, mod_str, ts_ms)
        if row_id:
            self._backend.update_analysis(row_id, analysis_row)
            return

        # Step 2: unclassified detection row at this freq+bw → promote it
        row_id = self._find_det(freq, bw, ts_ms)
        if row_id:
            self._backend.update_analysis(row_id, analysis_row)
            self._store_ana(freq, mod_str, ts_ms, row_id)
            return

        # Step 3: no prior row → insert classified signal directly
        row_id = self._backend.insert_classified({
            **analysis_row,
            "freq_hz": freq, "freq_mhz": freq / 1e6, "scanner_id": sid,
        })
        self._store_ana(freq, mod_str, ts_ms, row_id)

    # ── Dedup helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _freq_bucket(freq: float) -> int:
        """@brief Map a frequency to a DEDUP_HZ-wide bucket index."""
        return int(freq / DEDUP_HZ)

    @staticmethod
    def _bw_bucket(bw: float) -> int:
        """@brief Map a bandwidth to a coarse bucket (factor-of-2 grouping)."""
        if bw <= 0:
            return 0
        import math
        return int(math.log2(max(bw, 1000)) * 2)  # ~half-octave buckets

    def _find_det(self, freq: float, bw: float, ts_ms: int) -> int | None:
        """@brief Find a recent unclassified detection at this freq+BW.
        @return Row ID or None.
        """
        fb = self._freq_bucket(freq)
        bb = self._bw_bucket(bw)
        for df in (-1, 0, 1):
            for db_ in (-1, 0, 1):
                entry = self._det.get((fb + df, bb + db_))
                if not entry:
                    continue
                ef, ebw, ets, erid = entry
                if (abs(freq - ef) < DEDUP_HZ
                        and (bw <= 0 or ebw <= 0 or abs(bw - ebw) / max(ebw, 1000) < 0.5)
                        and (ts_ms - ets) < DEDUP_SEC * 1000):
                    return erid
        return None

    def _store_det(self, freq: float, bw: float, ts_ms: int, row_id: int) -> None:
        """@brief Cache a detection row for future freq+BW dedup lookups."""
        self._det[(self._freq_bucket(freq), self._bw_bucket(bw))] = (freq, bw, ts_ms, row_id)

    def _find_ana(self, freq: float, mod: str, ts_ms: int) -> int | None:
        """@brief Find a recent classified row with the same (freq, modulation).
        @return Row ID or None.
        """
        fb = self._freq_bucket(freq)
        for df in (-1, 0, 1):
            entry = self._ana.get((fb + df, mod))
            if not entry:
                continue
            ef, ets, erid = entry
            if abs(freq - ef) < DEDUP_HZ and (ts_ms - ets) < DEDUP_SEC * 1000:
                return erid
        return None

    def _store_ana(self, freq: float, mod: str, ts_ms: int, row_id: int) -> None:
        """@brief Cache a classified row by (freq, modulation) for future lookups."""
        self._ana[(self._freq_bucket(freq), mod)] = (freq, ts_ms, row_id)

    def _flush_old(self) -> None:
        """@brief Evict dedup entries older than DEDUP_SEC seconds."""
        cutoff = now_ms() - DEDUP_SEC * 1000
        self._det = {k: v for k, v in self._det.items() if v[2] >= cutoff}
        self._ana = {k: v for k, v in self._ana.items() if v[1] >= cutoff}


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """@brief Entry point: parse CLI args, open backend, and run the AMQP event loop."""
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

    sqlite_backend = _SqliteBackend(args.db)
    print(f"[signal_logger] Backend: SQLite {args.db}")

    if args.pg_host:
        try:
            pg_backend = _PgBackend(args.pg_host, args.pg_port, args.pg_db,
                                    args.pg_user, args.pg_pass)
            print(f"[signal_logger] Backend: PostgreSQL {args.pg_host}/{args.pg_db} (dual-write)")
            backend = _DualBackend(primary=pg_backend, secondary=sqlite_backend)
        except RuntimeError as e:
            print(f"[signal_logger] WARNING: {e} — falling back to SQLite only",
                  file=sys.stderr)
            backend = sqlite_backend
    else:
        backend = sqlite_backend

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

# ========================================================================
# End of file — OpenRFStack
# Subject to Personal Use License
# https://github.com/OpenRFStack
# ========================================================================
