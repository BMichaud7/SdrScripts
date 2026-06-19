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
    K3S_LITE_ARGS="--flannel-backend=none --disable=local-storage --disable-network-policy --disable-cloud-controller"
    log "K3S_LITE=true — flannel/local-storage/network-policy/cloud-controller disabled"
fi
log "Starting k3s server (port ${K3S_HTTPS_PORT}) …"
# shellcheck disable=SC2086
k3s server \
    --disable=traefik \
    --disable=servicelb \
    --disable=metrics-server \
    --snapshotter=native \
    --data-dir=/var/lib/rancher/k3s \
    --https-listen-port="${K3S_HTTPS_PORT}" \
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
k3s kubectl rollout status deployment/artemis -n sdr-system --timeout=180s \
    || warn "Artemis not ready yet — continuing"
k3s kubectl rollout status deployment/postgres -n sdr-system --timeout=180s \
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
