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
Live listener: subscribes to rf.detections and rf.analysis, prints a
consolidated signal table.  Run while AcquisitionApp + AnalysisApp are active.

Usage:
    PYTHONPATH=/tmp/proton_pkg python3 listen_results.py [--duration 120]
"""
from __future__ import annotations
import argparse, json, threading, time
import proton, proton.handlers, proton.reactor

BROKER  = "amqp://localhost:5672"
CREDS   = ("sdr_ctrl", "sdr_hw_test")
TOPICS  = ["rf.detections", "rf.analysis"]


class Listener(proton.handlers.MessagingHandler):
    def __init__(self, duration: float):
        super().__init__()
        self._deadline   = time.time() + duration
        self._detections: dict[float, dict] = {}   # freq_hz → best detection
        self._analyses:   dict[float, dict] = {}   # freq_hz → analysis result
        self._lock       = threading.Lock()
        self._printed    = set()

    def on_start(self, ev):
        c = ev.container.connect(BROKER, user=CREDS[0], password=CREDS[1],
                                 sasl_enabled=True, allowed_mechs="PLAIN")
        for topic in TOPICS:
            ev.container.create_receiver(c, topic)
        ev.container.schedule(1.0, self)   # tick every second

    def on_timer_task(self, ev):
        self._flush()
        if time.time() < self._deadline:
            ev.container.schedule(1.0, self)
        else:
            ev.container.stop()

    def on_message(self, ev):
        try:
            body = ev.message.body
            msg  = json.loads(body if isinstance(body, str) else body.decode())
        except Exception:
            return

        mtype = msg.get("msg_type", "")
        freq  = float(msg.get("center_freq_hz", 0))
        bw    = float(msg.get("bandwidth_hz", 0))

        with self._lock:
            if mtype == "RF_DETECTION":
                existing = self._detections.get(freq)
                if not existing or msg.get("power_db", -999) > existing.get("power_db", -999):
                    self._detections[freq] = msg

            elif mtype == "ANALYSIS_RESULT":
                existing = self._analyses.get(freq)
                if not existing or msg.get("snr_db", -999) > existing.get("snr_db", -999):
                    self._analyses[freq] = msg
                    self._print_analysis(msg)   # print immediately

    def _print_analysis(self, msg: dict):
        freq_mhz = msg["center_freq_hz"] / 1e6
        bw_khz   = msg["bandwidth_hz"] / 1e3
        snr      = msg.get("snr_db", 0)
        mod      = ""
        if msg.get("modulation"):
            m = msg["modulation"]
            if m.get("analog"):
                idx = m.get("analog_index", 0)
                mod = f"{m['analog']}"
                if idx: mod += f" (idx={idx:.2f})"
            elif m.get("digital"):
                mod = m["digital"]
                if m.get("m_ary", 0) > 1: mod += f"-{m['m_ary']}"
                if m.get("symbol_rate_sps", 0):
                    mod += f" @{m['symbol_rate_sps']/1e3:.0f}ksps"

        cs = msg.get("channel_structure", {})
        flags = []
        if cs.get("is_burst"):  flags.append("burst")
        if cs.get("is_tdma"):   flags.append("TDMA")
        if cs.get("is_fhss"):   flags.append("FHSS")
        if cs.get("is_dsss"):   flags.append("DSSS")
        if cs.get("is_ofdm") or msg.get("modulation", {}).get("is_ofdm"):
            flags.append("OFDM")
        flag_str = f" [{','.join(flags)}]" if flags else ""

        if not msg.get("classified"):
            mod = f"unclassified (SNR={snr:.1f}dB)"

        key = round(freq_mhz, 2)
        if key not in self._printed:
            self._printed.add(key)
            print(f"  {freq_mhz:9.3f} MHz  BW={bw_khz:7.1f}kHz  "
                  f"SNR={snr:5.1f}dB  {mod}{flag_str}")

    def _flush(self):
        """Print detections that never got an analysis result."""
        with self._lock:
            analysed_freqs = set(self._analyses.keys())
            for freq, det in sorted(self._detections.items()):
                if freq not in analysed_freqs:
                    freq_mhz = freq / 1e6
                    key = round(freq_mhz, 2)
                    if key not in self._printed:
                        self._printed.add(key)
                        pwr = det.get("power_db", 0)
                        bw  = det.get("bandwidth_hz", 0) / 1e3
                        print(f"  {freq_mhz:9.3f} MHz  BW={bw:7.1f}kHz  "
                              f"power={pwr:.1f}dBm  (detection only — analysis pending)")

    def summary(self):
        all_freqs = sorted(set(self._detections) | set(self._analyses))
        print(f"\n{'='*70}")
        print(f"  SCAN SUMMARY — {len(all_freqs)} unique signal(s) found")
        print(f"{'='*70}")
        if not all_freqs:
            print("  No signals detected above threshold.")
            return
        print(f"  {'Freq (MHz)':>11}  {'BW (kHz)':>9}  {'SNR':>6}  Modulation")
        print(f"  {'-'*66}")
        for freq in all_freqs:
            freq_mhz = freq / 1e6
            a = self._analyses.get(freq)
            d = self._detections.get(freq)
            bw_khz = (a or d or {}).get("bandwidth_hz", 0) / 1e3
            if a:
                snr = a.get("snr_db", 0)
                mod = ""
                m = a.get("modulation", {})
                if m.get("analog"):   mod = m["analog"]
                elif m.get("digital"):
                    mod = m["digital"]
                    if m.get("m_ary", 0) > 1: mod += f"-{m['m_ary']}"
                if not a.get("classified"): mod = "unclassified"
                print(f"  {freq_mhz:11.3f}  {bw_khz:9.1f}  {snr:6.1f}  {mod}")
            elif d:
                pwr = d.get("power_db", 0)
                print(f"  {freq_mhz:11.3f}  {bw_khz:9.1f}  {'--':>6}  "
                      f"(RF detection {pwr:.1f}dBm — no analysis)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=120,
                    help="seconds to listen (default 120)")
    args = ap.parse_args()

    print(f"{'='*70}")
    print(f"  Listening on rf.detections + rf.analysis for {args.duration:.0f}s")
    print(f"  (AcquisitionApp sweeps ~80–1000 MHz; each pass ~15–20s)")
    print(f"{'='*70}")
    print(f"  {'Freq (MHz)':>9}  {'BW (kHz)':>9}  {'SNR':>6}  Modulation")
    print(f"  {'-'*60}")

    handler = Listener(args.duration)
    container = proton.reactor.Container(handler)
    container.run()
    handler.summary()


if __name__ == "__main__":
    main()

# ========================================================================
# End of file — OpenRFStack
# Subject to Personal Use License
# https://github.com/OpenRFStack
# ========================================================================
