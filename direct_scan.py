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
@file direct_scan.py
@brief Standalone PlutoSDR sweep scanner via libiio ctypes (no AMQP / SdrResourceManager).

Tunes the AD9361 across 80–3000 MHz, captures IQ, runs FFT,
detects peaks above noise floor, writes results to SQLite.

Usage:
    python3 direct_scan.py [--start 80] [--stop 3000] [--db signals.db]
                           [--gain 50] [--passes 2]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import math
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
from scipy import signal as scipy_signal

PLUTO_IP     = "192.168.1.253"
SAMPLE_RATE  = 20_000_000      # 20 MSPS
USABLE_BW    = 0.78            # fraction of sample_rate that's clean
BUF_SAMPLES  = 131_072         # ~6.5 ms at 20 MSPS
SETTLE_S     = 0.8             # wait after tuning before capture
FFT_SIZE     = 8192
THRESHOLD_DB = 8.0             # dB above median noise floor
MIN_BW_HZ    = 8_000           # ignore narrower blobs

# ── libiio bindings ───────────────────────────────────────────────────────────

_lib = ctypes.CDLL("libiio.so.1")

def _setup_api():
    """@brief Wire ctypes argtypes/restype for every libiio function used by PlutoScanner."""
    p = ctypes.c_void_p
    ll = ctypes.c_longlong
    sz = ctypes.c_size_t
    s  = ctypes.c_ssize_t
    i  = ctypes.c_int
    b  = ctypes.c_bool
    cs = ctypes.c_char_p

    _lib.iio_create_network_context.restype  = p
    _lib.iio_create_network_context.argtypes = [cs]
    _lib.iio_context_destroy.restype  = None
    _lib.iio_context_destroy.argtypes = [p]

    _lib.iio_context_find_device.restype  = p
    _lib.iio_context_find_device.argtypes = [p, cs]

    _lib.iio_device_find_channel.restype  = p
    _lib.iio_device_find_channel.argtypes = [p, cs, b]

    _lib.iio_channel_enable.restype  = None
    _lib.iio_channel_enable.argtypes = [p]
    _lib.iio_channel_disable.restype  = None
    _lib.iio_channel_disable.argtypes = [p]

    _lib.iio_channel_attr_write_longlong.restype  = i
    _lib.iio_channel_attr_write_longlong.argtypes = [p, cs, ll]
    _lib.iio_channel_attr_write_double.restype  = i
    _lib.iio_channel_attr_write_double.argtypes  = [p, cs, ctypes.c_double]
    _lib.iio_channel_attr_write.restype  = i
    _lib.iio_channel_attr_write.argtypes = [p, cs, cs]

    _lib.iio_device_create_buffer.restype  = p
    _lib.iio_device_create_buffer.argtypes = [p, sz, b]
    _lib.iio_buffer_destroy.restype  = None
    _lib.iio_buffer_destroy.argtypes = [p]
    _lib.iio_buffer_refill.restype  = s
    _lib.iio_buffer_refill.argtypes = [p]
    _lib.iio_buffer_first.restype  = ctypes.c_void_p
    _lib.iio_buffer_first.argtypes = [p, p]
    _lib.iio_buffer_step.restype  = s
    _lib.iio_buffer_step.argtypes = [p]
    _lib.iio_buffer_end.restype  = ctypes.c_void_p
    _lib.iio_buffer_end.argtypes = [p]

_setup_api()


