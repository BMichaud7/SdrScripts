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
REPO_SDR_CONTROLLER=OpenRFStack/SdrResourceManager
REPO_SDR_ACQUISITION=OpenRFStack/AcquisitionApp
REPO_SDR_ANALYSIS=OpenRFStack/AnalysisApp
REPO_SDR_DEMOD=OpenRFStack/DemodApp
REPO_SDR_SPEECH=OpenRFStack/SpeechApp
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
    for bin in "${!SERVICE_MAP[@]}"; do
        IFS=: read -r repo enabled <<< "${SERVICE_MAP[$bin]}"
        [[ "$enabled" != "true" ]] && continue
        log "  Downloading from $repo …"
        gh release download "$RELEASE_TAG" \
            --repo "$repo" \
            --pattern "*.rpm" \
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
for cfg in devices.xml scanner.xml analysis.xml node.conf; do
    [[ ! -f "$SDR_ETC/$cfg" ]] && cp "$CONFIG_DIR/$cfg" "$SDR_ETC/$cfg" \
        && log "Installed default config: $SDR_ETC/$cfg"
done

# demod and speech use their own config dirs
mkdir -p /etc/sdr-demod /etc/sdr-speech
[[ ! -f /etc/sdr-demod/demod.xml ]] && cp "$CONFIG_DIR/demod.xml" /etc/sdr-demod/demod.xml \
    && log "Installed default config: /etc/sdr-demod/demod.xml"
[[ ! -f /etc/sdr-speech/speech.xml ]] && cp "$CONFIG_DIR/speech.xml" /etc/sdr-speech/speech.xml \
    && log "Installed default config: /etc/sdr-speech/speech.xml"

# ── Start k3s ─────────────────────────────────────────────────────────────────
log "Starting k3s server …"
k3s server \
    --disable=traefik \
    --disable=servicelb \
    --disable=metrics-server \
    --snapshotter=native \
    --data-dir=/var/lib/rancher/k3s \
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
        while true; do
            "/usr/bin/$bin" "${args[@]}" >> "$LOG_DIR/${name}.log" 2>&1
            warn "$name exited (code $?) — restarting in 3s"
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

ok "All services started"
k3s kubectl get pods -n sdr-system

# Keep container alive — exit if k3s dies
wait $K3S_PID
