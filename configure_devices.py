#!/usr/bin/env python3
"""
Interactive devices.xml / scanner.xml generator for OpenRFStack nodes.

Walks through adding one or more SDR devices (with sensible PlutoSDR/RTL-SDR
presets you can accept or override) and a sweep frequency range, then writes
devices.xml (and patches scanner.xml's sweep range) into one of the
deploy/<node-type>/configs/ directories, or to a custom path for mounting at
runtime with -v /path/to/devices.xml:/etc/sdr/devices.xml:ro,z.

Usage:
    ./configure_devices.py
"""
import os
import re
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
NODE_TYPES = ["sdr-node", "mobile-node", "recon-node"]

# sample_rate_min_msps: lowest rate the hardware ADC/driver can actually be
# set to (0.0 = no documented floor — don't enforce one). The controller
# doesn't reject requests below this; it acquires at an integer multiple
# that clears the floor and decimates back down via Ddc, transparently.
# These are fallback values used only when --probe-devices can't reach the
# unit (busy / not attached yet) — a live probe always wins when available.
PRESETS = {
    "pluto": {
        "label": "PlutoSDR",
        "driver": "plutosdr",
        "uri": "",
        "uri_hint": "leave empty to auto-discover via USB scan or zeroconf/mDNS",
        "rx_channels": 2, "tx_channels": 2,
        "freq_min_mhz": 70.0, "freq_max_mhz": 6000.0,
        "bandwidth_max_mhz": 20.0, "sample_rate_max_msps": 61.44,
        # AD9361 minimum interface sample rate (datasheet); SoapyPlutoSDR can
        # go lower with extra FIR decimation but this is the documented floor.
        "sample_rate_min_msps": 0.520833,
        "rx_gain_min_db": -3, "rx_gain_max_db": 71,
        "rx_agc": False, "rx_gain_db": 30,
    },
    "rtlsdr": {
        "label": "RTL-SDR",
        "driver": "rtlsdr",
        "uri": "driver=rtlsdr",
        "uri_hint": "SoapyRTLSDR ignores this beyond the driver= key",
        "rx_channels": 1, "tx_channels": 0,
        "freq_min_mhz": 0.5, "freq_max_mhz": 1700.0,
        "bandwidth_max_mhz": 3.2, "sample_rate_max_msps": 3.2,
        # RTL2832U rejects setSampleRate() below this (librtlsdr). Note there's
        # also a dead zone from 300,001-900,000 Hz this single floor can't
        # express — requests landing in that gap will still fail at the
        # hardware layer; not modelled here.
        "sample_rate_min_msps": 0.225001,
        "rx_gain_min_db": 0, "rx_gain_max_db": 49,
        "rx_agc": False, "rx_gain_db": 30,
    },
    "hackrf": {
        "label": "HackRF",
        "driver": "hackrf",
        "uri": "driver=hackrf",
        "uri_hint": "SoapyHackRF ignores this beyond the driver= key",
        "rx_channels": 1, "tx_channels": 1,
        "freq_min_mhz": 1.0, "freq_max_mhz": 6000.0,
        "bandwidth_max_mhz": 20.0, "sample_rate_max_msps": 20.0,
        # Officially documented HackRF One minimum sample rate.
        "sample_rate_min_msps": 2.0,
        # Aggregate of HackRF's 3 gain stages (LNA 0-40, VGA 0-62, amp 0/14)
        # as SoapyHackRF exposes a single combined RX gain.
        "rx_gain_min_db": 0, "rx_gain_max_db": 116,
        "rx_agc": False, "rx_gain_db": 30,
    },
    "limesdr": {
        "label": "LimeSDR",
        "driver": "lime",
        "uri": "driver=lime",
        "uri_hint": "SoapyLMS7 ignores this beyond the driver= key",
        "rx_channels": 2, "tx_channels": 2,
        "freq_min_mhz": 0.1, "freq_max_mhz": 3800.0,
        "bandwidth_max_mhz": 130.0, "sample_rate_max_msps": 61.44,
        # Not independently verified for this driver — leave unenforced
        # rather than risk an incorrect floor; use --probe-devices to fill in.
        "sample_rate_min_msps": 0.0,
        "rx_gain_min_db": 0, "rx_gain_max_db": 73,
        "rx_agc": False, "rx_gain_db": 30,
    },
    "usrp": {
        "label": "USRP B210",
        "driver": "uhd",
        "uri": "driver=uhd",
        "uri_hint": "add e.g. ,serial=XXXXXXX here to pin a specific unit if you have more than one",
        "rx_channels": 2, "tx_channels": 2,
        "freq_min_mhz": 70.0, "freq_max_mhz": 6000.0,
        # B200/B210 use the same AD9361 as PlutoSDR, hence matching bandwidth/SR.
        "bandwidth_max_mhz": 56.0, "sample_rate_max_msps": 61.44,
        "sample_rate_min_msps": 0.520833,
        "rx_gain_min_db": 0, "rx_gain_max_db": 76,
        "rx_agc": False, "rx_gain_db": 30,
    },
    "sdrplay": {
        "label": "SDRplay",
        "driver": "sdrplay",
        "uri": "driver=sdrplay",
        "uri_hint": "SoapySDRPlay3 ignores this beyond the driver= key",
        "rx_channels": 1, "tx_channels": 0,
        "freq_min_mhz": 0.001, "freq_max_mhz": 2000.0,
        "bandwidth_max_mhz": 8.0, "sample_rate_max_msps": 10.66,
        # Varies by RSP model — not independently verified; use
        # --probe-devices to fill in rather than guessing.
        "sample_rate_min_msps": 0.0,
        # SDRplay controls gain via reduction steps, not a clean dB range;
        # this is an approximate aggregate — adjust after checking your unit.
        "rx_gain_min_db": 0, "rx_gain_max_db": 40,
        "rx_agc": False, "rx_gain_db": 30,
    },
    "custom": {
        "label": "", "driver": "", "uri": "", "uri_hint": "",
        "rx_channels": 1, "tx_channels": 0,
        "freq_min_mhz": 0.0, "freq_max_mhz": 0.0,
        "bandwidth_max_mhz": 0.0, "sample_rate_max_msps": 0.0,
        "sample_rate_min_msps": 0.0,
        "rx_gain_min_db": 0, "rx_gain_max_db": 0,
        "rx_agc": False, "rx_gain_db": 30,
    },
}