class PlutoScanner:
    """@brief Direct PlutoSDR IQ capture using libiio over the network context.

    Connects to the PlutoSDR at @p ip, configures the AD9361 RF front end,
    and exposes tune() + capture_iq() for stepped frequency sweeps.
    No SoapySDR or SdrResourceManager dependency — use for offline testing
    or when the full stack is not available.
    """

    def __init__(self, ip: str, gain_db: int):
        """@brief Connect to the PlutoSDR and configure the AD9361.
        @param ip       IP address of the PlutoSDR (e.g. "192.168.1.253").
        @param gain_db  RX hardware gain in dB (manual mode).
        @throws RuntimeError if the device cannot be reached or channels not found.
        """
        self._ctx = _lib.iio_create_network_context(ip.encode())
        if not self._ctx:
            raise RuntimeError(f"Cannot connect to PlutoSDR at {ip}")

        self._phy    = _lib.iio_context_find_device(self._ctx, b"ad9361-phy")
        self._rx_dev = _lib.iio_context_find_device(self._ctx, b"cf-ad9361-lpc")
        if not self._phy or not self._rx_dev:
            raise RuntimeError("Cannot find ad9361-phy or cf-ad9361-lpc")

        self._rx_lo  = _lib.iio_device_find_channel(self._phy, b"altvoltage0", False)
        self._rx_phy = _lib.iio_device_find_channel(self._phy, b"voltage0", False)
        self._i_ch   = _lib.iio_device_find_channel(self._rx_dev, b"voltage0", False)
        self._q_ch   = _lib.iio_device_find_channel(self._rx_dev, b"voltage1", False)

        # Configure radio
        _lib.iio_channel_attr_write_longlong(self._rx_phy, b"rf_bandwidth",        int(SAMPLE_RATE * 0.9))
        _lib.iio_channel_attr_write_longlong(self._rx_phy, b"sampling_frequency",  int(SAMPLE_RATE))
        _lib.iio_channel_attr_write(self._rx_phy, b"gain_control_mode", b"manual")
        _lib.iio_channel_attr_write_longlong(self._rx_phy, b"hardwaregain",        int(gain_db))

        _lib.iio_channel_enable(self._i_ch)
        _lib.iio_channel_enable(self._q_ch)

        self._buf = _lib.iio_device_create_buffer(self._rx_dev, BUF_SAMPLES, False)
        if not self._buf:
            raise RuntimeError("Cannot create IQ buffer")

    def tune(self, freq_hz: int):
        """@brief Retune the AD9361 LO and wait for the settle time.
        @param freq_hz  Target LO frequency (Hz).
        """
        _lib.iio_channel_attr_write_longlong(self._rx_lo, b"frequency", freq_hz)
        time.sleep(SETTLE_S)

    def capture_iq(self) -> np.ndarray:
        """@brief Refill the IQ buffer and return samples as complex64.
        @return  numpy array of shape (BUF_SAMPLES,) dtype complex64, or empty array on error.
        """
        ret = _lib.iio_buffer_refill(self._buf)
        if ret < 0:
            return np.array([], dtype=np.complex64)

        start = _lib.iio_buffer_first(self._buf, self._i_ch)
        end   = _lib.iio_buffer_end(self._buf)
        step  = _lib.iio_buffer_step(self._buf)

        nbytes = end - start
        n = nbytes // step
        raw = (ctypes.c_int16 * (n * 2)).from_address(start)
        iq = np.frombuffer(raw, dtype=np.int16).reshape(n, 2).astype(np.float32)
        return (iq[:, 0] + 1j * iq[:, 1]).astype(np.complex64)

    def close(self):
        """@brief Destroy the IQ buffer and free the libiio context."""
        if self._buf:
            _lib.iio_buffer_destroy(self._buf)
        if self._ctx:
            _lib.iio_context_destroy(self._ctx)


# ── Signal detection ──────────────────────────────────────────────────────────

def detect_peaks(iq: np.ndarray, center_hz: int) -> list[dict]:
    """@brief Detect signal peaks in an IQ block using Welch PSD and median thresholding.
    @param iq         Complex64 IQ samples from PlutoScanner.capture_iq().
    @param center_hz  LO centre frequency used when the samples were captured (Hz).
    @return           List of dicts with keys: freq_hz, bandwidth_hz, power_db, snr_db.
                      Empty list when fewer than FFT_SIZE samples provided.
    """
    if len(iq) < FFT_SIZE:
        return []

    # Welch PSD for noise-robust estimate
    freqs, psd = scipy_signal.welch(iq, fs=SAMPLE_RATE,
                                    nperseg=FFT_SIZE, noverlap=FFT_SIZE//2,
                                    return_onesided=False)
    freqs = np.fft.fftshift(freqs)
    psd   = np.fft.fftshift(psd)
    psd_db = 10 * np.log10(psd + 1e-20)

    noise_floor = np.percentile(psd_db, 50)
    threshold   = noise_floor + THRESHOLD_DB

    above = psd_db > threshold
    # Label connected regions
    changes = np.diff(above.astype(int))
    starts  = np.where(changes == 1)[0] + 1
    ends    = np.where(changes == -1)[0] + 1
    if above[0]:  starts = np.r_[0, starts]
    if above[-1]: ends   = np.r_[ends, len(above)]

    usable_hz = SAMPLE_RATE * USABLE_BW
    results = []
    for s, e in zip(starts, ends):
        seg_freqs = freqs[s:e]
        seg_psd   = psd_db[s:e]
        bw_hz     = float(seg_freqs[-1] - seg_freqs[0])
        if bw_hz < MIN_BW_HZ:
            continue
        # Skip edges (outside usable bandwidth)
        peak_rel = float(seg_freqs[np.argmax(seg_psd)])
        if abs(peak_rel) > usable_hz / 2:
            continue
        peak_hz = center_hz + peak_rel
        if peak_hz < 70e6 or peak_hz > 6e9:
            continue
        peak_db = float(seg_psd.max()) - 30  # rough dBm offset
        results.append({
            "freq_hz": peak_hz,
            "bandwidth_hz": bw_hz,
            "power_db": peak_db,
            "snr_db": float(seg_psd.max()) - noise_floor,
        })
    return results


