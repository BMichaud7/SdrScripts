#!/usr/bin/env python3
"""
@file annotate_signals.py
@brief Cross-reference detected signals with US frequency allocations.

Usage:
    python3 annotate_signals.py [--db signals.db] [--min-hits 1]
"""
import argparse
import sqlite3
from typing import Optional

# US primary frequency allocations (MHz ranges)
# Format: (start_mhz, stop_mhz, service, expected_modulation, notes)
ALLOCATIONS = [
    (76,   88,   "TV (VHF low, ch2-6)",          "AM-VSB",  "Broadcast TV — rare now"),
    (88,   108,  "FM Broadcast",                 "FM_WB",   "Commercial FM radio, 200 kHz spacing"),
    (108,  118,  "Aviation Nav (VOR/ILS/LOC)",   "AM_DSB",  "Navigation beacons, 50 kHz spacing"),
    (118,  137,  "Aviation Voice (AM)",           "AM_DSB",  "ATC, ATIS, unicom — AM"),
    (137,  138,  "NOAA Weather Satellites",       "FM_NB",   "NOAA APT downlink 137.5/137.9125"),
    (138,  144,  "Federal/Military",              "FM_NB",   "US govt, SATCOM"),
    (144,  148,  "Amateur 2m",                    "FM_NB",   "Ham radio repeaters"),
    (148,  150,  "Mobile satellite uplink",       "GMSK",    "Orbcomm, etc."),
    (150,  162,  "VHF Land Mobile",               "FM_NB",   "Public safety, business"),
    (162,  163,  "NOAA Weather Radio",            "FM_NB",   "NOAA WX: 162.400–162.550"),
    (163,  174,  "VHF Land Mobile",               "FM_NB",   "Public safety, business"),
    (174,  216,  "TV (VHF high, ch7-13)",         "AM-VSB",  "Broadcast TV"),
    (216,  222,  "Fixed/Mobile",                  "various", "SCADA, telemetry"),
    (222,  225,  "Amateur 1.25m",                 "FM_NB",   "Ham radio"),
    (225,  400,  "Military Aviation",              "AM_DSB",  "DoD comms, military UHF"),
    (400,  406,  "Meteorological",                "FSK",     "Radiosondes, weather balloons"),
    (406,  420,  "UHF Federal",                   "FM_NB",   "US govt, P25"),
    (420,  450,  "Amateur 70cm",                  "FM_NB",   "Ham radio UHF"),
    (450,  470,  "UHF Business/Commercial",       "FM_NB",   "Land mobile, MURS-adjacent"),
    (470,  608,  "UHF TV (ch14-36)",              "OFDM",    "Broadcast TV / wireless mics"),
    (608,  614,  "Radio Astronomy",               None,      "Protected band"),
    (614,  698,  "UHF TV (ch38-51) / Wireless",  "OFDM",    "TV + wireless mics"),
    (698,  806,  "LTE Band 17/12/13/14",          "OFDM",    "4G/5G cellular (lower 700)"),
    (806,  824,  "Public Safety",                 "P25",     "FirstNet uplink, 700 MHz PS"),
    (824,  849,  "Cellular 850 uplink",           "CDMA",    "850 MHz cellular (ATT/Verizon)"),
    (849,  851,  "ESMR/SMR",                      "OFDM",    "Nextel-heritage"),
    (851,  869,  "Public Safety downlink",        "P25",     "800 MHz P25 downlink"),
    (869,  894,  "Cellular 850 downlink",         "CDMA",    "850 MHz cellular downlink"),
    (894,  896,  "ESMR/SMR",                      "various", ""),
    (896,  902,  "SMR uplink",                    "various", ""),
    (902,  928,  "ISM 900 MHz",                   "FSK",     "LoRa, Zigbee, 802.11ah, RFID"),
    (928,  935,  "Fixed/Paging",                  "FSK",     "Paging, SCADA"),
    (935,  941,  "SMR downlink",                  "various", ""),
    (941,  960,  "Fixed/Mobile",                  "various", ""),
    (960,  1215, "Aviation Radar / TACAN",         "pulse",   "DME, TACAN, SSR"),
    (1176, 1186, "GPS L5",                        "BPSK",    "GPS L5 1176.45 MHz"),
    (1215, 1240, "Radiolocation / GPS L2",        "BPSK",    "GPS L2 1227.6 MHz"),
    (1240, 1300, "Amateur 23cm",                  "various", "Ham radio SHF"),
    (1300, 1427, "Aviation Radar",                "pulse",   ""),
    (1427, 1435, "Fixed/Mobile Satellite",        "various", ""),
    (1525, 1559, "Mobile Satellite (downlink)",   "various", "Iridium, Inmarsat"),
    (1559, 1610, "Aeronautical Radionavigation",  "BPSK",    "GPS L1 1575.42, GLONASS 1602"),
    (1610, 1626, "Mobile Satellite",              "various", "Iridium"),
    (1626, 1660, "Mobile Satellite (uplink)",     "various", "Inmarsat, Iridium"),
    (1710, 1755, "AWS-1 uplink (LTE Band 4)",     "OFDM",    "T-Mobile, AT&T 4G"),
    (1755, 1850, "Federal/DoD",                   "various", "Military"),
    (1850, 1910, "PCS uplink (LTE B25/2)",        "OFDM",    "Sprint, T-Mobile 1900"),
    (1910, 1930, "DECT / unlicensed",             "GFSK",    "Cordless phones"),
    (1930, 1995, "PCS downlink (LTE B25/2)",      "OFDM",    "Sprint, T-Mobile 1900 downlink"),
    (2110, 2155, "AWS-1 downlink (LTE Band 4)",   "OFDM",    "T-Mobile, AT&T 4G downlink"),
    (2155, 2180, "AWS-2 (LTE Band 66)",           "OFDM",    "T-Mobile Band 66"),
    (2305, 2360, "Satellite Radio / MSS",         "OFDM",    "SiriusXM 2.3 GHz"),
    (2360, 2400, "Aviation test / Amateur",       "various", ""),
    (2400, 2484, "ISM 2.4 GHz",                  "OFDM",    "WiFi 802.11b/g/n, Bluetooth"),
    (2496, 2690, "BRS/EBS (LTE Band 41)",         "OFDM",    "T-Mobile 5G n41, CBRS"),
    (2690, 2700, "Radio Astronomy",               None,      "Protected"),
    (2700, 3000, "Aviation / Weather Radar",      "pulse",   "ASR, TDWR airport radar"),
]


