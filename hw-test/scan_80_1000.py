#!/usr/bin/env python3
"""
Scan 80 MHz – 1 GHz via SdrResourceManager, report all signals found.

Steps: 20 MHz BW @ 20 MSPS, 15 MHz increments (33% overlap) → 62 steps.
Dwell: 1.5 s per step (enough for Welch averaging with 2048-pt FFT).

Usage:
    python3 scan_80_1000.py
    python3 scan_80_1000.py --threshold 12 --dwell 2.0
"""
from __future__ import annotations
import argparse, json, socket, struct, sys, threading, time, uuid
import numpy as np
import proton, proton.handlers, proton.reactor

BROKER  = "amqp://localhost:5672"
REQ_Q   = "sdr.task.request"
RESP_Q  = "sdr.task.response"
CREDS   = ("sdr_ctrl", "sdr_hw_test")
DEST_IP = "127.0.0.1"

IQ_HDR   = struct.Struct("<I I Q Q I H B B")   # 32 bytes
IQ_MAGIC = 0x49515030

BW_HZ        = 20e6
SR_SPS       = 20e6
STEP_HZ      = 15e6       # 33% overlap
START_HZ     = 80e6
STOP_HZ      = 1000e6
FRAME_SIZE   = 8192       # Welch frame

# Generate centre frequencies with overlap
CENTERS: list[float] = []
cf = START_HZ + BW_HZ / 2
while cf - BW_HZ / 2 < STOP_HZ:
    CENTERS.append(cf)
    cf += STEP_HZ

# ── AMQP session ──────────────────────────────────────────────────────────────

class _Handler(proton.handlers.MessagingHandler):
    def __init__(self, broker, sess):
        super().__init__()
        self._b = broker
        self._s = sess

    def on_start(self, ev):
        c = ev.container.connect(self._b, user=CREDS[0], password=CREDS[1],
                                 sasl_enabled=True, allowed_mechs="PLAIN")
        ev.container.create_receiver(c, RESP_Q)
        self._sender = ev.container.create_sender(c, REQ_Q)
        self._s._handler = self

    def on_sendable(self, ev):
        self._s._ready.set()

    def on_message(self, ev):
        try:
            body = ev.message.body
            msg  = json.loads(body if isinstance(body, str) else body.decode())
        except Exception:
            return
        rid = msg.get("request_id", "")
        with self._s._lock:
            entry = self._s._pending.get(rid)
        if entry:
            ev2, box = entry
            box.append(msg)
            ev2.set()

    def send(self, d):
        self._sender.send(proton.Message(body=json.dumps(d),
                                         content_type="application/json"))


class Session:
    def __init__(self, broker):
        self._pending   = {}
        self._lock      = threading.Lock()
        self._ready     = threading.Event()
        self._handler   = None
        self._container = proton.reactor.Container(_Handler(broker, self))
        threading.Thread(target=self._container.run, daemon=True).start()
        self._ready.wait(10)

    def rpc(self, req, timeout=25):
        rid = req.get("request_id", "")
        ev  = threading.Event()
        box: list = []
        with self._lock:
            self._pending[rid] = (ev, box)
        self._handler.send(req)
        ev.wait(timeout)
        with self._lock:
            self._pending.pop(rid, None)
        return box[0] if box else None

    def fire(self, req):
        self._handler.send(req)

    def close(self):
        try:
            self._container.stop()
        except Exception:
            pass


# ── IQ collection ─────────────────────────────────────────────────────────────

def collect_iq(port: int, duration_s: float) -> np.ndarray:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
    s.settimeout(2.0)
    s.bind(("", port))
    chunks: list[np.ndarray] = []
    deadline = time.time() + duration_s
    try:
        while time.time() < deadline:
            try:
                data = s.recv(65536)
            except socket.timeout:
                break
            if len(data) < IQ_HDR.size:
                continue
            fields = IQ_HDR.unpack_from(data)
            if fields[0] != IQ_MAGIC:
                continue
            n_samp  = fields[5]
            payload = data[IQ_HDR.size:]
            n_bytes = n_samp * 8
            if len(payload) < n_bytes:
                continue
            raw = np.frombuffer(payload[:n_bytes], dtype=np.float32)
            chunks.append(raw[0::2] + 1j * raw[1::2])
    finally:
        s.close()
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.complex64)


