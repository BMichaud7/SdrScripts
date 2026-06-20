#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# OpenRFStack node entrypoint
# 1. Loads node.conf to determine which services to run.
# 2. Installs SDR RPMs from GitHub Releases if not already present.
# 3. Copies default configs to /etc/sdr/ if not already there.
# 4. Starts k3s server; deploys Artemis and PostgreSQL via k3s.
# 5. Starts enabled SDR services as supervised background processes.
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

MANIFEST_DIR=/opt/sdr-node/k8s
CONFIG_DIR=/opt/sdr-node/configs
SDR_ETC=/etc/sdr
SDR_DATA=/var/lib/sdr
LOG_DIR=/var/log/sdr

K3S_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export KUBECONFIG=$K3S_KUBECONFIG

GRN='\033[0;32m'; BLU='\033[0;34m'; YLW='\033[0;33m'; RST='\033[0m'
log()  { echo -e "${BLU}[sdr-node]${RST} $*"; }
ok()   { echo -e "${GRN}[sdr-node]${RST} $*"; }
warn() { echo -e "${YLW}[sdr-node]${RST} $*"; }

# ── Load node config ──────────────────────────────────────────────────────────
NODE_CONF="${SDR_ETC}/node.conf"
[[ ! -f "$NODE_CONF" ]] && NODE_CONF="$CONFIG_DIR/node.conf"

ENABLE_SDR_CONTROLLER=true
ENABLE_SDR_ACQUISITION=true
ENABLE_SDR_ANALYSIS=true
ENABLE_SDR_DEMOD=true
ENABLE_SDR_SPEECH=true
ENABLE_SDR_GPS=false
REPO_SDR_CONTROLLER=OpenRFStack/SdrResourceManager
REPO_SDR_ACQUISITION=OpenRFStack/AcquisitionApp
REPO_SDR_ANALYSIS=OpenRFStack/AnalysisApp
REPO_SDR_DEMOD=OpenRFStack/DemodApp
REPO_SDR_SPEECH=OpenRFStack/SpeechApp
REPO_SDR_GPS=OpenRFStack/GpsApp
RELEASE_TAG=latest-main

if [[ -f "$NODE_CONF" ]]; then
    log "Loading config: $NODE_CONF"
    # shellcheck disable=SC1090
    source <(grep -v '^\s*#' "$NODE_CONF" | grep '=')
fi

# ── Data / log directories ─────────────────────────────────────────────────────
mkdir -p "$SDR_DATA"/{pgdata,transcripts,demod-output,speech-output,acq-cache}
mkdir -p "$SDR_ETC" "$LOG_DIR"

# ── RPM install (skipped if all enabled binaries already present) ─────────────
declare -A SERVICE_MAP=(
    [sdr_controller]="${REPO_SDR_CONTROLLER}:${ENABLE_SDR_CONTROLLER}"
    [sdr_acquisition]="${REPO_SDR_ACQUISITION}:${ENABLE_SDR_ACQUISITION}"
    [sdr_analysis]="${REPO_SDR_ANALYSIS}:${ENABLE_SDR_ANALYSIS}"
    [sdr_demod]="${REPO_SDR_DEMOD}:${ENABLE_SDR_DEMOD}"
    [sdr_speech]="${REPO_SDR_SPEECH}:${ENABLE_SDR_SPEECH}"
    [sdr_gps]="${REPO_SDR_GPS}:${ENABLE_SDR_GPS}"
)

need_install=0
for bin in "${!SERVICE_MAP[@]}"; do
    IFS=: read -r _repo enabled <<< "${SERVICE_MAP[$bin]}"
    [[ "$enabled" != "true" ]] && continue
    [[ ! -x /usr/bin/$bin ]] && { need_install=1; break; }
done