def lookup(freq_mhz: float) -> Optional[tuple]:
    """@brief Find the US frequency allocation entry for a given frequency.
    @param freq_mhz  Frequency in MHz.
    @return          (service, expected_modulation, notes) tuple, or None if unallocated.
    """
    for start, stop, service, mod, notes in ALLOCATIONS:
        if start <= freq_mhz < stop:
            return (service, mod, notes)
    return None


def verdict(detected_mod: str, expected_mod: Optional[str]) -> str:
    """@brief Compare a detected modulation against the expected allocation modulation.
    @param detected_mod  Modulation string from the signals table (may be empty).
    @param expected_mod  Expected modulation from ALLOCATIONS (may be None).
    @return              "match", "mismatch", "unclassified", or "protected".
    """
    if not expected_mod:
        return "protected"
    if not detected_mod:
        return "unclassified"
    dm = detected_mod.upper()
    em = expected_mod.upper()
    if em in dm or dm in em:
        return "match"
    # fuzzy matches
    pairs = [("FM_WB", "FM"), ("FM_NB", "FM"), ("AM_DSB", "AM"), ("OFDM", "LTE")]
    for a, b in pairs:
        if (a in dm and b in em) or (b in dm and a in em):
            return "match"
    return "mismatch"


def main():
    """@brief Entry point: cross-reference all signals in the DB against ALLOCATIONS and print a report."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="signals.db")
    ap.add_argument("--min-hits", type=int, default=1)
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    rows = db.execute(
        """SELECT freq_mhz, bandwidth_hz/1000.0, power_db, snr_db,
                  modulation, mod_class, hits, first_seen
           FROM signals
           WHERE hits >= ?
           ORDER BY freq_mhz""",
        (args.min_hits,)
    ).fetchall()

    if not rows:
        print("No signals in DB (try lowering --min-hits)")
        return

    print(f"\n{'Freq':>10}  {'BW':>8}  {'Pwr':>7}  {'SNR':>6}  {'Hits':>4}  {'Modulation':<16}  {'Expected Band / Service'}")
    print("─" * 120)

    match_count = mismatch_count = unclass_count = 0

    for freq_mhz, bw_khz, power_db, snr_db, modulation, mod_class, hits, first_seen in rows:
        alloc = lookup(freq_mhz)
        if alloc:
            service, expected_mod, notes = alloc
            v = verdict(modulation or "", expected_mod)
        else:
            service, expected_mod, notes, v = "Unallocated", None, "", "unknown"

        snr_str = f"{snr_db:5.1f}" if snr_db else "  —  "
        mod_str = modulation or "—"

        marker = ""
        if v == "match":       marker = "✓"; match_count += 1
        elif v == "mismatch":  marker = "✗"; mismatch_count += 1
        elif v == "unclassified": marker = "?"; unclass_count += 1

        print(f"{freq_mhz:>10.3f}  {bw_khz:>7.1f}k  {power_db:>6.1f}  {snr_str}  {hits:>4}  {mod_str:<16}  {marker} {service}")
        if notes:
            print(f"{'':>10}  {'':>8}  {'':>7}  {'':>6}  {'':>4}  {'':16}    {notes}")

    total = len(rows)
    print("─" * 120)
    print(f"\nTotal: {total} signals  |  Match: {match_count}  |  Mismatch: {mismatch_count}  |  Unclassified: {unclass_count}")
    db.close()


if __name__ == "__main__":
    main()
