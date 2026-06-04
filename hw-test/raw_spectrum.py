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
Raw spectrum snapshot at a single frequency.
Collects IQ, prints Welch PSD top-20 peaks, and shows a text waterfall.
No threshold filtering — shows everything, noise floor included.

Usage:
    PYTHONPATH=/tmp/proton_pkg python3 raw_spectrum.py --freq 98e6
    PYTHONPATH=/tmp/proton_pkg python3 raw_spectrum.py --freq 433.92e6 --gain 40
"""
from __future__ import annotations
import argparse, json, socket, struct, threading, time, uuid
import numpy as np
import proton, proton.handlers, proton.reactor

BROKER = "amqp://localhost:5672"
REQ_Q  = "sdr.task.request"
RESP_Q = "sdr.task.response"
CREDS  = ("sdr_ctrl", "sdr_hw_test")
DEST_IP = "127.0.0.1"
IQ_HDR  = struct.Struct("<I I Q Q I H B B")
IQ_MAGIC = 0x49515030


class _H(proton.handlers.MessagingHandler):
    def __init__(self, sess):
        super().__init__()
        self._s = sess
    def on_start(self, ev):
        c = ev.container.connect(BROKER, user=CREDS[0], password=CREDS[1],
                                 sasl_enabled=True, allowed_mechs="PLAIN")
        ev.container.create_receiver(c, RESP_Q)
        self._sender = ev.container.create_sender(c, REQ_Q)
        self._s._handler = self
    def on_sendable(self, ev): self._s._ready.set()
    def on_message(self, ev):
        try:
            msg = json.loads(ev.message.body)
        except Exception: return
        rid = msg.get("request_id", "")
        with self._s._lock:
            e = self._s._pending.get(rid)
        if e: e[1].append(msg); e[0].set()
    def send(self, d):
        self._sender.send(proton.Message(body=json.dumps(d),
                                          content_type="application/json"))

class Session:
    def __init__(self):
        self._pending = {}; self._lock = threading.Lock()
        self._ready = threading.Event(); self._handler = None
        self._ctr = proton.reactor.Container(_H(self))
        threading.Thread(target=self._ctr.run, daemon=True).start()
        self._ready.wait(10)
    def rpc(self, req, timeout=25):
        rid = req["request_id"]; ev = threading.Event(); box = []
        with self._lock: self._pending[rid] = (ev, box)
        self._handler.send(req)
        ev.wait(timeout)
        with self._lock: self._pending.pop(rid, None)
        return box[0] if box else None
    def fire(self, req): self._handler.send(req)
    def close(self):
        try: self._ctr.stop()
        except: pass


def collect(port, secs):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16*1024*1024)
    s.settimeout(2.0); s.bind(("", port))
    chunks = []; t = time.time() + secs
    try:
        while time.time() < t:
            try: data = s.recv(65536)
            except socket.timeout: break
            if len(data) < IQ_HDR.size: continue
            if IQ_HDR.unpack_from(data)[0] != IQ_MAGIC: continue
            n = IQ_HDR.unpack_from(data)[5]
            raw = np.frombuffer(data[IQ_HDR.size:IQ_HDR.size+n*8], dtype=np.float32)
            if len(raw) == n*2:
                chunks.append(raw[0::2] + 1j*raw[1::2])
    finally: s.close()
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.complex64)


def spectrum(iq, center_hz, sr, n_fft=8192):
    win  = np.blackman(n_fft)
    step = n_fft // 2
    nf   = max(1, (len(iq) - n_fft) // step)
    acc  = np.zeros(n_fft)
    for i in range(nf):
        seg = iq[i*step:i*step+n_fft] * win
        acc += np.abs(np.fft.fft(seg))**2
    acc  /= nf
    db    = np.fft.fftshift(10*np.log10(acc + 1e-30))
    freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1/sr)) + center_hz
    return freqs, db


def waterfall_row(db, width=60):
    lo, hi = np.percentile(db, 5), np.percentile(db, 99)
    span   = max(hi - lo, 1)
    chars  = " ._-=+*#@"
    row    = ""
    for x in np.interp(np.linspace(0, len(db)-1, width), np.arange(len(db)), db):
        idx = int((x - lo) / span * (len(chars)-1))
        row += chars[max(0, min(idx, len(chars)-1))]
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freq",  type=float, default=98e6,  help="Centre Hz (default 98e6)")
    ap.add_argument("--bw",    type=float, default=10e6,  help="Bandwidth Hz (default 10e6)")
    ap.add_argument("--sr",    type=float, default=10e6,  help="Sample rate sps (default 10e6)")
    ap.add_argument("--gain",  type=float, default=40.0,  help="RX gain dB (default 40)")
    ap.add_argument("--dwell", type=float, default=3.0,   help="Collect seconds (default 3)")
    ap.add_argument("--peaks", type=int,   default=20,    help="Top N peaks to show")
    args = ap.parse_args()

    sess = Session()
    rid  = str(uuid.uuid4())
    print(f"\nRequesting IQ at {args.freq/1e6:.3f} MHz, BW={args.bw/1e6:.0f} MHz, "
          f"gain={args.gain:.0f} dB, {args.dwell:.0f}s dwell …")

    resp = sess.rpc({
        "msg_type": "TASK_REQUEST", "schema_version": "2.0",
        "request_id": rid, "timestamp_ms": int(time.time()*1000),
        "task_type": "WIDEBAND", "rank": 2,
        "schedule": {"mode": "IMMEDIATE", "duration_ms": int(args.dwell*1000)+1000},
        "rf": {
            "center_freq_hz": args.freq, "bandwidth_hz": args.bw,
            "sample_rate_sps": args.sr, "rx_count": 1,
            "rx_gain_db": [args.gain], "rx_agc": [False],
        },
        "streaming": {"dest_ip": DEST_IP},
        "wideband": {"record_raw_iq": True, "fft_size": 2048},
    }, timeout=25)

    if not resp or resp.get("status") != "ACCEPTED":
        print("REJECTED:", resp.get("reject_reason","timeout") if resp else "no response")
        sess.close(); return

    port    = resp["streams"][0]["udp_port"]
    task_id = resp["task_id"]
    print(f"Accepted — collecting on UDP :{port} …")

    iq = collect(port, args.dwell)
    sess.fire({"msg_type": "TASK_STOP", "request_id": str(uuid.uuid4()),
               "task_id": task_id, "timestamp_ms": int(time.time()*1000),
               "reason": "spectrum done"})
    sess.close()

    print(f"Collected {len(iq):,} samples  ({len(iq)/args.sr:.2f}s @ {args.sr/1e6:.0f} MSPS)\n")
    if len(iq) < 8192:
        print("Too few samples — check UDP connectivity"); return

    freqs, db = spectrum(iq, args.freq, args.sr)
    noise     = np.median(db)
    peak_db   = db.max()
    peak_freq = freqs[db.argmax()]

    print(f"Noise floor : {noise:.1f} dBFS (Welch median)")
    print(f"Peak        : {peak_db:.1f} dBFS @ {peak_freq/1e6:.3f} MHz  "
          f"(+{peak_db-noise:.1f} dB above noise)\n")

    # Top-N peaks
    print(f"{'─'*60}")
    print(f"Top {args.peaks} peaks above noise floor:")
    print(f"{'─'*60}")

    # Simple peak-pick: sliding maximum with 10 kHz guard
    guard_bins = max(1, int(10e3 / (args.sr / len(freqs))))
    remaining  = db.copy()
    peaks_out  = []
    for _ in range(args.peaks * 3):
        idx  = int(np.argmax(remaining))
        pwr  = remaining[idx]
        if pwr < noise + 1.0:
            break
        peaks_out.append((freqs[idx], pwr, pwr - noise))
        lo = max(0, idx - guard_bins)
        hi = min(len(remaining), idx + guard_bins + 1)
        remaining[lo:hi] = noise - 99
        if len(peaks_out) >= args.peaks:
            break

    peaks_out.sort(key=lambda x: x[0])
    for f, p, above in peaks_out:
        bar = "█" * min(int(above), 40)
        print(f"  {f/1e6:9.4f} MHz  {p:7.1f} dBFS  +{above:5.1f} dB  {bar}")

    print(f"\n{'─'*60}")
    print("Text waterfall (full band, lo=5th pct, hi=99th pct):")
    lo_f, hi_f = freqs[0]/1e6, freqs[-1]/1e6
    print(f"  {lo_f:.2f} MHz {'':^48} {hi_f:.2f} MHz")
    print(f"  {waterfall_row(db)}")
    print()


if __name__ == "__main__":
    main()
