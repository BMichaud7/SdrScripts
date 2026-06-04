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
Full 80–1000 MHz sweep: collects IQ at each step, finds signals above noise,
deduplicates and prints a final sorted table.

PYTHONPATH=/tmp/proton_pkg python3 full_sweep.py
"""
from __future__ import annotations
import json, socket, struct, threading, time, uuid
import numpy as np
import proton, proton.handlers, proton.reactor

BROKER   = "amqp://localhost:5672"
REQ_Q    = "sdr.task.request"
RESP_Q   = "sdr.task.response"
CREDS    = ("sdr_ctrl", "sdr_hw_test")
DEST_IP  = "127.0.0.1"
IQ_HDR   = struct.Struct("<I I Q Q I H B B")
IQ_MAGIC = 0x49515030

# Sweep parameters
BW_HZ    = 10e6    # 10 MHz per step
SR_SPS   = 10e6
STEP_HZ  = 8e6     # 20% overlap
START_HZ = 80e6
STOP_HZ  = 1000e6
GAIN_DB  = 40.0
DWELL_S  = 2.0
THRESH_DB = 10.0   # dB above local noise floor
FFT_SIZE  = 16384  # 16k FFT → 610 Hz bins at 10 MSPS

CENTERS = []
cf = START_HZ + BW_HZ / 2
while cf - BW_HZ / 2 < STOP_HZ:
    CENTERS.append(cf)
    cf += STEP_HZ


# ── AMQP ─────────────────────────────────────────────────────────────────────

class _H(proton.handlers.MessagingHandler):
    def __init__(self, sess):
        super().__init__(); self._s = sess
    def on_start(self, ev):
        c = ev.container.connect(BROKER, user=CREDS[0], password=CREDS[1],
                                 sasl_enabled=True, allowed_mechs="PLAIN")
        ev.container.create_receiver(c, RESP_Q)
        self._sender = ev.container.create_sender(c, REQ_Q)
        self._s._handler = self
    def on_sendable(self, ev): self._s._ready.set()
    def on_message(self, ev):
        try: msg = json.loads(ev.message.body)
        except: return
        rid = msg.get("request_id","")
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
        self._handler.send(req); ev.wait(timeout)
        with self._lock: self._pending.pop(rid, None)
        return box[0] if box else None
    def fire(self, req): self._handler.send(req)
    def close(self):
        try: self._ctr.stop()
        except: pass


# ── IQ ───────────────────────────────────────────────────────────────────────

def collect(port, secs):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16*1024*1024)
    s.settimeout(1.5); s.bind(("", port))
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


# ── Signal detection ──────────────────────────────────────────────────────────

def find_signals(iq, center_hz, sr, thresh_db):
    if len(iq) < FFT_SIZE * 2:
        return []
    win  = np.blackman(FFT_SIZE)
    step = FFT_SIZE // 2
    nfr  = max(1, (len(iq) - FFT_SIZE) // step)
    acc  = np.zeros(FFT_SIZE)
    for i in range(nfr):
        seg = iq[i*step:i*step+FFT_SIZE] * win
        acc += np.abs(np.fft.fft(seg))**2
    acc  /= nfr
    db    = np.fft.fftshift(10*np.log10(acc + 1e-30))
    freqs = np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, 1/sr)) + center_hz

    noise  = np.percentile(db, 30)   # 30th pct = noise floor
    thresh = noise + thresh_db

    # Only look at the inner 80% of the band (avoid filter roll-off edges)
    margin = int(FFT_SIZE * 0.10)
    db_inner    = db[margin:-margin]
    freqs_inner = freqs[margin:-margin]

    # Peak-pick with 25 kHz guard bands
    guard = max(1, int(25e3 / (sr / FFT_SIZE)))
    remaining = db_inner.copy()
    peaks = []
    for _ in range(50):
        idx = int(np.argmax(remaining))
        if remaining[idx] < thresh:
            break
        # Measure −3 dB bandwidth
        peak_pwr = remaining[idx]
        lo_i = idx
        while lo_i > 0 and remaining[lo_i] > peak_pwr - 3:
            lo_i -= 1
        hi_i = idx
        while hi_i < len(remaining)-1 and remaining[hi_i] > peak_pwr - 3:
            hi_i += 1
        bw = freqs_inner[hi_i] - freqs_inner[lo_i]
        peaks.append({
            "freq_hz":  float(freqs_inner[idx]),
            "freq_mhz": float(freqs_inner[idx]/1e6),
            "bw_hz":    max(float(bw), sr/FFT_SIZE),
            "bw_khz":   max(float(bw/1e3), sr/FFT_SIZE/1e3),
            "above_db": float(peak_pwr - noise),
            "pwr_dbfs": float(peak_pwr),
            "noise_dbfs": float(noise),
        })
        lo = max(0, idx - guard)
        hi = min(len(remaining), idx + guard + 1)
        remaining[lo:hi] = noise - 99

    return peaks


# ── Signal classifier ─────────────────────────────────────────────────────────

def classify(f, bw):
    if 87.5e6 <= f <= 108e6:
        if bw > 80e3:  return "FM broadcast (WFM)"
        if bw > 15e3:  return "FM subband / pilot"
        return "FM pilot tone / RDS"
    if 108e6 <= f < 118e6:
        return "Aviation nav (VOR/ILS)"
    if 118e6 <= f < 136e6:
        return "Aircraft voice (AM)"
    if 136e6 <= f < 139e6:
        return "NOAA weather sat / LEO"
    if 144e6 <= f < 148e6:
        if bw < 20e3: return "NFM — 2m amateur"
        return "2m amateur (SSB/FM)"
    if 148e6 <= f < 174e6:
        return "NFM — VHF public safety / marine"
    if 162.3e6 <= f <= 162.6e6:
        return "NOAA weather radio"
    if 174e6 <= f < 230e6:
        if bw > 1e6:  return "DAB / DVB-T (digital radio/TV)"
        return "NFM — VHF-hi"
    if 230e6 <= f < 400e6:
        if bw > 1e6:  return "DVB-T / DAB (digital)"
        if bw > 150e3: return "TETRA / DMR (trunked PMR)"
        return "NFM — UHF business / military"
    if 400e6 <= f < 420e6:
        return "Government / military UHF"
    if 406e6 <= f <= 406.1e6:
        return "EPIRB / PLB distress beacon"
    if 430e6 <= f < 440e6:
        if bw < 500e3: return "ISM 433 MHz (OOK/FSK remotes)"
        return "70cm amateur"
    if 433e6 <= f <= 434.8e6:
        return "ISM 433 MHz (OOK/FSK)"
    if 440e6 <= f < 470e6:
        return "NFM — UHF land mobile"
    if 470e6 <= f < 790e6:
        if bw > 5e6:  return "DVB-T (UHF digital TV)"
        if bw > 500e3: return "LTE / 4G (cellular DL)"
        return "UHF digital / NFM"
    if 791e6 <= f < 862e6:
        if bw > 1e6:  return "LTE band 20 / 800 MHz cellular"
        return "UHF"
    if 862e6 <= f < 870e6:
        return "GSM 900 / LTE (cellular UL)"
    if 869e6 <= f < 960e6:
        if bw > 1e6:  return "LTE / UMTS 900 (cellular DL)"
        if bw < 200e3: return "ISM 915 MHz (OOK/FSK)"
        return "GSM / UMTS 900"
    if bw > 1e6:   return "Wideband signal"
    if bw > 80e3:  return "Wideband FM / digital"
    if bw > 15e3:  return "NFM (narrowband FM)"
    return "AM / SSB / narrowband"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    n = len(CENTERS)
    est_min = n * (DWELL_S + 0.8) / 60
    print("=" * 72)
    print(f"  Full sweep {START_HZ/1e6:.0f}–{STOP_HZ/1e6:.0f} MHz")
    print(f"  {n} steps × {BW_HZ/1e6:.0f} MHz BW, {DWELL_S:.0f}s dwell, "
          f"{STEP_HZ/1e6:.0f} MHz step, gain={GAIN_DB:.0f} dB")
    print(f"  Threshold: {THRESH_DB:.0f} dB above noise · "
          f"FFT: {FFT_SIZE} pts ({SR_SPS/FFT_SIZE/1e3:.1f} kHz/bin)")
    print(f"  Estimated time: {est_min:.1f} min")
    print("=" * 72)

    sess    = Session()
    all_sig = []           # all found signals (de-duplicated later)
    t0      = time.time()

    for i, cf in enumerate(CENTERS):
        lo_mhz = (cf - BW_HZ/2)/1e6
        hi_mhz = (cf + BW_HZ/2)/1e6
        pct    = (i+1)/n*100
        ela    = time.time() - t0
        eta    = ela / max(i, 1) * (n - i)
        print(f"[{i+1:3d}/{n}] {lo_mhz:6.1f}–{hi_mhz:6.1f} MHz  "
              f"{pct:.0f}%  ETA {eta:.0f}s", end="  ", flush=True)

        rid  = str(uuid.uuid4())
        resp = sess.rpc({
            "msg_type": "TASK_REQUEST", "schema_version": "2.0",
            "request_id": rid, "timestamp_ms": int(time.time()*1000),
            "task_type": "WIDEBAND", "rank": 2,
            "schedule": {"mode": "IMMEDIATE",
                         "duration_ms": int(DWELL_S*1000)+1000},
            "rf": {"center_freq_hz": cf, "bandwidth_hz": BW_HZ,
                   "sample_rate_sps": SR_SPS, "rx_count": 1,
                   "rx_gain_db": [GAIN_DB], "rx_agc": [False]},
            "streaming": {"dest_ip": DEST_IP},
            "wideband":  {"record_raw_iq": True, "fft_size": 2048},
        }, timeout=20)

        if not resp or resp.get("status") != "ACCEPTED":
            print(f"SKIP ({resp.get('reject_reason','timeout') if resp else 'no resp'})")
            continue

        port    = resp["streams"][0]["udp_port"]
        task_id = resp["task_id"]
        iq      = collect(port, DWELL_S)
        sess.fire({"msg_type": "TASK_STOP", "request_id": str(uuid.uuid4()),
                   "task_id": task_id, "timestamp_ms": int(time.time()*1000),
                   "reason": "done"})

        sigs = find_signals(iq, cf, SR_SPS, THRESH_DB)

        # De-dup against already-logged signals (same freq ±200 kHz = same signal)
        new = [s for s in sigs
               if not any(abs(s["freq_hz"] - p["freq_hz"]) < 200e3
                          for p in all_sig)]

        if new:
            print(f"→ {len(new)} signal(s)")
            for s in new:
                typ = classify(s["freq_hz"], s["bw_hz"])
                s["type"] = typ
                print(f"         {s['freq_mhz']:9.3f} MHz  "
                      f"BW={s['bw_khz']:7.1f} kHz  "
                      f"+{s['above_db']:5.1f} dB  {typ}")
            all_sig.extend(new)
        else:
            print("clear")

        time.sleep(0.2)

    sess.close()
    elapsed = time.time() - t0

    print()
    print("=" * 72)
    print(f"  SCAN COMPLETE — {elapsed:.0f}s — "
          f"{len(all_sig)} unique signal(s) found")
    print("=" * 72)
    print(f"\n  {'Freq (MHz)':>11}  {'BW (kHz)':>9}  {'+dB':>6}  Signal type")
    print(f"  {'-'*68}")
    for s in sorted(all_sig, key=lambda x: x["freq_hz"]):
        print(f"  {s['freq_mhz']:11.3f}  {s['bw_khz']:9.1f}  "
              f"{s['above_db']:6.1f}  {s['type']}")
    print()


if __name__ == "__main__":
    main()