# ── Welch PSD + peak detection ────────────────────────────────────────────────

def welch_psd(samples: np.ndarray, frame: int = FRAME_SIZE) -> tuple[np.ndarray, np.ndarray]:
    """Return (freq_bins_norm, power_db) via Welch averaging."""
    win    = np.blackman(frame)
    step   = frame // 2
    n_fr   = max(1, (len(samples) - frame) // step)
    acc    = np.zeros(frame)
    for i in range(n_fr):
        seg  = samples[i * step : i * step + frame] * win
        acc += np.abs(np.fft.fft(seg)) ** 2
    acc   /= n_fr
    return np.fft.fftfreq(frame), np.fft.fftshift(10 * np.log10(acc + 1e-30))


def detect_signals(samples: np.ndarray, center_hz: float,
                   sr: float, threshold_db: float) -> list[dict]:
    if len(samples) < FRAME_SIZE * 4:
        return []

    freqs_norm, spec_db = welch_psd(samples)
    freqs_hz  = np.fft.fftshift(freqs_norm) * sr + center_hz
    noise_floor = np.median(spec_db)
    thresh      = noise_floor + threshold_db

    # Contiguous-region scan
    above = spec_db > thresh
    in_sig = False
    sig_start = 0
    raw: list[tuple[float, float, float]] = []
    for i, a in enumerate(above):
        if a and not in_sig:
            in_sig = True
            sig_start = i
        elif not a and in_sig:
            in_sig = False
            lo, hi = sig_start, i - 1
            pk = lo + int(np.argmax(spec_db[lo:hi + 1]))
            raw.append((freqs_hz[pk], freqs_hz[hi] - freqs_hz[lo],
                        spec_db[pk] - noise_floor))
    if in_sig:
        lo, hi = sig_start, len(above) - 1
        pk = lo + int(np.argmax(spec_db[lo:hi + 1]))
        raw.append((freqs_hz[pk], freqs_hz[hi] - freqs_hz[lo],
                    spec_db[pk] - noise_floor))

    # Merge blobs closer than 100 kHz
    merged: list[tuple[float, float, float]] = []
    for freq, bw, pwr in sorted(raw):
        if merged and abs(freq - merged[-1][0]) < 100e3:
            pf, pb, pp = merged[-1]
            lo_e = min(pf - pb / 2, freq - bw / 2)
            hi_e = max(pf + pb / 2, freq + bw / 2)
            merged[-1] = (pf if pp >= pwr else freq,
                          hi_e - lo_e, max(pp, pwr))
        else:
            merged.append((freq, bw, pwr))

    results = []
    for freq, bw, pwr in merged:
        if bw < 1.5e3:
            continue                 # sub-1.5 kHz — noise spike
        if bw > sr * 0.92:
            continue                 # spans whole band — DC/wideband artefact
        # Skip if freq is outside the valid window (5% from edges)
        margin = sr * 0.10
        if freq < center_hz - sr / 2 + margin:
            continue
        if freq > center_hz + sr / 2 - margin:
            continue
        results.append({
            "freq_hz":   freq,
            "freq_mhz":  freq / 1e6,
            "bw_khz":    bw / 1e3,
            "power_dbc": pwr,
            "type":      classify(freq, bw),
        })
    return results


# ── Signal classifier ─────────────────────────────────────────────────────────

def classify(f: float, bw: float) -> str:
    # FM broadcast 87.5–108 MHz
    if 87.5e6 <= f <= 108e6:
        if bw > 80e3:   return "WFM (broadcast FM)"
        if bw > 15e3:   return "NFM (FM subband)"
        return "pilot/RDS tone"

    # Aviation nav 108–118 MHz
    if 108e6 <= f < 118e6:
        if bw < 30e3:   return "AM (VOR/ILS nav)"
        return "AM (aviation wideband)"

    # Aircraft voice 118–136 MHz (AM, 25/8.33 kHz ch)
    if 118e6 <= f < 136e6:
        return "AM (aircraft voice)"

    # Weather satellite 137–138 MHz
    if 136e6 <= f < 139e6:
        if bw > 30e3:   return "APT/LRPT (met satellite)"
        return "FSK (LEO telemetry)"

    # 2m amateur 144–148 MHz
    if 144e6 <= f < 148e6:
        if bw < 20e3:   return "NFM (2m amateur)"
        return "WFM/SSB (2m amateur)"

    # Public safety / APRS 148–162 MHz
    if 148e6 <= f < 162e6:
        return "NFM (VHF public safety)"

    # NOAA Weather 162.4–162.55 MHz
    if 162.3e6 <= f <= 162.6e6:
        return "NFM (NOAA weather)"

    # VHF marine 156–174 MHz
    if 156e6 <= f < 174e6:
        return "NFM (marine VHF)"

    # Digital TV (DVB-T) 174–230 MHz
    if 174e6 <= f < 230e6:
        if bw > 5e6:    return "DVB-T (digital TV)"
        return "NFM/digital (VHF-hi)"

    # UHF TV / business 230–470 MHz
    if 230e6 <= f < 380e6:
        if bw > 5e6:    return "DVB-T / DAB (UHF TV)"
        if bw > 200e3:  return "TETRA/DMR (trunked)"
        return "NFM (UHF business)"

    # 70cm amateur / APRS 430–450 MHz
    if 430e6 <= f < 450e6:
        if bw < 20e3:   return "NFM (70cm amateur)"
        return "digital (70cm)"

    # ISM 433–434 MHz (EU)
    if 433e6 <= f <= 434.8e6:
        if bw < 500e3:  return "OOK/FSK (ISM 433 MHz)"
        return "wideband (ISM 433 MHz)"

    # UHF 450–470 MHz (land mobile)
    if 450e6 <= f < 470e6:
        return "NFM (UHF land mobile)"

    # DVB-T UHF main band 470–790 MHz
    if 470e6 <= f < 790e6:
        if bw > 5e6:    return "DVB-T (UHF digital TV)"
        if bw > 200e3:  return "LTE/4G (downlink)"
        return "NFM/digital (UHF)"

    # 900 MHz ISM / GSM / 3G
    if 869e6 <= f < 960e6:
        if bw > 1e6:    return "LTE/3G (cellular)"
        if bw < 200e3:  return "OOK/FSK (ISM 915)"
        return "GSM/UMTS (cellular)"

    # Catch-all
    if bw > 1e6:    return "wideband (unknown)"
    if bw > 100e3:  return "WFM (wideband)"
    if bw > 15e3:   return "NFM (narrowband FM)"
    return "AM/SSB (narrowband)"


# ── One scan step ─────────────────────────────────────────────────────────────

def scan_step(sess: Session, cf_hz: float,
              dwell_s: float, threshold_db: float) -> list[dict]:
    req_id = str(uuid.uuid4())
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   int(time.time() * 1000),
        "task_type":      "WIDEBAND",
        "rank":           2,
        "schedule":       {"mode": "IMMEDIATE",
                           "duration_ms": int(dwell_s * 1000) + 500},
        "rf":             {"center_freq_hz": cf_hz,
                           "bandwidth_hz":   BW_HZ,
                           "sample_rate_sps": SR_SPS,
                           "rx_count": 1},
        "streaming":      {"dest_ip": DEST_IP},
        "wideband":       {"record_raw_iq": True, "fft_size": 2048},
    }, timeout=25)

    if not resp or resp.get("status") != "ACCEPTED":
        reason = (resp.get("reject_reason", "no response") if resp else "timeout")
        print(f"  [SKIP] {reason}")
        return []

    streams  = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    task_id  = resp.get("task_id", "")
    if not udp_port:
        print("  [ERR] no UDP port assigned")
        return []

    samples = collect_iq(udp_port, dwell_s)
    sess.fire({
        "msg_type":    "TASK_STOP",
        "request_id":  str(uuid.uuid4()),
        "task_id":     task_id,
        "timestamp_ms": int(time.time() * 1000),
        "reason":      "scan step done",
    })
    return detect_signals(samples, cf_hz, SR_SPS, threshold_db)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Scan 80 MHz–1 GHz and report signals")
    ap.add_argument("--threshold", type=float, default=10.0,
                    help="dB above noise floor to flag a signal (default 10)")
    ap.add_argument("--dwell",     type=float, default=1.5,
                    help="seconds of IQ per step (default 1.5)")
    ap.add_argument("--start",     type=float, default=80e6,  help="Start Hz")
    ap.add_argument("--stop",      type=float, default=1000e6, help="Stop Hz")
    args = ap.parse_args()

    # Recompute centres if overridden
    centers: list[float] = []
    cf = args.start + BW_HZ / 2
    while cf - BW_HZ / 2 < args.stop:
        centers.append(cf)
        cf += STEP_HZ

    total_time_est = len(centers) * (args.dwell + 0.8)
    print("=" * 70)
    print(f"  SDR Scan  {args.start/1e6:.0f} MHz – {args.stop/1e6:.0f} MHz")
    print(f"  {len(centers)} steps × {BW_HZ/1e6:.0f} MHz BW, "
          f"{args.dwell:.1f}s dwell, {STEP_HZ/1e6:.0f} MHz step")
    print(f"  Threshold: {args.threshold:.0f} dB above noise floor")
    print(f"  Estimated scan time: {total_time_est/60:.1f} min")
    print("=" * 70)

    sess = Session(BROKER)
    if not sess._handler:
        sys.exit("Could not connect to AMQP broker at " + BROKER)
    print("Connected to broker.\n")

    all_signals: list[dict] = []
    t_start = time.time()

    for step_i, cf in enumerate(centers):
        lo = (cf - BW_HZ / 2) / 1e6
        hi = (cf + BW_HZ / 2) / 1e6
        pct = (step_i + 1) / len(centers) * 100
        elapsed = time.time() - t_start
        eta = elapsed / max(step_i, 1) * (len(centers) - step_i)
        print(f"[{step_i+1:2d}/{len(centers)}] {lo:.0f}–{hi:.0f} MHz "
              f"({pct:.0f}%)  ETA {eta:.0f}s", end="  ", flush=True)

        sigs = scan_step(sess, cf, args.dwell, args.threshold)

        # Deduplicate against already-found signals (same freq ±500 kHz)
        new_sigs = []
        for s in sigs:
            if not any(abs(s["freq_hz"] - p["freq_hz"]) < 500e3
                       for p in all_signals):
                new_sigs.append(s)

        if new_sigs:
            print(f"→ {len(new_sigs)} signal(s)")
            for s in new_sigs:
                print(f"         {s['freq_mhz']:8.3f} MHz  "
                      f"BW={s['bw_khz']:6.1f} kHz  "
                      f"+{s['power_dbc']:4.1f} dBc  "
                      f"→ {s['type']}")
            all_signals.extend(new_sigs)
        else:
            print("(clear)")

        time.sleep(0.3)   # brief pause between steps

    sess.close()
    elapsed_total = time.time() - t_start

    print()
    print("=" * 70)
    print(f"  SCAN COMPLETE  —  {elapsed_total:.0f}s elapsed")
    print(f"  {len(all_signals)} signal(s) found between "
          f"{args.start/1e6:.0f}–{args.stop/1e6:.0f} MHz")
    print("=" * 70)
    print(f"\n{'Freq (MHz)':>11}  {'BW (kHz)':>9}  {'+dBc':>6}  Signal Type")
    print("-" * 70)
    for s in sorted(all_signals, key=lambda x: x["freq_hz"]):
        print(f"{s['freq_mhz']:11.3f}  {s['bw_khz']:9.1f}  "
              f"{s['power_dbc']:6.1f}  {s['type']}")
    print()


if __name__ == "__main__":
    main()