# ── Live device probing (SoapySDRUtil --probe) ──────────────────────────────
# Matches the args convention RadioDevice.cpp uses to open devices:
# driver=X[,uri=Y] (or ,remote=Y for SoapyRemote) — see RadioDevice::open().
_UNIT_MULT = {
    "hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9,
    "sps": 1.0, "ksps": 1e3, "msps": 1e6, "gsps": 1e9,
}


def soapy_args(driver, uri):
    uri = (uri or "").strip()
    if uri.startswith("driver="):
        return uri  # preset already stores a full args string (rtlsdr/hackrf/etc.)
    parts = [f"driver={driver}"]
    if driver == "remote":
        if uri:
            parts.append(f"remote={uri}")
    elif uri:
        parts.append(f"uri={uri}")
    return ",".join(parts)


def _parse_ranges(line):
    """'[1, 56] MSps' / '[0.225, 0.3], [0.9, 3.2] MHz' -> [(lo_hz, hi_hz), ...]"""
    unit_m = re.search(r"\]\s*([A-Za-z]+)\s*$", line)
    mult = _UNIT_MULT.get(unit_m.group(1).lower(), 1.0) if unit_m else 1.0
    out = []
    for grp in re.findall(r"\[([^\]]+)\]", line):
        parts = [p.strip() for p in grp.split(",")]
        if len(parts) != 2:
            continue
        try:
            lo, hi = float(parts[0]) * mult, float(parts[1]) * mult
        except ValueError:
            continue
        out.append((lo, hi))
    return out


