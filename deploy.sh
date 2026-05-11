#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  deploy.sh — Build RPMs and deploy the SDR stack (native services + containers)
#
#  Containers managed here:
#    sdr-artemis    — ActiveMQ Artemis AMQP broker  (always containerised)
#    sdr-soapy      — SoapySDRServer for PlutoSDR   (always containerised)
#
#  Native systemd services (installed via RPM):
#    sdr-controller   — SdrResourceManager
#    sdr-acquisition  — AcquisitionApp
#    sdr-analysis     — AnalysisApp
#
#  Usage:
#    ./deploy.sh build          # build RPMs (requires podman + source repos)
#    ./deploy.sh install        # install RPMs + enable services (needs sudo)
#    ./deploy.sh start          # start containers + native services
#    ./deploy.sh stop           # stop services + containers
#    ./deploy.sh status         # show full stack status
#    ./deploy.sh restart        # stop then start
#    ./deploy.sh uninstall      # stop + remove RPMs (needs sudo)
#    ./deploy.sh logs <service> # journalctl for a native service
#
#    ./deploy.sh build install start   # full first-time setup in one go
#
#  Override env:
#    PLUTO_IP=192.168.2.1 ./deploy.sh start
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PLUTO_IP="${PLUTO_IP:-192.168.1.253}"
BROKER_USER="${BROKER_USER:-sdr_ctrl}"
BROKER_PASS="${BROKER_PASS:-sdr_hw_test}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(dirname "$SCRIPT_DIR")"
RPM_DIR="$SCRIPT_DIR/rpm"
RPM_OUT="$SCRIPT_DIR/rpms"

RPMBUILD_IMAGE="sdr-rpmbuild:1.0"
ARTEMIS_IMAGE="apache/activemq-artemis:latest-alpine"
SOAPY_IMAGE="soapy-pluto:1.0"

ARTEMIS_CTR="sdr-artemis"
SOAPY_CTR="sdr-soapy"

NATIVE_SERVICES=(sdr-controller sdr-acquisition sdr-analysis)
RPMS=(
    "sdr-controller"
    "sdr-acquisition"
    "sdr-analysis"
)

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
BLU='\033[0;34m'; RST='\033[0m'
info() { echo -e "${BLU}[deploy]${RST} $*"; }
ok()   { echo -e "${GRN}[deploy]${RST} $*"; }
warn() { echo -e "${YLW}[deploy]${RST} $*"; }
die()  { echo -e "${RED}[deploy]${RST} $*" >&2; exit 1; }

need_sudo() {
    [[ $EUID -eq 0 ]] && return 0
    command -v sudo >/dev/null || die "sudo not available and not running as root"
    sudo -n true 2>/dev/null || {
        info "sudo password required for system changes:"
        sudo true
    }
}