if [[ $need_install -eq 1 ]]; then
    log "SDR binaries not found — pulling RPMs (tag: ${RELEASE_TAG}) …"
    mkdir -p /tmp/sdr-rpms
    RPM_ARCH=$(uname -m)  # x86_64 or aarch64
    for bin in "${!SERVICE_MAP[@]}"; do
        IFS=: read -r repo enabled <<< "${SERVICE_MAP[$bin]}"
        [[ "$enabled" != "true" ]] && continue
        log "  Downloading from $repo …"
        gh release download "$RELEASE_TAG" \
            --repo "$repo" \
            --pattern "*.${RPM_ARCH}.rpm" \
            --dir /tmp/sdr-rpms \
            --clobber 2>/dev/null \
        || warn "  No release found for $repo — skipping"
    done
    if ls /tmp/sdr-rpms/*.rpm &>/dev/null; then
        log "Installing RPMs …"
        dnf install -y /tmp/sdr-rpms/*.rpm
        ok "RPMs installed"
    else
        warn "No RPMs downloaded — services may not start"
    fi
    rm -rf /tmp/sdr-rpms
else
    ok "SDR binaries already installed — skipping RPM download"
fi

# ── Whisper model (optional auto-download) ────────────────────────────────────
# Set WHISPER_MODEL=base.en (or tiny.en, small.en, medium.en) to auto-download.
# Or mount a pre-downloaded model: -v /path/to/models:/etc/sdr-speech/models:ro,z
WHISPER_MODEL_DIR=/etc/sdr-speech/models
mkdir -p "$WHISPER_MODEL_DIR"
if [[ -n "${WHISPER_MODEL:-}" ]]; then
    MODEL_FILE="$WHISPER_MODEL_DIR/ggml-${WHISPER_MODEL}.bin"
    if [[ ! -f "$MODEL_FILE" ]]; then
        log "Downloading whisper model: ${WHISPER_MODEL} …"
        curl -fL \
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${WHISPER_MODEL}.bin" \
            -o "$MODEL_FILE" \
        && ok "Whisper model downloaded: $MODEL_FILE" \
        || warn "Failed to download whisper model — speech transcription disabled"
    else
        ok "Whisper model already present: $MODEL_FILE"
    fi
fi

# ── Default configs ───────────────────────────────────────────────────────────
for cfg in devices.xml scanner.xml analysis.xml node.conf; do
    [[ ! -f "$SDR_ETC/$cfg" ]] && cp "$CONFIG_DIR/$cfg" "$SDR_ETC/$cfg" \
        && log "Installed default config: $SDR_ETC/$cfg"
done

# demod, speech, and gps use their own config dirs
mkdir -p /etc/sdr-demod /etc/sdr-speech /etc/sdr-gps
[[ ! -f /etc/sdr-demod/demod.xml ]] && cp "$CONFIG_DIR/demod.xml" /etc/sdr-demod/demod.xml \
    && log "Installed default config: /etc/sdr-demod/demod.xml"
[[ ! -f /etc/sdr-speech/speech.xml ]] && cp "$CONFIG_DIR/speech.xml" /etc/sdr-speech/speech.xml \
    && log "Installed default config: /etc/sdr-speech/speech.xml"
[[ ! -f /etc/sdr-gps/gps.xml ]] && cp "$CONFIG_DIR/gps.xml" /etc/sdr-gps/gps.xml \
    && log "Installed default config: /etc/sdr-gps/gps.xml"

# ── D-Bus + Avahi (zeroconf so SoapyPlutoSDR can find a network-attached
#    PlutoSDR via mDNS with no IP hand-maintained anywhere — see
#    RadioDevice::open() in SdrResourceManager and deploy/README.md) ─────────
mkdir -p /var/run/dbus
[[ ! -S /var/run/dbus/system_bus_socket ]] && dbus-daemon --system --fork
avahi-daemon --no-chroot --no-drop-root -D >> "$LOG_DIR/avahi.log" 2>&1 \
    && ok "avahi-daemon started (zeroconf PlutoSDR discovery)" \
    || warn "avahi-daemon failed to start — PlutoSDR discovery falls back to USB/PLUTO_IP"

# ── Start k3s ─────────────────────────────────────────────────────────────────
K3S_LITE_ARGS=""
if [[ "${K3S_LITE:-false}" == "true" ]]; then
    # Artemis/Postgres pods both run hostNetwork:true and mount hostPath
    # volumes (no PVC/StorageClass), so the CNI and local-path-provisioner
    # are dead weight here. Cuts k3s's idle RSS substantially on <=1GB boards.
    # coredns is disabled too: it's the only stock k3s pod that ISN'T
    # hostNetwork:true, so it's also the only one that actually needs a real
    # CNI plugin (not just a conf file) to get a sandbox -- and nothing here
    # does cluster-DNS lookups (every SDR service talks to localhost). Without
    # this it crash-loops forever on "failed to find plugin loopback in path
    # [/opt/cni/bin]" since flannel (which normally installs that binary) is
    # disabled above.
    K3S_LITE_ARGS="--flannel-backend=none --disable=local-storage --disable-network-policy --disable-cloud-controller --disable=coredns"
    log "K3S_LITE=true — flannel/local-storage/network-policy/cloud-controller/coredns disabled"
    # All SDR pods run hostNetwork:true (no real CNI plumbing needed), but
    # kubelet still gates node Ready on a CNI conf being present — without
    # flannel to write one, it sits in NetworkPluginNotReady forever.
    mkdir -p /etc/cni/net.d
    cat > /etc/cni/net.d/100-loopback.conf <<'CNIEOF'
{
  "cniVersion": "0.4.0",
  "name": "lo",
  "type": "loopback"
}
CNIEOF
fi

# ── Detect a k3s-mode switch on a reused data volume ──────────────────────────
# Switching K3S_LITE (lite<->full) on a volume that still has k3s server
# state from the OTHER mode (different CNI/flannel/snapshotter setup)
# crashes crun with "setns mnt: Bad file descriptor" on startup. Previously
# this needed a manual `podman volume rm sdr-k3s` to recover. Detect the
# switch via a marker file and wipe just the k3s data dir automatically --
# same cost as the manual workaround (fresh server + image re-pull), but
# without needing an operator to notice and intervene.
K3S_DATA_DIR=/var/lib/rancher/k3s
K3S_MODE_MARKER="$K3S_DATA_DIR/.sdr_k3s_lite_mode"
CURRENT_K3S_MODE="${K3S_LITE:-false}"
if [[ -d "$K3S_DATA_DIR" ]] && [[ -n "$(ls -A "$K3S_DATA_DIR" 2>/dev/null)" ]]; then
    PREV_K3S_MODE=$(cat "$K3S_MODE_MARKER" 2>/dev/null || echo "")
    if [[ "$PREV_K3S_MODE" != "$CURRENT_K3S_MODE" ]]; then
        warn "k3s mode changed ('${PREV_K3S_MODE:-unknown}' -> '$CURRENT_K3S_MODE') on a reused data dir — wiping $K3S_DATA_DIR to avoid a crun setns crash"
        # $K3S_DATA_DIR is itself a bind-mounted volume root when run under
        # podman/k8s with a named volume -- `rm -rf` on the mount point
        # fails with "Device or resource busy" (confirmed on real hardware
        # during round-2 verification). Clear its contents instead.
        find "$K3S_DATA_DIR" -mindepth 1 -delete
    fi
fi
mkdir -p "$K3S_DATA_DIR"
echo "$CURRENT_K3S_MODE" > "$K3S_MODE_MARKER"

log "Starting k3s server …"
# shellcheck disable=SC2086
k3s server \
    --disable=traefik \
    --disable=servicelb \
    --disable=metrics-server \
    --snapshotter=native \
    --data-dir=/var/lib/rancher/k3s \
    --kubelet-arg="feature-gates=KubeletInUserNamespace=true" \
    ${K3S_LITE_ARGS} \
    &
K3S_PID=$!

log "Waiting for k3s to be ready …"
until k3s kubectl get nodes &>/dev/null 2>&1; do sleep 2; done
ok "k3s ready"

# ── Deploy infrastructure via k3s ─────────────────────────────────────────────
log "Applying infrastructure manifests …"
k3s kubectl apply -f "$MANIFEST_DIR/00-namespace.yaml"
k3s kubectl apply -f "$MANIFEST_DIR/01-artemis.yaml"
k3s kubectl apply -f "$MANIFEST_DIR/02-postgres.yaml"

log "Waiting for Artemis and PostgreSQL to be ready …"
# 600s, not 180s: a cold image pull of Artemis/Postgres over a slow/cellular
# link can take 5-8+ minutes. A premature timeout here used to let the SDR
# app processes start before the broker was reachable — AcquisitionApp's
# controller-discovery HEALTH_QUERY would then time out and the process
# would hang waiting on AMQP channel teardown, never submitting a scan task.
k3s kubectl rollout status deployment/artemis -n sdr-system --timeout=600s \
    || warn "Artemis not ready yet — continuing"
k3s kubectl rollout status deployment/postgres -n sdr-system --timeout=600s \
    || warn "PostgreSQL not ready yet — continuing"
ok "Infrastructure ready"

# k3s's own internal bootstrap (creating its embedded CRDs, e.g.
# etcdsnapshotfiles.k3s.cattle.io) is still finishing in the background even
# after `kubectl get nodes` succeeds and Artemis/Postgres roll out — on a weak
# ARM board, immediately launching 5 CPU-heavy SDR binaries (controller,
# acquisition, analysis, demod, speech) starves that background work just
# enough to lose a race against k3s's own embedded apiserver proxy port,
# which is fatal to the k3s process (and thus the whole container). A short
# settle delay here lets k3s finish bootstrapping under low load first.
sleep 10

# Clean up legacy SDR k8s deployments if present from a previous image version
for svc in sdr-controller sdr-acquisition sdr-analysis sdr-demod sdr-speech; do
    k3s kubectl delete deployment "$svc" -n sdr-system --ignore-not-found 2>/dev/null || true
done

# ── Start SDR services as supervised processes ────────────────────────────────
# Run binaries directly in this container to avoid nested-container glibc conflicts.
SDR_PIDS=()

start_service() {
    local name=$1 bin=$2; shift 2
    local args=("$@")
    if [[ ! -x "/usr/bin/$bin" ]]; then
        warn "Binary /usr/bin/$bin not found — skipping $name"
        return
    fi
    log "Starting $name …"
    (
        set +e
        while true; do
            "/usr/bin/$bin" "${args[@]}" >> "$LOG_DIR/${name}.log" 2>&1
            rc=$?
            warn "$name exited (code $rc) — restarting in 3s"
            sleep 3
        done
    ) &
    SDR_PIDS+=($!)
    ok "$name started (pid ${SDR_PIDS[-1]})"
}

[[ "$ENABLE_SDR_CONTROLLER"  == "true" ]] && start_service sdr-controller  sdr_controller  "$SDR_ETC/devices.xml"
[[ "$ENABLE_SDR_ACQUISITION" == "true" ]] && start_service sdr-acquisition sdr_acquisition "$SDR_ETC/scanner.xml"
[[ "$ENABLE_SDR_ANALYSIS"    == "true" ]] && start_service sdr-analysis    sdr_analysis    "$SDR_ETC/analysis.xml"
[[ "$ENABLE_SDR_DEMOD"       == "true" ]] && start_service sdr-demod       sdr_demod  /etc/sdr-demod/demod.xml
[[ "$ENABLE_SDR_SPEECH"      == "true" ]] && start_service sdr-speech      sdr_speech /etc/sdr-speech/speech.xml
[[ "$ENABLE_SDR_GPS"         == "true" ]] && start_service sdr-gps         sdr_gps    /etc/sdr-gps/gps.xml

ok "All services started"
k3s kubectl get pods -n sdr-system

# Keep container alive — exit if k3s dies
wait $K3S_PID