def probe_device(driver, uri, timeout=15):
    """Run `SoapySDRUtil --probe=...` and pull capability fields out of its
    text output. Returns a dict of overrides (MHz/MSPS-keyed, matching the
    preset dict's units) on success, or None if the probe didn't yield usable
    RX info (tool missing, device not found, device already claimed by
    another process, timeout, etc.) — callers should fall back to preset
    defaults in that case, not treat it as fatal.
    """
    args = soapy_args(driver, uri)
    try:
        proc = subprocess.run(
            ["SoapySDRUtil", f"--probe={args}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout or ""
    if "RX Channel" not in out:
        return None  # make() failed, no match, device busy, etc.

    # Limit parsing to the first "-- RX Channel" block so a TX-only line
    # with different ranges (e.g. HackRF) doesn't get mixed in.
    rx_start = out.find("RX Channel")
    rx_end = out.find("TX Channel")
    block = out[rx_start: rx_end if rx_end != -1 else len(out)]

    overrides = {}
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("Sample rates:"):
            ranges = _parse_ranges(s)
            if ranges:
                overrides["sample_rate_min_msps"] = min(lo for lo, _ in ranges) / 1e6
                overrides["sample_rate_max_msps"] = max(hi for _, hi in ranges) / 1e6
        elif s.startswith("Full freq range:"):
            ranges = _parse_ranges(s)
            if ranges:
                overrides["freq_min_mhz"] = ranges[0][0] / 1e6
                overrides["freq_max_mhz"] = ranges[0][1] / 1e6
        elif s.startswith("Filter bandwidths:"):
            nums = re.findall(r"[\d.]+", s.split(":", 1)[1])
            unit_m = re.search(r"([A-Za-z]+)\s*$", s)
            mult = _UNIT_MULT.get(unit_m.group(1).lower(), 1e6) if unit_m else 1e6
            if nums:
                overrides["bandwidth_max_mhz"] = max(float(n) for n in nums) * mult / 1e6
        elif s.startswith("Full gain range:"):
            ranges = _parse_ranges(s)
            if ranges:
                overrides["rx_gain_min_db"] = ranges[0][0]
                overrides["rx_gain_max_db"] = ranges[0][1]
    return overrides or None

DEVICES_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!--
  SdrResourceManager device config — generated by configure_devices.py
  Override at runtime: -v /path/to/devices.xml:/etc/sdr/devices.xml:ro,z
-->
<sdr_controller version="2.0">

  <broker>
    <url>amqp://localhost:5672</url>
    <username>sdr_ctrl</username>
    <password>sdr_hw_test</password>
    <request_queue>sdr.task.request</request_queue>
    <response_queue>sdr.task.response</response_queue>
    <status_topic>sdr.status</status_topic>
    <health_topic>sdr.health</health_topic>
    <reconnect_interval_sec>5</reconnect_interval_sec>
    <max_reconnect_interval_sec>30</max_reconnect_interval_sec>
    <send_queue_depth>512</send_queue_depth>
  </broker>

  <policy>
    <max_concurrent_tasks>4</max_concurrent_tasks>
    <guard_band_hz>200000</guard_band_hz>
    <usable_bw_fraction>0.80</usable_bw_fraction>
    <default_task_timeout_ms>120000</default_task_timeout_ms>
    <scheduler_tick_ms>200</scheduler_tick_ms>
    <watchdog_tick_ms>1000</watchdog_tick_ms>
    <udp_port_pool_start>30000</udp_port_pool_start>
    <udp_port_pool_end>30099</udp_port_pool_end>
    <iq_packet_samples>256</iq_packet_samples>
    <retune_conflict_policy>REJECT_NEW</retune_conflict_policy>
    <heartbeat_interval_ms>10000</heartbeat_interval_ms>
  </policy>

  <devices>
{devices_xml}
  </devices>

</sdr_controller>
"""

DEVICE_TEMPLATE = """    <device id="{id}">
      <driver>{driver}</driver>
      <uri>{uri}</uri>
      <label>{label}</label>
      <streaming_source_ip>127.0.0.1</streaming_source_ip>
      <!-- rx_agc: true = hardware AGC picks gain per-channel (good for
           varying/unknown signal strength). false = fixed manual gain at
           rx_gain_db below. rx_gain_db is ignored when rx_agc=true but
           stays here so you can flip back without re-adding it. -->
      <rx_agc>{rx_agc}</rx_agc>
      <rx_gain_db>{rx_gain_db}</rx_gain_db>
      <capabilities>
        <rx_channels>{rx_channels}</rx_channels>
        <tx_channels>{tx_channels}</tx_channels>
        <freq_min_hz>{freq_min_hz}</freq_min_hz>
        <freq_max_hz>{freq_max_hz}</freq_max_hz>
        <bandwidth_max_hz>{bandwidth_max_hz}</bandwidth_max_hz>
        <sample_rate_max_sps>{sample_rate_max_sps}</sample_rate_max_sps>
        <sample_rate_min_sps>{sample_rate_min_sps}</sample_rate_min_sps>
        <rx_gain_min_db>{rx_gain_min_db}</rx_gain_min_db>
        <rx_gain_max_db>{rx_gain_max_db}</rx_gain_max_db>
      </capabilities>
    </device>"""


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else default


def ask_float(prompt, default):
    while True:
        raw = ask(prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            print("  Please enter a number.")


def ask_bool(prompt, default):
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "true", "1")


def ask_int(prompt, default):
    while True:
        raw = ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print("  Please enter a whole number.")


def ask_choice(prompt, choices, default_idx=0):
    print(prompt)
    for i, c in enumerate(choices, 1):
        marker = " (default)" if i - 1 == default_idx else ""
        print(f"  {i}) {c}{marker}")
    while True:
        raw = input(f"Choice [1-{len(choices)}]: ").strip()
        if not raw:
            return default_idx
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return int(raw) - 1
        print("  Invalid choice.")


def configure_device(existing_ids):
    idx = ask_choice(
        "\nDevice type:",
        [
            "PlutoSDR (network or USB)",
            "RTL-SDR (USB)",
            "HackRF (USB)",
            "LimeSDR (USB)",
            "USRP B-series, via UHD (USB)",
            "SDRplay RSP series (USB)",
            "Custom / other SoapySDR driver",
        ],
    )
    preset_key = ["pluto", "rtlsdr", "hackrf", "limesdr", "usrp", "sdrplay", "custom"][idx]
    preset = PRESETS[preset_key]

    default_id = preset_key if preset_key != "custom" else "sdr"
    n = 0
    while f"{default_id}-{n}" in existing_ids:
        n += 1
    dev_id = ask("Device id", f"{default_id}-{n}")

    driver = preset["driver"] if preset_key != "custom" else ask("SoapySDR driver name (e.g. plutosdr, rtlsdr, lime, uhd)", "")
    label = ask("Label", preset["label"] or driver)
    if preset["uri_hint"]:
        print(f"  uri hint: {preset['uri_hint']}")
    uri = ask("uri (leave empty for discovery)", preset["uri"])

    if ask_choice(
        "\nProbe the device live now (SoapySDRUtil) to auto-fill capabilities below?",
        ["Yes", "No — use preset/static values"], default_idx=0,
    ) == 0:
        print("  Probing (device must be attached and not already in use)...")
        found = probe_device(driver, uri)
        if found:
            print(f"  Probe OK — found: {found}")
            preset = {**preset, **found}
        else:
            print("  Probe failed or returned no RX info (tool missing, device "
                  "busy/not found, or driver doesn't support --probe) — "
                  "falling back to preset/static values below.")

    print("Capabilities (Enter to accept preset/probed default):")
    rx_channels = ask_int("  RX channels", preset["rx_channels"])
    tx_channels = ask_int("  TX channels", preset["tx_channels"])
    freq_min_mhz = ask_float("  Min frequency (MHz)", preset["freq_min_mhz"])
    freq_max_mhz = ask_float("  Max frequency (MHz)", preset["freq_max_mhz"])
    bandwidth_max_mhz = ask_float("  Max instantaneous bandwidth (MHz)", preset["bandwidth_max_mhz"])
    sample_rate_max_msps = ask_float("  Max sample rate (MSPS)", preset["sample_rate_max_msps"])
    sample_rate_min_msps = ask_float(
        "  Min sample rate (MSPS, 0 = no known floor)", preset["sample_rate_min_msps"])
    rx_gain_min_db = ask_int("  RX gain min (dB)", preset["rx_gain_min_db"])
    rx_gain_max_db = ask_int("  RX gain max (dB)", preset["rx_gain_max_db"])

    print("Gain control:")
    rx_agc = ask_bool("  Use hardware AGC (auto gain)?", preset["rx_agc"])
    if rx_agc:
        rx_gain_db = preset["rx_gain_db"]  # unused while AGC is on, kept so flipping back needs no re-entry
    else:
        rx_gain_db = ask_int("  Manual RX gain (dB)", preset["rx_gain_db"])

    return DEVICE_TEMPLATE.format(
        id=dev_id, driver=driver, uri=uri, label=label,
        rx_channels=rx_channels, tx_channels=tx_channels,
        freq_min_hz=int(freq_min_mhz * 1e6), freq_max_hz=int(freq_max_mhz * 1e6),
        bandwidth_max_hz=int(bandwidth_max_mhz * 1e6),
        sample_rate_max_sps=int(sample_rate_max_msps * 1e6),
        sample_rate_min_sps=int(sample_rate_min_msps * 1e6),
        rx_gain_min_db=rx_gain_min_db, rx_gain_max_db=rx_gain_max_db,
        rx_agc="true" if rx_agc else "false", rx_gain_db=rx_gain_db,
    ), dev_id


def patch_scanner_sweep(scanner_path, start_hz, stop_hz):
    if not os.path.isfile(scanner_path):
        print(f"  (no scanner.xml at {scanner_path} — skipping sweep range patch)")
        return
    with open(scanner_path) as f:
        text = f.read()
    import re
    text, n1 = re.subn(r"<start_hz>\d+</start_hz>", f"<start_hz>{start_hz}</start_hz>", text)
    text, n2 = re.subn(r"<stop_hz>\d+</stop_hz>", f"<stop_hz>{stop_hz}</stop_hz>", text)
    if n1 == 0 or n2 == 0:
        print(f"  (couldn't find <start_hz>/<stop_hz> in {scanner_path} — skipping)")
        return
    shutil.copy2(scanner_path, scanner_path + ".bak")
    with open(scanner_path, "w") as f:
        f.write(text)
    print(f"  Patched {scanner_path} (backup at {scanner_path}.bak)")


def main():
    print("OpenRFStack device config generator\n")

    devices_xml_parts = []
    existing_ids = set()
    while True:
        device_xml, dev_id = configure_device(existing_ids)
        devices_xml_parts.append(device_xml)
        existing_ids.add(dev_id)
        if ask_choice("\nAdd another device?", ["No", "Yes"], default_idx=0) == 0:
            break

    print("\nSweep frequency range (used by scanner.xml if you choose to patch it):")
    start_mhz = ask_float("  Start (MHz)", 80.0)
    stop_mhz = ask_float("  Stop (MHz)", 1000.0)
    start_hz, stop_hz = int(start_mhz * 1e6), int(stop_mhz * 1e6)

    devices_xml = DEVICES_TEMPLATE.format(devices_xml="\n\n".join(devices_xml_parts))

    print("\nWhere should this be written?")
    target_idx = ask_choice(
        "",
        [f"deploy/{t}/configs/ (overwrites that node type's defaults)" for t in NODE_TYPES]
        + ["Custom path (for mounting with -v at runtime)"],
        default_idx=0,
    )

    if target_idx < len(NODE_TYPES):
        node_dir = os.path.join(REPO_ROOT, "deploy", NODE_TYPES[target_idx], "configs")
        devices_path = os.path.join(node_dir, "devices.xml")
        scanner_path = os.path.join(node_dir, "scanner.xml")
    else:
        devices_path = ask("Output path for devices.xml", os.path.join(REPO_ROOT, "devices.xml"))
        scanner_path = None

    if os.path.isfile(devices_path):
        if ask_choice(f"\n{devices_path} already exists. Overwrite?", ["No", "Yes"], default_idx=0) == 0:
            print("Aborted — nothing written.")
            return
        shutil.copy2(devices_path, devices_path + ".bak")
        print(f"  Backed up existing file to {devices_path}.bak")

    os.makedirs(os.path.dirname(devices_path), exist_ok=True)
    with open(devices_path, "w") as f:
        f.write(devices_xml)
    print(f"\nWrote {devices_path}")

    if scanner_path:
        if ask_choice(f"Patch {scanner_path}'s sweep range to {start_mhz}-{stop_mhz} MHz too?",
                      ["Yes", "No"], default_idx=0) == 0:
            patch_scanner_sweep(scanner_path, start_hz, stop_hz)

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(1)
