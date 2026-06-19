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

Also requires **rootful podman** (`sudo podman run ...`), not rootless —
rootless can't satisfy kubelet's cgroup delegation (`mkdir
/sys/fs/cgroup/kubepods: permission denied`) even with `--privileged
--cgroupns=host`.
