#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# OpenRFStack node entrypoint
# 1. Installs SDR RPMs from GitHub Releases if not already present.
# 2. Copies default configs to /etc/sdr/ if not already there.
# 3. Starts k3s server.
# 4. Applies k8s manifests (Artemis → Postgres → SDR services).
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

MANIFEST_DIR=/opt/sdr-node/k8s
CONFIG_DIR=/opt/sdr-node/configs
SDR_ETC=/etc/sdr
SDR_DATA=/var/lib/sdr

K3S_KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export KUBECONFIG=$K3S_KUBECONFIG

GRN='\033[0;32m'; BLU='\033[0;34m'; YLW='\033[0;33m'; RST='\033[0m'
log()  { echo -e "${BLU}[sdr-node]${RST} $*"; }
ok()   { echo -e "${GRN}[sdr-node]${RST} $*"; }
warn() { echo -e "${YLW}[sdr-node]${RST} $*"; }

# ── Data directories ──────────────────────────────────────────────────────────
mkdir -p "$SDR_DATA"/{pgdata,transcripts,demod-output}
mkdir -p "$SDR_ETC"

# ── RPM install (skipped if binaries already present) ────────────────────────
need_install=0
for bin in sdr_controller sdr_acquisition sdr_analysis sdr_demod sdr_speech; do
    if [[ ! -x /usr/bin/$bin ]]; then
        need_install=1
        break
    fi
done

if [[ $need_install -eq 1 ]]; then
    log "SDR binaries not found — pulling latest RPMs from GitHub Releases …"
    mkdir -p /tmp/sdr-rpms

    declare -A REPOS=(
        [sdr-controller]=OpenRFStack/SdrResourceManager
        [sdr-acquisition]=OpenRFStack/AcquisitionApp
        [sdr-analysis]=OpenRFStack/AnalysisApp
        [sdr-demod]=OpenRFStack/DemodApp
        [sdr-speech]=OpenRFStack/SpeechApp
    )

    for pkg in "${!REPOS[@]}"; do
        repo="${REPOS[$pkg]}"
        log "  Downloading $pkg from $repo …"
        if ! gh release download latest-main \
                --repo "$repo" \
                --pattern "*.rpm" \
                --dir /tmp/sdr-rpms \
                --clobber 2>/dev/null; then
            warn "  No release found for $repo — skipping $pkg"
        fi
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

# ── Default configs (copy only if not already present via volume mount) ───────
for cfg in devices.xml scanner.xml analysis.xml; do
    if [[ ! -f "$SDR_ETC/$cfg" ]]; then
        cp "$CONFIG_DIR/$cfg" "$SDR_ETC/$cfg"
        log "Installed default config: $SDR_ETC/$cfg"
    fi
done

# ── Start k3s ─────────────────────────────────────────────────────────────────
log "Starting k3s server …"

# native snapshotter is required when k3s runs inside a container
k3s server \
    --disable=traefik \
    --disable=servicelb \
    --disable=metrics-server \
    --snapshotter=native \
    --data-dir=/var/lib/rancher/k3s \
    &
K3S_PID=$!

# Wait for k3s API server
log "Waiting for k3s to be ready …"
until k3s kubectl get nodes &>/dev/null 2>&1; do
    sleep 2
done
ok "k3s ready"

# ── Apply manifests in order ──────────────────────────────────────────────────
log "Applying manifests …"

k3s kubectl apply -f "$MANIFEST_DIR/00-namespace.yaml"

# Infrastructure first
k3s kubectl apply -f "$MANIFEST_DIR/01-artemis.yaml"
k3s kubectl apply -f "$MANIFEST_DIR/02-postgres.yaml"

log "Waiting for Artemis and PostgreSQL to be ready …"
k3s kubectl wait --for=condition=ready pod -l app=artemis \
    -n sdr-system --timeout=120s
k3s kubectl wait --for=condition=ready pod -l app=postgres \
    -n sdr-system --timeout=120s
ok "Infrastructure ready"

# SDR services
for manifest in \
    03-sdr-controller.yaml \
    04-sdr-acquisition.yaml \
    05-sdr-analysis.yaml \
    06-sdr-demod.yaml \
    07-sdr-speech.yaml
do
    [[ -f "$MANIFEST_DIR/$manifest" ]] && k3s kubectl apply -f "$MANIFEST_DIR/$manifest"
done

ok "All manifests applied"
k3s kubectl get pods -n sdr-system

# Keep the container alive — k3s handles its own children
wait $K3S_PID
