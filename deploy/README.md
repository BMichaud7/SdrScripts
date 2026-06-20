# OpenRFStack node containers

Three node types, each published as a full and a `-lite` GHCR package —
6 images total, all multi-arch (amd64 + arm64).

| Package | Services | Use case |
|---|---|---|
| `openrfstack-node` | controller, acquisition, analysis, demod, speech | full-power host doing on-device classification/demod/transcription |
| `openrfstack-node-lite` | same as above | same, but on a <=1GB board |
| `openrfstack-mobile` | controller, acquisition, gps | drones / embedded — no analysis/demod/speech, light enough to fly |
| `openrfstack-mobile-lite` | same as above | same, but on a <=1GB board |
| `openrfstack-recon` | controller, acquisition, gps, recon (passive IQ capture on detection, GPS-tagged) | unattended field collection; captures pulled later for offline ID elsewhere |
| `openrfstack-recon-lite` | same as above | same, but on a <=1GB board |

All six pull from `ghcr.io/openrfstack/<package>:latest` and are public.

## The lite/default split

`-lite` and default are the same RPMs/binaries, built from the same
Containerfile with `--build-arg K3S_LITE=true|false`. The only difference is
which k3s subsystems are enabled at startup (see `entrypoint.sh`):

```
K3S_LITE_ARGS="--flannel-backend=none --disable=local-storage --disable-network-policy --disable-cloud-controller"
```

Artemis and PostgreSQL both run `hostNetwork: true` with `hostPath` volumes,
so they never need a CNI or a StorageClass — trimming those out cuts k3s's
idle RSS substantially on Raspberry-Pi-class boards with no functional
difference. `K3S_LITE` can still be overridden at `podman run` time with
`-e K3S_LITE=true|false` on either image if you want to mix and match.

## Why three node types and not one configurable image

