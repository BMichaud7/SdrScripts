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
Scan 80–200 MHz, collect IQ per step, FFT, find peaks, classify signals.

Steps (each 20 MHz BW @ 20 MSPS):
  90, 110, 130, 150, 170, 190 MHz  → covers 80–200 MHz

Signal classification by bandwidth + frequency:
  > 100 kHz  in FM band (87.5–108)  → Wideband FM broadcast
  20–100 kHz in aviation (108–136)  → Aviation AM (narrowband AM)
  100–250 kHz anywhere              → Narrowband FM (public safety / weather)
  etc.
"""
from __future__ import annotations
import json, socket, struct, threading, time, uuid
import numpy as np
import proton, proton.handlers, proton.reactor

BROKER  = "amqp://localhost:5672"
REQ_Q   = "sdr.task.request"
RESP_Q  = "sdr.task.response"
CREDS   = ("sdr_ctrl", "sdr_hw_test")
DEST_IP = "127.0.0.1"

IQ_HDR   = struct.Struct("<I I Q Q I H B B")   # 32 bytes
IQ_MAGIC = 0x49515030
DWELL_S  = 2.0          # seconds of IQ per step
FFT_SIZE = 65536        # FFT resolution
BW_HZ    = 20e6
SR_SPS   = 20e6

CENTERS = [90e6, 110e6, 130e6, 150e6, 170e6, 190e6]   # 80–200 MHz

# ── AMQP session ─────────────────────────────────────────────────────────────

class _Handler(proton.handlers.MessagingHandler):
    def __init__(self, broker, sess):
        super().__init__()
        self._b = broker; self._s = sess
    def on_start(self, ev):
        c = ev.container.connect(self._b, user=CREDS[0], password=CREDS[1],
                                 sasl_enabled=True, allowed_mechs="PLAIN")
        ev.container.create_receiver(c, RESP_Q)
        self._sender = ev.container.create_sender(c, REQ_Q)
        self._s._handler = self
    def on_sendable(self, ev): self._s._ready.set()
    def on_message(self, ev):
        try:
            body = ev.message.body
            msg  = json.loads(body if isinstance(body, str) else body.decode())
        except Exception: return
        rid = msg.get("request_id","")
        with self._s._lock:
            entry = self._s._pending.get(rid)
        if entry:
            ev2, box = entry; box.append(msg); ev2.set()
    def send(self, d):
        self._sender.send(proton.Message(body=json.dumps(d),
                                         content_type="application/json"))

class Session:
    def __init__(self, broker):
        self._pending = {}; self._lock = threading.Lock()
        self._ready   = threading.Event(); self._handler = None; self.error = None
        self._container = proton.reactor.Container(_Handler(broker, self))
        threading.Thread(target=self._container.run, daemon=True).start()
        self._ready.wait(10)
    def rpc(self, req, timeout=20):
        rid = req.get("request_id","")
        ev = threading.Event(); box = []
        with self._lock: self._pending[rid] = (ev, box)
        self._handler.send(req)
        ev.wait(timeout)
        with self._lock: self._pending.pop(rid, None)
        return box[0] if box else None
    def fire(self, req): self._handler.send(req)
    def close(self):
        try: self._container.stop()
        except: pass

# ── IQ collection ─────────────────────────────────────────────────────────────

def collect_iq(port: int, duration_s: float) -> np.ndarray:
    """Return complex64 samples from the UDP stream."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8*1024*1024)
    s.settimeout(3.0)
    s.bind(("", port))
    chunks = []
    deadline = time.time() + duration_s
    try:
        while time.time() < deadline:
            try: data = s.recv(65536)
            except socket.timeout: break
            if len(data) < IQ_HDR.size: continue
            if IQ_HDR.unpack_from(data)[0] != IQ_MAGIC: continue
            n_samp = IQ_HDR.unpack_from(data)[5]
            payload = data[IQ_HDR.size:]
            n_bytes = n_samp * 8   # CF32: 4 bytes I + 4 bytes Q
            if len(payload) < n_bytes: continue
            raw = np.frombuffer(payload[:n_bytes], dtype=np.float32)
            chunks.append(raw[0::2] + 1j * raw[1::2])
    finally:
        s.close()
    return np.concatenate(chunks) if chunks else np.array([], dtype=np.complex64)

