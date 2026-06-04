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
Hardware test: exercise every task type supported by SdrResourceManager.

Task types tested:
  1. NARROWBAND  — doAcceptStandard, streams IQ to UDP port
  2. WIDEBAND    — doAcceptStandard, streams IQ to UDP port
  3. TRIGGERED   — doAcceptStandard + TriggerMonitor, power-threshold capture
  4. SNAPSHOT    — doAcceptSnapshot, synchronous spectrum FFT, no streaming
  5. CALIBRATION — doAcceptCalibration, multi-channel IQ streaming
  6. SCHEDULED   — NARROWBAND with future start_time (schedulerTick activates it)
  7. SCAN        — doAcceptScan (AcquisitionApp already verified; we send one shot)
  8. DF          — doAcceptStandard with df_params (may reject on single-chan hardware)

Usage (from the host):
    podman run --rm --network=host \\
        -v /home/brendan/hw-test/test_all_task_types.py:/test.py:ro,z \\
        sdr-controller:test-integ python3 /test.py [--broker amqp://localhost:5672]
"""
from __future__ import annotations
import argparse, json, socket, struct, sys, threading, time, uuid
import proton, proton.handlers, proton.reactor

BROKER   = "amqp://localhost:5672"
REQ_Q    = "sdr.task.request"
RESP_Q   = "sdr.task.response"
CREDS    = ("sdr_ctrl", "sdr_hw_test")
CF_HZ    = 476.5e6   # a frequency the PlutoSDR can tune to
BW_HZ    = 2e6
SR_SPS   = 2e6
DEST_IP  = "127.0.0.1"

IQ_HEADER = struct.Struct("<I I Q Q I H B B")  # matches sdr::IqPacketHeader
IQ_MAGIC  = 0x49515030

# ── RPC session ────────────────────────────────────────────────────────────────

class _Handler(proton.handlers.MessagingHandler):
    def __init__(self, broker, session):
        super().__init__()
        self._broker = broker
        self._s: _Session = session

    def on_start(self, event):
        conn = event.container.connect(
            self._broker, user=CREDS[0], password=CREDS[1],
            sasl_enabled=True, allowed_mechs="PLAIN")
        event.container.create_receiver(conn, RESP_Q)
        self._sender = event.container.create_sender(conn, REQ_Q)
        self._s._handler = self

    def on_sendable(self, event):
        self._s._ready.set()

    def on_message(self, event):
        try:
            body = event.message.body
            msg  = json.loads(body if isinstance(body, str) else body.decode())
        except Exception:
            return
        req_id = msg.get("request_id", "")
        with self._s._lock:
            entry = self._s._pending.get(req_id)
        if entry:
            ev, box = entry
            box.append(msg)
            ev.set()

    def on_transport_error(self, event):
        self._s.error = str(event.transport.condition)
        self._s._ready.set()

    def send(self, body: dict):
        msg = proton.Message(body=json.dumps(body), content_type="application/json")
        self._sender.send(msg)


class _Session:
    def __init__(self, broker):
        self._broker = broker
        self._pending: dict[str, tuple] = {}
        self._lock    = threading.Lock()
        self._ready   = threading.Event()
        self._handler = None
        self.error    = None
        self._container = proton.reactor.Container(_Handler(broker, self))
        self._thread    = threading.Thread(target=self._container.run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)

    def rpc(self, req: dict, timeout_s: float = 20.0) -> dict | None:
        req_id = req.get("request_id", "")
        ev = threading.Event(); box: list = []
        with self._lock:
            self._pending[req_id] = (ev, box)
        self._handler.send(req)
        ev.wait(timeout=timeout_s)
        with self._lock:
            self._pending.pop(req_id, None)
        return box[0] if box else None

    def fire(self, req: dict):
        """Send without waiting for a response."""
        self._handler.send(req)

    def close(self):
        try: self._container.stop()
        except Exception: pass
        self._thread.join(timeout=3)


# ── UDP helpers ────────────────────────────────────────────────────────────────

def _bind_udp(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    s.settimeout(10.0)
    s.bind(("", port))
    return s

def _count_iq_packets(port: int, want_samples: int, timeout_s: float = 15.0) -> int:
    """Return number of valid IQ packets received until we hit want_samples."""
    sock = _bind_udp(port)
    total = 0; pkts = 0
    deadline = time.time() + timeout_s
    try:
        while total < want_samples and time.time() < deadline:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if len(data) < IQ_HEADER.size:
                continue
            magic = IQ_HEADER.unpack_from(data)[0]
            if magic != IQ_MAGIC:
                continue
            n_samp = IQ_HEADER.unpack_from(data)[5]
            total += n_samp; pkts += 1
    finally:
        sock.close()
    return pkts


def _mk_id(): return str(uuid.uuid4())
def _now_ms(): return int(time.time() * 1000)

def _stop(sess: _Session, task_id: str, req_id: str | None = None):
    sess.rpc({
        "msg_type":    "TASK_STOP",
        "request_id":  req_id or _mk_id(),
        "task_id":     task_id,
        "timestamp_ms": _now_ms(),
        "reason":      "test complete",
    }, timeout_s=10)


# ══════════════════════════════════════════════════════════════════════════════
# Individual task-type tests
# ══════════════════════════════════════════════════════════════════════════════

def test_narrowband(sess: _Session) -> bool:
    print("\n── NARROWBAND ──────────────────────────────────────────────────")
    req_id = _mk_id()
    resp = sess.rpc({
        "msg_type":        "TASK_REQUEST",
        "schema_version":  "2.0",
        "request_id":      req_id,
        "timestamp_ms":    _now_ms(),
        "task_type":       "NARROWBAND",
        "rank":            2,
        "schedule":        {"mode": "IMMEDIATE", "duration_ms": 3000},
        "rf":              {"center_freq_hz": CF_HZ, "bandwidth_hz": BW_HZ,
                            "sample_rate_sps": SR_SPS, "rx_count": 1},
        "streaming":       {"dest_ip": DEST_IP},
        "narrowband":      {"demod": "FM"},
    }, timeout_s=20)

    if not resp:
        print("  ✗ No response"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')} task_id={resp.get('task_id','?')[:8]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason',resp.get('status','?'))}"); return False

    streams = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    print(f"  udp_port={udp_port}")
    if not udp_port:
        print("  ✗ no udp_port in response"); return False

    pkts = _count_iq_packets(udp_port, want_samples=10000, timeout_s=8)
    _stop(sess, resp["task_id"])
    ok = pkts > 0
    print(f"  {'✓' if ok else '✗'} received {pkts} IQ packets")
    return ok


def test_wideband(sess: _Session) -> bool:
    print("\n── WIDEBAND ────────────────────────────────────────────────────")
    req_id = _mk_id()
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   _now_ms(),
        "task_type":      "WIDEBAND",
        "rank":           2,
        "schedule":       {"mode": "IMMEDIATE", "duration_ms": 3000},
        "rf":             {"center_freq_hz": CF_HZ, "bandwidth_hz": BW_HZ,
                           "sample_rate_sps": SR_SPS, "rx_count": 1},
        "streaming":      {"dest_ip": DEST_IP},
        "wideband":       {"record_raw_iq": True, "fft_size": 2048},
    }, timeout_s=20)

    if not resp:
        print("  ✗ No response"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')} task_id={resp.get('task_id','?')[:8]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason', resp.get('status','?'))}"); return False

    streams = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    if not udp_port:
        print("  ✗ no udp_port"); return False

    pkts = _count_iq_packets(udp_port, want_samples=10000, timeout_s=8)
    _stop(sess, resp["task_id"])
    ok = pkts > 0
    print(f"  {'✓' if ok else '✗'} received {pkts} IQ packets (port={udp_port})")
    return ok


def test_triggered(sess: _Session) -> bool:
    print("\n── TRIGGERED ───────────────────────────────────────────────────")
    req_id = _mk_id()
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   _now_ms(),
        "task_type":      "TRIGGERED",
        "rank":           2,
        "schedule":       {"mode": "IMMEDIATE", "duration_ms": 5000},
        "rf":             {"center_freq_hz": CF_HZ, "bandwidth_hz": BW_HZ,
                           "sample_rate_sps": SR_SPS, "rx_count": 1},
        "streaming":      {"dest_ip": DEST_IP},
        "trigger":        {"trigger_type": "POWER_THRESHOLD",
                           "threshold_dbfs": -90.0,   # very low → triggers immediately
                           "pre_trigger_ms": 10, "post_trigger_ms": 100,
                           "max_captures": 1},
    }, timeout_s=20)

    if not resp:
        print("  ✗ No response"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')} task_id={resp.get('task_id','?')[:8]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason', resp.get('status','?'))}"); return False

    streams = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    if not udp_port:
        print("  ✗ no udp_port"); return False

    # With threshold=-90dBFS the trigger fires immediately on any real signal
    pkts = _count_iq_packets(udp_port, want_samples=5000, timeout_s=10)
    _stop(sess, resp["task_id"])
    ok = pkts > 0
    print(f"  {'✓' if ok else '✗'} received {pkts} trigger-capture IQ packets (port={udp_port})")
    return ok


def test_snapshot(sess: _Session) -> bool:
    print("\n── SNAPSHOT ────────────────────────────────────────────────────")
    req_id = _mk_id()
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST_SNAPSHOT",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   _now_ms(),
        "task_type":      "SNAPSHOT",
        "rank":           2,
        "snapshot":       {"center_freq_hz": CF_HZ, "bandwidth_hz": BW_HZ,
                           "sample_rate_sps": SR_SPS, "fft_size": 1024,
                           "n_averages": 4},
    }, timeout_s=30)

    if not resp:
        print("  ✗ No response (timeout)"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    task_id  = resp.get("task_id", "")
    print(f"  status={resp.get('status','?')} task_id={task_id[:20]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason', resp.get('status','?'))}"); return False
    ok = task_id.startswith("snapshot-") or bool(task_id)
    print(f"  {'✓' if ok else '✗'} snapshot returned task_id={task_id[:20]!r}")
    return ok


def test_calibration(sess: _Session) -> bool:
    print("\n── CALIBRATION (multi-ch, single board) ────────────────────────")
    req_id = _mk_id()

    # Probe how many RX channels the hardware actually has.
    # The controller clamps rx_channels to the hardware's reported count,
    # so request max=2 and fall back to 1 if rejected.
    for n_ch in (2, 1):
        req_id = _mk_id()
        resp = sess.rpc({
            "msg_type":       "TASK_REQUEST_CALIBRATION",
            "schema_version": "2.0",
            "request_id":     req_id,
            "timestamp_ms":   _now_ms(),
            "task_type":      "CALIBRATION",
            "rank":           2,
            "calibration": {
                "center_freq_hz":      CF_HZ,
                "bandwidth_hz":        BW_HZ,
                "sample_rate_sps":     SR_SPS,
                "duration_ms":         3000,
                "rx_count_per_device": n_ch,
                "coherency_group":     "",
                "devices":             ["pluto-0"],
            },
            "streaming": {"dest_ip": DEST_IP},
        }, timeout_s=20)
        if not resp:
            print("  ✗ No response"); return False
        if resp.get("status") == "ACCEPTED" or resp.get("accepted"):
            break
        print(f"  {n_ch}-ch rejected ({resp.get('reject_reason','')}), trying {n_ch-1}...")

    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')} task_id={resp.get('task_id','?')[:8]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason', resp.get('status','?'))}"); return False

    streams = resp.get("streams", [])
    n_streams = len(streams)
    print(f"  {n_streams} channel(s) on single board")

    # Receive IQ from all channels in parallel
    import threading
    results = {}
    def collect(port, ch):
        results[ch] = _count_iq_packets(port, want_samples=5000, timeout_s=5)

    threads = []
    for s in streams:
        t = threading.Thread(target=collect,
                             args=(s.get("udp_port",0), s.get("channel_index","?")))
        t.start(); threads.append(t)
    for t in threads: t.join()

    total = 0
    for ch, pkts in sorted(results.items()):
        ok = pkts > 0
        print(f"  ch{ch}: {pkts} packets {'✓' if ok else '✗'}")
        total += pkts
    return total > 0


def test_scheduled(sess: _Session) -> bool:
    print("\n── SCHEDULED (NARROWBAND, future start) ────────────────────────")
    req_id   = _mk_id()
    start_ms = _now_ms() + 5000   # 5 seconds from now
    stop_ms  = start_ms + 4000

    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   _now_ms(),
        "task_type":      "NARROWBAND",
        "rank":           2,
        "schedule":       {"mode": "SCHEDULED",
                           "start_time_epoch_ms": start_ms,
                           "end_time_epoch_ms":   stop_ms},
        "rf":             {"center_freq_hz": CF_HZ, "bandwidth_hz": BW_HZ,
                           "sample_rate_sps": SR_SPS, "rx_count": 1},
        "streaming":      {"dest_ip": DEST_IP},
    }, timeout_s=15)

    if not resp:
        print("  ✗ No response"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')} task_id={resp.get('task_id','?')[:8]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason', resp.get('status','?'))}"); return False

    streams  = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    task_id  = resp.get("task_id", "")
    print(f"  ACCEPTED with SCHEDULED mode, udp_port={udp_port}")
    print(f"  waiting for schedulerTick to activate at T+5s ...")

    # Wait until IQ flows (scheduler fires after start_ms)
    pkts = _count_iq_packets(udp_port, want_samples=5000, timeout_s=15)
    _stop(sess, task_id)
    ok = pkts > 0
    print(f"  {'✓' if ok else '✗'} received {pkts} IQ packets after scheduled start")
    return ok


def test_scan(sess: _Session) -> bool:
    print("\n── SCAN (one-shot, 3 entries) ──────────────────────────────────")
    req_id = _mk_id()
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST_SCAN",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   _now_ms(),
        "task_type":      "SCAN",
        "rank":           2,
        "schedule":       {"mode": "IMMEDIATE", "duration_ms": 6000},
        "rf":             {"center_freq_hz": CF_HZ, "bandwidth_hz": 10e6,
                           "sample_rate_sps": 10e6, "rx_count": 1},
        "streaming":      {"dest_ip": DEST_IP},
        "scan_params": {
            "repeat": False,
            "entries": [
                {"step": 0, "center_freq_hz": 460e6, "bandwidth_hz": 10e6,
                 "sample_rate_sps": 10e6, "dwell_ms": 500},
                {"step": 1, "center_freq_hz": 470e6, "bandwidth_hz": 10e6,
                 "sample_rate_sps": 10e6, "dwell_ms": 500},
                {"step": 2, "center_freq_hz": 480e6, "bandwidth_hz": 10e6,
                 "sample_rate_sps": 10e6, "dwell_ms": 500},
            ],
        },
    }, timeout_s=20)

    if not resp:
        print("  ✗ No response"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')} task_id={resp.get('task_id','?')[:8]}")
    if not accepted:
        print(f"  ✗ rejected: {resp.get('reject_reason', resp.get('status','?'))}"); return False

    streams  = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    pkts = _count_iq_packets(udp_port, want_samples=10000, timeout_s=20)
    ok = pkts > 0
    print(f"  {'✓' if ok else '✗'} received {pkts} IQ packets across scan entries (port={udp_port})")
    return ok


def test_df(sess: _Session) -> bool:
    print("\n── DF (direction-finding; may reject on single-chan hardware) ───")
    req_id = _mk_id()
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   _now_ms(),
        "task_type":      "DF",
        "rank":           2,
        "schedule":       {"mode": "IMMEDIATE", "duration_ms": 3000},
        "rf":             {"center_freq_hz": CF_HZ, "bandwidth_hz": BW_HZ,
                           "sample_rate_sps": SR_SPS, "rx_count": 2},  # needs 2 channels
        "streaming":      {"dest_ip": DEST_IP},
        "df_params":      {"algorithm": "MUSIC", "num_sources": 1,
                           "snapshot_count": 512, "angular_res_deg": 1.0},
    }, timeout_s=15)

    if not resp:
        print("  ✗ No response"); return False
    accepted = resp.get("status") == "ACCEPTED" or resp.get("accepted")
    print(f"  status={resp.get('status','?')}")
    if not accepted:
        reason = resp.get("reject_reason", resp.get("status", "?"))
        print(f"  ✗ rejected (expected on single-channel hardware): {reason}")
        return False  # expected failure — report but don't count as blocking

    streams  = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    pkts = _count_iq_packets(udp_port, want_samples=5000, timeout_s=8)
    _stop(sess, resp["task_id"])
    ok = pkts > 0
    print(f"  {'✓' if ok else '✗'} received {pkts} IQ packets on DF stream")
    return ok


# ── Runner ─────────────────────────────────────────────────────────────────────

ALL_TESTS = [
    ("NARROWBAND",  test_narrowband),
    ("WIDEBAND",    test_wideband),
    ("TRIGGERED",   test_triggered),
    ("SNAPSHOT",    test_snapshot),
    ("CALIBRATION", test_calibration),
    ("SCHEDULED",   test_scheduled),
    ("SCAN",        test_scan),
    ("DF",          test_df),
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default=BROKER)
    ap.add_argument("--test",   default=None, help="Run only this test")
    args = ap.parse_args()

    print(f"Connecting to {args.broker} ...")
    sess = _Session(args.broker)
    if sess.error:
        sys.exit(f"AMQP connect error: {sess.error}")
    print("Connected.\n")

    tests = [(n, f) for n, f in ALL_TESTS if args.test is None or n == args.test]
    results: dict[str, bool] = {}
    df_expected_fail = False

    for name, fn in tests:
        # Give the controller a moment between tests for hardware cleanup
        if results:
            time.sleep(4)
        try:
            results[name] = fn(sess)
        except Exception as ex:
            print(f"  EXCEPTION: {ex}")
            results[name] = False

    sess.close()

    print("\n── Summary ─────────────────────────────────────────────────────")
    all_ok = True
    for name, ok in results.items():
        note = " (single-channel hardware — expected)" if name == "DF" and not ok else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{note}")
        if not ok and name != "DF":
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

# ========================================================================
# End of file — OpenRFStack
# Subject to Personal Use License
# https://github.com/OpenRFStack
# ========================================================================
