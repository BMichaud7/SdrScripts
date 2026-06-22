#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# OpenRFStack mobile node entrypoint (lightweight — drone/embedded)
# Enabled services: sdr_controller, sdr_acquisition, sdr_gps
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
log()  { echo -e "${BLU}[mobile-node]${RST} $*"; }
ok()   { echo -e "${GRN}[mobile-node]${RST} $*"; }
warn() { echo -e "${YLW}[mobile-node]${RST} $*"; }

# ── Load node config ──────────────────────────────────────────────────────────
NODE_CONF="${SDR_ETC}/node.conf"
[[ ! -f "$NODE_CONF" ]] && NODE_CONF="$CONFIG_DIR/node.conf"

ENABLE_SDR_CONTROLLER=true
ENABLE_SDR_ACQUISITION=true
ENABLE_SDR_ANALYSIS=false
ENABLE_SDR_DEMOD=false
ENABLE_SDR_SPEECH=false
ENABLE_SDR_GPS=true
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
mkdir -p "$SDR_DATA"/{pgdata,gps,acq-cache}
mkdir -p "$SDR_ETC" "$LOG_DIR"

# ── RPM install ───────────────────────────────────────────────────────────────
declare -A SERVICE_MAP=(
    [sdr_controller]="${REPO_SDR_CONTROLLER}:${ENABLE_SDR_CONTROLLER}"
    [sdr_acquisition]="${REPO_SDR_ACQUISITION}:${ENABLE_SDR_ACQUISITION}"
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

# ── AnalysisApp ONNX model (optional auto-download) ────────────────────────────
# AnalysisApp's OnnxClassifier is a no-op with an empty model_path (see
# analysis.xml) -- nothing ever populated one, so AMR classification has
# been silently disabled in every shipped container until now. Set
# ANALYSIS_MODEL=amr_cnn_24class (the default; matches analysis.xml's
# model_path/classes_path) to auto-fetch the latest models-* release from
# OpenRFStack/AnalysisApp (see tools/ml/publish_models_release.sh there).
# Or mount a pre-downloaded model: -v /path/to/models:/etc/sdr-analysis/models:ro,z
# ENABLE_SDR_ANALYSIS defaults to false here, so this is inert by default.
if [[ "$ENABLE_SDR_ANALYSIS" == "true" ]]; then
    ANALYSIS_MODEL="${ANALYSIS_MODEL:-amr_cnn_24class}"
    ANALYSIS_MODEL_DIR=/etc/sdr-analysis/models
    mkdir -p "$ANALYSIS_MODEL_DIR"
    ONNX_FILE="$ANALYSIS_MODEL_DIR/${ANALYSIS_MODEL}.onnx"
    CLASSES_FILE="$ANALYSIS_MODEL_DIR/${ANALYSIS_MODEL}.classes.json"
    if [[ ! -f "$ONNX_FILE" || ! -f "$CLASSES_FILE" ]]; then
        MODEL_TAG=$(gh release list --repo OpenRFStack/AnalysisApp --json tagName \
            -q '[.[] | select(.tagName | startswith("models-"))][0].tagName' 2>/dev/null || true)
        if [[ -n "$MODEL_TAG" ]]; then
            log "Downloading AMR model ${ANALYSIS_MODEL} from release ${MODEL_TAG} …"
            gh release download "$MODEL_TAG" --repo OpenRFStack/AnalysisApp \
                --pattern "${ANALYSIS_MODEL}.onnx*" \
                --pattern "${ANALYSIS_MODEL}.classes.json" \
                --dir "$ANALYSIS_MODEL_DIR" --clobber 2>/dev/null \
            && ok "AMR model downloaded: $ONNX_FILE" \
            || warn "Failed to download AMR model — classification disabled"
        else
            warn "No models-* release found for OpenRFStack/AnalysisApp — classification disabled"
        fi
    else
        ok "AMR model already present: $ONNX_FILE"
    fi
fi

# ── Default configs ───────────────────────────────────────────────────────────
for cfg in devices.xml scanner.xml node.conf; do
    [[ ! -f "$SDR_ETC/$cfg" ]] && cp "$CONFIG_DIR/$cfg" "$SDR_ETC/$cfg" \
        && log "Installed default config: $SDR_ETC/$cfg"
done

mkdir -p /etc/sdr-gps
[[ ! -f /etc/sdr-gps/gps.xml ]] && cp "$CONFIG_DIR/gps.xml" /etc/sdr-gps/gps.xml \
    && log "Installed default config: /etc/sdr-gps/gps.xml"

# ── GPS daemon (gpsd) ────────────────────────────────────────────────────────
GPS_SERIAL_DEVICE=${GPS_SERIAL_DEVICE:-/dev/ttyACM0}
if [[ "$ENABLE_SDR_GPS" == "true" && -e "$GPS_SERIAL_DEVICE" ]]; then
    log "Starting gpsd on ${GPS_SERIAL_DEVICE} …"
    gpsd -N -n "$GPS_SERIAL_DEVICE" -F /var/run/gpsd.sock >> "$LOG_DIR/gpsd.log" 2>&1 &
    ok "gpsd started"
elif [[ "$ENABLE_SDR_GPS" == "true" ]]; then
    warn "GPS enabled but ${GPS_SERIAL_DEVICE} not found — sdr-gps will retry gpsd connection"
fi

# ── D-Bus + Avahi (zeroconf so SoapyPlutoSDR can find a network-attached
#    PlutoSDR via mDNS with no IP hand-maintained anywhere — see
#    RadioDevice::open() in SdrResourceManager and deploy/README.md) ─────────
mkdir -p /var/run/dbus
[[ ! -S /var/run/dbus/system_bus_socket ]] && dbus-daemon --system --fork
avahi-daemon --no-chroot --no-drop-root -D >> "$LOG_DIR/avahi.log" 2>&1 \
    && ok "avahi-daemon started (zeroconf PlutoSDR discovery)" \
    || warn "avahi-daemon failed to start — PlutoSDR discovery falls back to USB/PLUTO_IP"

# ── Start k3s ─────────────────────────────────────────────────────────────────
# K3S_HTTPS_PORT defaults to 6444 so mobile can coexist with sdr-node (6443)
# on the same host without a port conflict. Override with -e K3S_HTTPS_PORT=6443
# when running mobile standalone.
K3S_HTTPS_PORT=${K3S_HTTPS_PORT:-6444}
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
    # kube-proxy and helm-controller are also dead weight: there are no
    # kind: Service or HelmChart objects anywhere in this repo (every pod
    # is hostNetwork:true, deployed as plain manifests), and kube-proxy's
    # iptables-restore calls fail forever on nf_tables-only kernels (no
    # legacy xt_*/ip_tables modules -- confirmed on both x86_64 and arm64
    # hosts) -- an infinite ~10-30s retry loop that was severe enough on a
    # <=1GB board to starve out the rest of k3s's own bootstrap (apiserver
    # never reached Ready). Verified fix: same image, same board, node
    # Ready in 24s with these two flags added vs. never reaching Ready
    # without them.
    K3S_LITE_ARGS="--flannel-backend=none --disable=local-storage --disable-network-policy --disable-cloud-controller --disable=coredns --disable-kube-proxy --disable-helm-controller"
    log "K3S_LITE=true — flannel/local-storage/network-policy/cloud-controller/coredns/kube-proxy/helm-controller disabled"
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

log "Starting k3s server (port ${K3S_HTTPS_PORT}) …"
# shellcheck disable=SC2086
k3s server \
    --disable=traefik \
    --disable=servicelb \
    --disable=metrics-server \
    --snapshotter=native \
    --data-dir=/var/lib/rancher/k3s \
    --https-listen-port="${K3S_HTTPS_PORT}" \
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

# ── Start SDR services ────────────────────────────────────────────────────────
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
[[ "$ENABLE_SDR_GPS"         == "true" ]] && start_service sdr-gps         sdr_gps         /etc/sdr-gps/gps.xml

ok "All services started"
k3s kubectl get pods -n sdr-system

# Keep container alive — exit if k3s dies
wait $K3S_PID