# ── Spectrum analysis ─────────────────────────────────────────────────────────

FRAME_SIZE = 8192    # per-frame FFT for Welch averaging
THRESHOLD_DB = 10.0  # dB above noise floor

def analyse(samples: np.ndarray, center_hz: float, sr: float):
    """Welch-averaged PSD → peak detection → signal list."""
    if len(samples) < FRAME_SIZE * 4:
        return []

    win    = np.blackman(FRAME_SIZE)
    step   = FRAME_SIZE // 2
    frames = (len(samples) - FRAME_SIZE) // step
    acc    = np.zeros(FRAME_SIZE)
    for i in range(frames):
        seg  = samples[i*step : i*step + FRAME_SIZE] * win
        acc += np.abs(np.fft.fft(seg, FRAME_SIZE)) ** 2
    acc    /= frames
    spec_db = np.fft.fftshift(10 * np.log10(acc + 1e-30))
    freqs   = np.fft.fftshift(np.fft.fftfreq(FRAME_SIZE, 1/sr)) + center_hz

    noise_floor = np.median(spec_db)
    threshold   = noise_floor + THRESHOLD_DB

    # Find contiguous regions above threshold
    above = spec_db > threshold
    in_sig = False; sig_start = 0
    raw_sigs = []
    for i, a in enumerate(above):
        if a and not in_sig:  in_sig = True; sig_start = i
        elif not a and in_sig:
            in_sig = False
            lo, hi = sig_start, i - 1
            peak_i  = lo + np.argmax(spec_db[lo:hi+1])
            raw_sigs.append((freqs[peak_i], freqs[hi]-freqs[lo],
                              spec_db[peak_i] - noise_floor))
    if in_sig:
        lo, hi = sig_start, len(above) - 1
        peak_i  = lo + np.argmax(spec_db[lo:hi+1])
        raw_sigs.append((freqs[peak_i], freqs[hi]-freqs[lo],
                         spec_db[peak_i] - noise_floor))

    # Merge signals closer than 50 kHz into one (handles FM pilot / RDS tones)
    merged = []
    for freq, bw, power in sorted(raw_sigs):
        if merged and abs(freq - merged[-1][0]) < 50e3:
            pf, pb, pp = merged[-1]
            # Extend bandwidth to cover both, keep highest power
            new_bw  = max(pf + pb/2, freq + bw/2) - min(pf - pb/2, freq - bw/2)
            merged[-1] = (pf if pp >= power else freq, new_bw, max(pp, power))
        else:
            merged.append((freq, bw, power))

    results = []
    for (freq, bw, power) in merged:
        if bw < 2e3:   continue   # skip < 2 kHz (still noise after merge)
        if bw > 19e6:  continue   # skip full-band DC artefact
        results.append(dict(freq_mhz=freq/1e6, bw_khz=bw/1e3,
                            power_dbc=power, demod=classify(freq, bw)))
    return results


def classify(freq_hz: float, bw_hz: float) -> str:
    """Heuristic demodulation / signal type guess."""
    f, bw = freq_hz, bw_hz
    # FM broadcast: 87.5–108 MHz, ~200 kHz wide
    if 87.5e6 <= f <= 108e6 and bw > 80e3:
        return "WFM (broadcast FM)"
    # Aviation VOR/ILS: 108–118 MHz, narrow AM
    if 108e6 <= f < 118e6:
        if bw < 30e3: return "AM (aviation nav VOR/ILS)"
        return "AM (aviation, wideband)"
    # Aircraft voice: 118–136 MHz, AM 25 kHz channels
    if 118e6 <= f < 136e6:
        return "AM (aircraft voice)"
    # Weather satellites / Meteor: 137–138 MHz
    if 136e6 <= f < 139e6:
        if bw > 30e3: return "APT/LRPT (met satellite)"
        return "FSK/BPSK (LEO telemetry)"
    # 2m amateur: 144–148 MHz
    if 144e6 <= f < 148e6:
        if bw < 20e3: return "NFM (2m amateur)"
        return "WFM / SSB (2m amateur)"
    # Public safety / APRS: 148–162 MHz
    if 148e6 <= f < 162e6:
        return "NFM (public safety / APRS)"
    # NOAA Weather Radio: 162.4–162.55 MHz
    if 162.3e6 <= f <= 162.6e6:
        return "NFM (NOAA weather radio)"
    # Rest of VHF-hi
    if 162e6 <= f < 174e6:
        return "NFM (VHF public safety)"
    if 174e6 <= f < 200e6:
        if bw > 5e6: return "DVB-T (digital TV)"
        return "NFM / digital (VHF-hi)"
    # Generic fallback
    if bw > 100e3: return "WFM (wideband)"
    if bw > 20e3:  return "NFM (narrowband FM)"
    return "AM/SSB (narrowband)"