# ── Database ──────────────────────────────────────────────────────────────────

SCHEMA = """
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
    scanner_id   TEXT    DEFAULT 'direct'
);
CREATE INDEX IF NOT EXISTS idx_freq_time ON signals (freq_hz, timestamp_ms DESC);
"""

DEDUP_HZ  = 100_000
DEDUP_SEC = 600


def open_db(path: str) -> sqlite3.Connection:
    """@brief Open (or create) the signals SQLite database and apply the schema.
    @param path  File path for the SQLite database.
    @return      Open sqlite3.Connection in WAL mode.
    """
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


def upsert_signal(db: sqlite3.Connection, sig: dict, recent: dict) -> dict:
    """@brief Insert a new signal or increment the hit counter for a recent duplicate.
    @param db      Open database connection.
    @param sig     Signal dict with keys: freq_hz, bandwidth_hz, power_db, snr_db.
    @param recent  In-memory dedup cache: {freq_hz: (timestamp_ms, row_id)}.
    @return        Updated @p recent dict.
    """
    freq  = sig["freq_hz"]
    ts_ms = int(time.time() * 1000)
    iso   = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for known_freq, (known_ts, row_id) in list(recent.items()):
        if abs(freq - known_freq) < DEDUP_HZ and (ts_ms - known_ts) < DEDUP_SEC * 1000:
            db.execute("UPDATE signals SET last_seen=?, hits=hits+1, snr_db=MAX(snr_db,?) WHERE id=?",
                       (iso, sig["snr_db"], row_id))
            db.commit()
            return recent

    cur = db.execute(
        "INSERT INTO signals (first_seen,last_seen,timestamp_ms,freq_hz,freq_mhz,bandwidth_hz,power_db,snr_db) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (iso, iso, ts_ms, freq, freq/1e6, sig["bandwidth_hz"], sig["power_db"], sig["snr_db"]),
    )
    db.commit()
    recent[freq] = (ts_ms, cur.lastrowid)
    return recent


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """@brief Entry point: parse arguments and run the PlutoSDR sweep loop."""
    ap = argparse.ArgumentParser(description="Direct PlutoSDR sweep scanner")
    ap.add_argument("--start",  type=int, default=80,   help="Start MHz")
    ap.add_argument("--stop",   type=int, default=3000, help="Stop MHz")
    ap.add_argument("--db",     default="signals.db")
    ap.add_argument("--gain",   type=int, default=50,   help="RX gain dB")
    ap.add_argument("--passes", type=int, default=2,    help="Number of full sweeps")
    args = ap.parse_args()

    step_hz   = int(SAMPLE_RATE * USABLE_BW)
    start_hz  = args.start * 1_000_000
    stop_hz   = args.stop  * 1_000_000
    centers   = list(range(start_hz + step_hz // 2, stop_hz, step_hz))
    total     = len(centers) * args.passes

    print(f"[direct_scan] PlutoSDR at {PLUTO_IP}")
    print(f"[direct_scan] Range: {args.start}–{args.stop} MHz  ({len(centers)} steps × {args.passes} passes)")
    print(f"[direct_scan] Step: {step_hz/1e6:.1f} MHz  |  DB: {args.db}")

    db      = open_db(args.db)
    recent: dict = {}
    scanner = PlutoScanner(PLUTO_IP, args.gain)

    done = 0
    try:
        for pass_n in range(args.passes):
            print(f"\n[direct_scan] === Pass {pass_n+1}/{args.passes} ===")
            for center in centers:
                scanner.tune(center)
                iq = scanner.capture_iq()
                peaks = detect_peaks(iq, center)
                for p in peaks:
                    recent = upsert_signal(db, p, recent)
                done += 1
                pct = done / total * 100
                cnt = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
                print(f"  {center/1e6:7.1f} MHz  {len(peaks):2d} signals  ({pct:.0f}%  total={cnt})", end="\r")
    except KeyboardInterrupt:
        print("\n[direct_scan] Stopped by user")
    finally:
        scanner.close()

    count = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    print(f"\n[direct_scan] Done — {count} signals in DB")
    db.close()


if __name__ == "__main__":
    main()

# ========================================================================
# End of file — OpenRFStack
# Subject to Personal Use License
# https://github.com/OpenRFStack
# ========================================================================