run_sudo() { [[ $EUID -eq 0 ]] && "$@" || sudo "$@"; }
is_running() { podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

wait_port() {
    local host="$1" port="$2" label="$3" max="${4:-30}"
    info "Waiting for $label ..."
    for i in $(seq 1 "$max"); do
        bash -c ">/dev/tcp/$host/$port" 2>/dev/null && { ok "$label ready"; return 0; }
        sleep 1
    done
    die "$label did not become ready in ${max}s"
}

# ── Build RPMs ────────────────────────────────────────────────────────────────

cmd_build() {
    info "Building RPM build image (this may take several minutes) ..."
    podman build \
        -t "$RPMBUILD_IMAGE" \
        -f "$RPM_DIR/Containerfile.rpmbuild" \
        "$SRC_ROOT"

    info "Extracting RPMs ..."
    mkdir -p "$RPM_OUT"
    local cid
    cid=$(podman create "$RPMBUILD_IMAGE")
    podman cp "$cid":/rpms/. "$RPM_OUT"/
    podman rm "$cid" >/dev/null

    ok "RPMs written to $RPM_OUT:"
    ls -lh "$RPM_OUT"/*.rpm 2>/dev/null || warn "No RPMs found in $RPM_OUT"
}

# ── Install RPMs ──────────────────────────────────────────────────────────────

cmd_install() {
    local rpms=("$RPM_OUT"/*.rpm)
    [[ ${#rpms[@]} -gt 0 && -f "${rpms[0]}" ]] || \
        die "No RPMs in $RPM_OUT — run: ./deploy.sh build"

    need_sudo
    info "Installing RPMs ..."
    run_sudo dnf install -y "${rpms[@]}"
    ok "RPMs installed"

    # Patch PLUTO_IP into the SoapySDR server URI in devices.xml if it differs
    local current_ip
    current_ip=$(grep -oP '(?<=PLUTO_IP=)[0-9.]+' /etc/sysconfig/sdr-controller 2>/dev/null || echo "")
    if [[ "$current_ip" != "$PLUTO_IP" ]]; then
        info "Patching PlutoSDR IP to $PLUTO_IP in /etc/sysconfig/sdr-controller ..."
        run_sudo bash -c "
            grep -q PLUTO_IP /etc/sysconfig/sdr-controller \
                && sed -i 's/^PLUTO_IP=.*/PLUTO_IP=$PLUTO_IP/' /etc/sysconfig/sdr-controller \
                || echo 'PLUTO_IP=$PLUTO_IP' >> /etc/sysconfig/sdr-controller"
    fi

    # Ensure runtime directories exist
    run_sudo install -d -m 750 -o sdr -g sdr /var/lib/sdr /run/sdr

    # Enable services (don't start yet — broker must be up first)
    run_sudo systemctl daemon-reload
    for svc in "${NATIVE_SERVICES[@]}"; do
        run_sudo systemctl enable "$svc"
    done
    ok "Services enabled (not yet started — run: ./deploy.sh start)"
}

# ── Start containers ──────────────────────────────────────────────────────────

start_artemis() {
    if is_running "$ARTEMIS_CTR"; then warn "Artemis already running"; return; fi
    info "Starting Artemis broker ..."
    podman run -d --rm --name "$ARTEMIS_CTR" --network=host \
        -e ARTEMIS_USER="$BROKER_USER" \
        -e ARTEMIS_PASSWORD="$BROKER_PASS" \
        "$ARTEMIS_IMAGE" >/dev/null
    wait_port localhost 5672 "Artemis AMQP" 40
}

start_soapy() {
    if is_running "$SOAPY_CTR"; then warn "SoapySDR server already running"; return; fi
    podman image exists "$SOAPY_IMAGE" || \
        die "SoapySDR server image $SOAPY_IMAGE not found.\nBuild it with: podman build -t $SOAPY_IMAGE -f hw-test/Containerfile.soapy-pluto $SRC_ROOT"

    info "Starting SoapySDR server (PlutoSDR @ $PLUTO_IP) ..."
    podman run -d --rm --name "$SOAPY_CTR" --network=host \
        -e PLUTO_IP="$PLUTO_IP" \
        "$SOAPY_IMAGE" >/dev/null
    wait_port localhost 55132 "SoapySDR server" 20
}

# ── Start / Stop native services ──────────────────────────────────────────────

start_native() {
    need_sudo
    for svc in "${NATIVE_SERVICES[@]}"; do
        if run_sudo systemctl is-active --quiet "$svc" 2>/dev/null; then
            warn "$svc already running"
        else
            info "Starting $svc ..."
            run_sudo systemctl start "$svc"
            ok "$svc started"
        fi
    done
}

stop_native() {
    need_sudo
    for svc in $(echo "${NATIVE_SERVICES[@]}" | tr ' ' '\n' | tac); do
        if run_sudo systemctl is-active --quiet "$svc" 2>/dev/null; then
            info "Stopping $svc ..."
            run_sudo systemctl stop "$svc"
            ok "$svc stopped"
        else
            warn "$svc not running"
        fi
    done
}

stop_container() {
    local ctr="$1" label="$2"
    if is_running "$ctr"; then
        info "Stopping $label ..."
        podman stop "$ctr" >/dev/null 2>&1 || true
        ok "$label stopped"
    else
        warn "$label not running"
    fi
}

# ── Status ────────────────────────────────────────────────────────────────────

cmd_status() {
    echo ""
    printf "%-22s %-10s %s\n" "Component" "State" "Detail"
    printf "%-22s %-10s %s\n" "---------" "-----" "------"

    # Containers
    for pair in "Artemis broker:$ARTEMIS_CTR" "SoapySDR server:$SOAPY_CTR"; do
        local label="${pair%%:*}" ctr="${pair##*:}"
        if is_running "$ctr"; then
            printf "${GRN}%-22s %-10s${RST} %s\n" "$label" "UP" "(container: $ctr)"
        else
            printf "${RED}%-22s %-10s${RST} %s\n" "$label" "DOWN" "(container: $ctr)"
        fi
    done

    # Native services
    for svc in "${NATIVE_SERVICES[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            local uptime
            uptime=$(systemctl show "$svc" --property=ActiveEnterTimestamp \
                     --value 2>/dev/null | cut -d' ' -f2-3 || echo "")
            printf "${GRN}%-22s %-10s${RST} since %s\n" "$svc" "RUNNING" "$uptime"
        elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            printf "${RED}%-22s %-10s${RST} (installed, not started)\n" "$svc" "STOPPED"
        else
            printf "${YLW}%-22s %-10s${RST} (not installed — run: ./deploy.sh install)\n" \
                "$svc" "NOT INSTALLED"
        fi
    done

    echo ""
    ping -c1 -W1 "$PLUTO_IP" >/dev/null 2>&1 \
        && ok  "PlutoSDR reachable at $PLUTO_IP" \
        || warn "PlutoSDR NOT reachable at $PLUTO_IP"
    echo ""
}

# ── Uninstall ─────────────────────────────────────────────────────────────────

cmd_uninstall() {
    need_sudo
    stop_native || true
    info "Removing RPMs ..."
    run_sudo dnf remove -y "${RPMS[@]}" 2>/dev/null || warn "Some packages were not installed"
    ok "Uninstalled"
}

# ── Logs ─────────────────────────────────────────────────────────────────────

cmd_logs() {
    local svc="${1:-}"
    case "$svc" in
        artemis|broker) podman logs -f "$ARTEMIS_CTR" ;;
        soapy)          podman logs -f "$SOAPY_CTR" ;;
        controller|acquisition|analysis)
            journalctl -fu "sdr-$svc" ;;
        sdr-controller|sdr-acquisition|sdr-analysis)
            journalctl -fu "$svc" ;;
        *) die "Usage: $0 logs <broker|soapy|controller|acquisition|analysis>" ;;
    esac
}