# ── Main ─────────────────────────────────────────────────────────────────────

def scan_step(sess: Session, cf_hz: float) -> list:
    req_id = str(uuid.uuid4())
    resp = sess.rpc({
        "msg_type":       "TASK_REQUEST",
        "schema_version": "2.0",
        "request_id":     req_id,
        "timestamp_ms":   int(time.time()*1000),
        "task_type":      "WIDEBAND",
        "rank":           2,
        "schedule":       {"mode": "IMMEDIATE", "duration_ms": int(DWELL_S*1000)+500},
        "rf":             {"center_freq_hz": cf_hz, "bandwidth_hz": BW_HZ,
                           "sample_rate_sps": SR_SPS, "rx_count": 1},
        "streaming":      {"dest_ip": DEST_IP},
        "wideband":       {"record_raw_iq": True, "fft_size": 2048},
    }, timeout=20)

    if not resp or resp.get("status") != "ACCEPTED":
        reason = resp.get("reject_reason","no response") if resp else "timeout"
        print(f"  [REJECT] {reason}")
        return []

    streams  = resp.get("streams", [])
    udp_port = streams[0].get("udp_port", 0) if streams else 0
    task_id  = resp.get("task_id","")
    if not udp_port:
        print("  [ERR] no UDP port"); return []

    print(f"  collecting IQ on port {udp_port} for {DWELL_S:.0f}s …", flush=True)
    samples = collect_iq(udp_port, DWELL_S)
    sess.fire({
        "msg_type": "TASK_STOP", "request_id": str(uuid.uuid4()),
        "task_id": task_id, "timestamp_ms": int(time.time()*1000),
        "reason": "scan step done"
    })

    print(f"  {len(samples):,} samples received", flush=True)
    if len(samples) == 0:
        return []
    return analyse(samples, cf_hz, SR_SPS)


def main():
    print("Starting AMQP session …")
    sess = Session(BROKER)
    if sess.error:
        raise SystemExit(f"Connect error: {sess.error}")
    print(f"Connected. Scanning 80–200 MHz in {len(CENTERS)} steps.\n")

    all_signals = []
    for cf in CENTERS:
        lo = (cf - BW_HZ/2) / 1e6
        hi = (cf + BW_HZ/2) / 1e6
        print(f"── {lo:.0f}–{hi:.0f} MHz (cf={cf/1e6:.0f} MHz) ────────────────")
        sigs = scan_step(sess, cf)
        for s in sigs:
            print(f"  {s['freq_mhz']:8.3f} MHz  BW={s['bw_khz']:6.1f} kHz  "
                  f"+{s['power_dbc']:4.1f} dBc  → {s['demod']}")
            all_signals.append(s)
        if not sigs:
            print("  (no signals detected above threshold)")
        time.sleep(1)   # brief gap between steps

    sess.close()

    print(f"\n══ Summary: {len(all_signals)} signal(s) found across 80–200 MHz ══")
    for s in sorted(all_signals, key=lambda x: x["freq_mhz"]):
        print(f"  {s['freq_mhz']:8.3f} MHz  BW={s['bw_khz']:6.1f} kHz  → {s['demod']}")


if __name__ == "__main__":
    main()