Each node type installs and runs a different subset of SDR binaries (RPMs
pulled from each app's GitHub Releases) — it's a different process mix, not
just a flag:

- **sdr-node**: the only one running AnalysisApp/DemodApp/SpeechApp (does
  on-device signal ID, demodulation, and transcription).
- **mobile-node**: acquisition + GPS only, sized to run on something that
  flies.
- **recon-node**: acquisition + GPS + `sdr_recon.py`, a passive AMQP
  consumer that watches `rf.detections` and, on an SNR-triggered match,
  requests a dedicated IQ capture from `sdr_controller`, GPS-tags it, and
  writes it to disk for later offline identification on a more capable
  machine. Never classifies on-device.

## Running RTL-SDR and PlutoSDR together

All three node types' `devices.xml` ship with both `rtlsdr-0` and `pluto-0`
enabled, and `AcquisitionApp`'s band-splitting (see `main.cpp`) spreads the
configured sweep range across however many devices `sdr_controller` reports
online, so both run simultaneously with no extra config.

`pluto-0`'s `<uri>` is intentionally left empty — `RadioDevice::open()`
calls `SoapySDR::Device::enumerate()` when no uri is configured, which lets
SoapyPlutoSDR's own discovery run: USB scan first, then zeroconf (mDNS).
No IP is hand-maintained anywhere. This requires two things the image now
provides:

- libiio built with `-DHAVE_DNS_SD=ON` (its zeroconf scan backend is
  otherwise compiled out entirely — confirmed via `Unable to scan ip: -19`
  before this was enabled).
- `dbus-daemon --system` and `avahi-daemon` running (entrypoint.sh starts
  both before k3s). The ADALM-PLUTO's own firmware already advertises
  itself via mDNS out of the box, so a network-attached Pluto on the same
  L2 segment is found with zero configuration on either side.

If mDNS is ever unavailable on your network (some routers block multicast
across VLANs), SoapyPlutoSDR still falls back to a `PLUTO_IP` env var —
e.g. `-e PLUTO_IP=192.168.1.253` — as a manual override, but that's an
escape hatch, not the default path.

## RX gain: AGC vs. manual

Each `<device>` in `devices.xml` has `<rx_agc>` and `<rx_gain_db>`:

- `<rx_agc>true</rx_agc>` — hardware AGC (`SoapySDR::Device::setGainMode`)
  picks gain per-channel automatically. Default for all shipped configs,
  since signal strength varies (different antennas, distances, environments).
- `<rx_agc>false</rx_agc>` — fixed manual gain at `<rx_gain_db>`, for
  repeatable/deterministic captures with known signal levels. `<rx_gain_db>`
  is ignored while `rx_agc` is `true` but stays in the file so you can flip
  back without re-adding it.

Both fields default to `rx_agc=false`, `rx_gain_db=30` if omitted entirely.
Wired through `ConfigParser` → `RadioDevice::setRxGain()` in
`SdrResourceManager`.

## Generating devices.xml interactively

`../configure_devices.py` (repo root) walks through adding one or more SDR
devices — PlutoSDR and RTL-SDR presets you can accept or override, or a
custom SoapySDR driver — plus a sweep frequency range, then writes
`devices.xml` into one of the three `deploy/<node-type>/configs/`
directories (or a custom path, for mounting at runtime with
`-v /path/to/devices.xml:/etc/sdr/devices.xml:ro,z`). It can optionally patch
the matching `scanner.xml`'s `<start_hz>`/`<stop_hz>` to the same range.
Existing files are backed up to `.bak` before being overwritten.

```
./configure_devices.py
```

No external dependencies — plain `python3`.

## Known k3s-in-container gotchas (already worked around)

Running k3s rootful-in-a-container on a Pi hits two kubelet startup bugs,
both fixed in `entrypoint.sh`:

1. **`cgroup_disable=memory` from Pi firmware** — append
   `cgroup_enable=memory cgroup_memory=1` to `/boot/firmware/cmdline.txt` on
   the host (kernel-level, can't be fixed from inside the container).
2. **kubelet `ContainerManager` fails to start** (`permission denied` on
   `/proc/sys/*`) — fixed via
   `--kubelet-arg="feature-gates=KubeletInUserNamespace=true"`.
3. **Node stuck `NotReady` forever** when `K3S_LITE=true` disables flannel —
   nothing writes a CNI conf, and kubelet gates readiness on one existing
   even though every pod here uses `hostNetwork: true` and never invokes
   CNI. Fixed by dropping a dummy loopback CNI conf to `/etc/cni/net.d/`.
4. **`AcquisitionApp` silently hangs on startup, never submits a scan task** —
   its controller-discovery probe sends a `HEALTH_QUERY` before Artemis has
   actually finished booting. If Artemis's image pull is slow (5-8+ min over
   a weak/cellular link), `entrypoint.sh`'s old 180s rollout-status timeout
   gave up and started the SDR app processes anyway; the probe's HEALTH_QUERY
   then timed out and the process hung tearing down that AMQP channel,
   leaving `sdr_acquisition`/`sdr_controller` running but idle (zero CPU, no
   `rf.detections` messages ever sent). Fixed by bumping the rollout-status
   timeout to 600s so the SDR services genuinely wait for Artemis/Postgres
   instead of racing them. If you ever see acquisition logs go silent right
   after `[AmqpPublisher] connected`, this is almost certainly it — killing
   the `sdr_controller`/`sdr_acquisition`/`sdr_gps` processes (the supervisor
   loop respawns them in ~3s) once Artemis is confirmed `1/1 Running` is the
   immediate workaround.

Also requires **rootful podman** (`sudo podman run ...`), not rootless.
Tried dropping `--cgroupns=host` (which forces the container to see the
*host's* real cgroup tree, where a rootless uid has no write access) — that
got past the first error, but rootless then failed with `failed to find
cpuset cgroup (v2)`: the Pi's systemd user session doesn't delegate the
`cpuset` controller to user slices by default. Fixing that needs a host-side
systemd drop-in (`systemctl --user` delegate config), not a container-side
change, so rootful remains the supported path for now.