# ── Main ──────────────────────────────────────────────────────────────────────

[[ $# -eq 0 ]] && { "$0" help; exit 0; }

while [[ $# -gt 0 ]]; do
    cmd="$1"; shift
    case "$cmd" in
        build)     cmd_build ;;
        install)   cmd_install ;;
        start)
            start_artemis
            start_soapy
            start_native
            echo ""
            cmd_status
            ;;
        stop)
            stop_native  || true
            stop_container "$SOAPY_CTR"    "SoapySDR server"
            stop_container "$ARTEMIS_CTR"  "Artemis broker"
            ;;
        restart)
            "$0" stop
            sleep 2
            "$0" start
            ;;
        status)    cmd_status ;;
        uninstall) cmd_uninstall ;;
        logs)      cmd_logs "${1:-}"; shift || true ;;
        help|--help|-h)
            cat <<EOF

  ${BLU}deploy.sh${RST} — SDR stack native deployment

  ${YLW}Workflow:${RST}
    ./deploy.sh build install start     # first-time setup

  ${YLW}Commands:${RST}
    build          Build RPMs from source (uses podman build)
    install        Install RPMs + enable systemd services  [sudo]
    start          Start containers (Artemis, SoapySDR) + native services
    stop           Stop native services + containers
    restart        stop then start
    status         Show full stack status
    uninstall      Stop services + remove RPMs  [sudo]
    logs <svc>     Stream logs: broker | soapy | controller | acquisition | analysis

  ${YLW}Override env:${RST}
    PLUTO_IP=$PLUTO_IP  BROKER_USER=$BROKER_USER  BROKER_PASS=***

EOF
            ;;
        *) die "Unknown command: $cmd — run './deploy.sh help'" ;;
    esac
done
