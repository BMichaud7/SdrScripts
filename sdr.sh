#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  sdr.sh — SDR stack lifecycle manager
#
#  Usage:
#    ./sdr.sh start [services...]   # start all, or specific ones
#    ./sdr.sh stop  [services...]   # stop all, or specific ones
#    ./sdr.sh restart               # stop then start all
#    ./sdr.sh status                # show running containers + health
#    ./sdr.sh logs  <service>       # tail logs for a service
#    ./sdr.sh scanner               # launch Qt scanner (needs DISPLAY)
#    ./sdr.sh build [services...]   # rebuild images from source
#
#  Services: broker  controller  acquisition  analysis
#            (scanner is launched interactively, not as a daemon)
#
#  Override any variable via env:
#    PLUTO_IP=192.168.1.100 ./sdr.sh start
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PLUTO_IP="${PLUTO_IP:-192.168.1.253}"
BROKER_USER="${BROKER_USER:-sdr_ctrl}"
BROKER_PASS="${BROKER_PASS:-sdr_hw_test}"
DEST_IP="${DEST_IP:-127.0.0.1}"

# Container names
ARTEMIS_CTR="sdr-artemis"
CONTROLLER_CTR="sdr-controller"
ACQUISITION_CTR="sdr-acquisition"
ANALYSIS_CTR="sdr-analysis"

# Images
ARTEMIS_IMAGE="apache/activemq-artemis:latest-alpine"
CONTROLLER_IMAGE="sdr-controller-hw:2.0"
ACQUISITION_IMAGE="sdr-acquisition:hw-test"
ANALYSIS_IMAGE="sdr-analysis:hw-test"
SCANNER_IMAGE="sdr-scanner:1.0"

# Source roots (for build)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(dirname "$SCRIPT_DIR")"
HW_TEST_DIR="$SCRIPT_DIR/../hw-test"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; RST='\033[0m'
info()  { echo -e "${BLU}[sdr]${RST} $*"; }
ok()    { echo -e "${GRN}[sdr]${RST} $*"; }
warn()  { echo -e "${YLW}[sdr]${RST} $*"; }
die()   { echo -e "${RED}[sdr]${RST} $*" >&2; exit 1; }

# ── Helpers ───────────────────────────────────────────────────────────────────
is_running() { podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

wait_port() {
    local host="$1" port="$2" label="$3" max="${4:-30}"
    info "Waiting for $label on $host:$port ..."
    for i in $(seq 1 "$max"); do
        if bash -c ">/dev/tcp/$host/$port" 2>/dev/null; then
            ok "$label is ready"
            return 0
        fi
        sleep 1
    done
    die "$label did not become ready after ${max}s"
}

require_image() {
    podman image exists "$1" || die "Image $1 not found — run: ./sdr.sh build"
}

# ── Start functions ───────────────────────────────────────────────────────────

start_broker() {
    if is_running "$ARTEMIS_CTR"; then warn "broker already running"; return; fi
    info "Starting Artemis broker ..."
    podman run -d --rm --name "$ARTEMIS_CTR" --network=host \
        -e ARTEMIS_USER="$BROKER_USER" \
        -e ARTEMIS_PASSWORD="$BROKER_PASS" \
        "$ARTEMIS_IMAGE" >/dev/null
    wait_port localhost 5672 "Artemis AMQP" 40
}

start_controller() {
    if is_running "$CONTROLLER_CTR"; then warn "controller already running"; return; fi
    require_image "$CONTROLLER_IMAGE"

    local devices_xml
    devices_xml="$(find "$SCRIPT_DIR" -maxdepth 1 -name "devices*.xml" | head -1)"
    [[ -z "$devices_xml" ]] && devices_xml="$HW_TEST_DIR/devices-direct.xml"
    [[ -f "$devices_xml" ]] || die "No devices.xml found — expected at $HW_TEST_DIR/devices-direct.xml"

    # Patch PLUTO_IP in a temp copy if needed
    local cfg_path="$devices_xml"
    if ! grep -q "$PLUTO_IP" "$devices_xml" 2>/dev/null; then
        local tmp; tmp="$(mktemp --suffix=.xml)"
        sed "s|ip:[0-9.]*|ip:$PLUTO_IP|g" "$devices_xml" > "$tmp"
        cfg_path="$tmp"
        warn "Patched PlutoSDR IP to $PLUTO_IP in temp config"
    fi

    info "Starting controller (PlutoSDR @ $PLUTO_IP) ..."
    podman run -d --rm --name "$CONTROLLER_CTR" --network=host \
        -v "$cfg_path:/etc/sdr-controller/devices.xml:ro,z" \
        "$CONTROLLER_IMAGE" >/dev/null
    sleep 2
    if ! is_running "$CONTROLLER_CTR"; then
        podman logs "$CONTROLLER_CTR" 2>&1 | tail -10
        die "Controller failed to start"
    fi
    ok "Controller running"
}

start_acquisition() {
    if is_running "$ACQUISITION_CTR"; then warn "acquisition already running"; return; fi
    require_image "$ACQUISITION_IMAGE"

    local acq_xml
    acq_xml="$(find "$SCRIPT_DIR" -maxdepth 1 -name "scanner*.xml" | head -1)"
    [[ -z "$acq_xml" ]] && acq_xml="$HW_TEST_DIR/scanner.xml"
    [[ -f "$acq_xml" ]] || die "No scanner.xml found — expected at $HW_TEST_DIR/scanner.xml"

    info "Starting AcquisitionApp ..."
    podman run -d --rm --name "$ACQUISITION_CTR" --network=host \
        -v "$acq_xml:/etc/sdr-acquisition/scanner.xml:ro,z" \
        "$ACQUISITION_IMAGE" >/dev/null
    sleep 2
    is_running "$ACQUISITION_CTR" && ok "AcquisitionApp running" || die "AcquisitionApp failed to start"
}

start_analysis() {
    if is_running "$ANALYSIS_CTR"; then warn "analysis already running"; return; fi
    require_image "$ANALYSIS_IMAGE"

    local analysis_xml
    analysis_xml="$(find "$SCRIPT_DIR" -maxdepth 1 -name "analysis*.xml" | head -1)"
    [[ -z "$analysis_xml" ]] && analysis_xml="$HW_TEST_DIR/analysis.xml"
    [[ -f "$analysis_xml" ]] || die "No analysis.xml found — expected at $HW_TEST_DIR/analysis.xml"

    info "Starting AnalysisApp ..."
    podman run -d --rm --name "$ANALYSIS_CTR" --network=host \
        -v "$analysis_xml:/etc/sdr-analysis/analysis.xml:ro,z" \
        "$ANALYSIS_IMAGE" >/dev/null
    sleep 2
    is_running "$ANALYSIS_CTR" && ok "AnalysisApp running" || die "AnalysisApp failed to start"
}

# ── Stop functions ────────────────────────────────────────────────────────────

stop_one() {
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
    printf "%-20s %-8s %s\n" "Service" "State" "Image"
    printf "%-20s %-8s %s\n" "-------" "-----" "-----"

    status_row() {
        local ctr="$1" label="$2" image="$3"
        if is_running "$ctr"; then
            printf "${GRN}%-20s %-8s${RST} %s\n" "$label" "UP" "$image"
        else
            printf "${RED}%-20s %-8s${RST} %s\n" "$label" "DOWN" "$image"
        fi
    }

    status_row "$ARTEMIS_CTR"     "broker"      "$ARTEMIS_IMAGE"
    status_row "$CONTROLLER_CTR"  "controller"  "$CONTROLLER_IMAGE"
    status_row "$ACQUISITION_CTR" "acquisition" "$ACQUISITION_IMAGE"
    status_row "$ANALYSIS_CTR"    "analysis"    "$ANALYSIS_IMAGE"
    echo ""

    # Show broker queue depth if running
    if is_running "$ARTEMIS_CTR"; then
        local depth
        depth=$(podman exec "$ARTEMIS_CTR" \
            artemis queue stat --url tcp://localhost:61616 \
            --user "$BROKER_USER" --password "$BROKER_PASS" 2>/dev/null \
            | grep -c "sdr\." || true)
        info "Broker: $depth SDR queue(s) active"
    fi

    # Show PlutoSDR reachability
    if ping -c1 -W1 "$PLUTO_IP" >/dev/null 2>&1; then
        ok "PlutoSDR reachable at $PLUTO_IP"
    else
        warn "PlutoSDR NOT reachable at $PLUTO_IP"
    fi
    echo ""
}

# ── Build ─────────────────────────────────────────────────────────────────────

cmd_build() {
    local services=("${@:-controller acquisition analysis scanner}")
    for svc in "${services[@]}"; do
        case "$svc" in
            controller)
                info "Building controller image ..."
                podman build -t "$CONTROLLER_IMAGE" \
                    -f "$SRC_ROOT/hw-test/Containerfile.hw-controller" \
                    "$SRC_ROOT"
                ok "controller built"
                ;;
            acquisition)
                info "Building acquisition image ..."
                # Context is AcquisitionApp/ — siblings are cloned from GitHub in Containerfile
                podman build -t "$ACQUISITION_IMAGE" \
                    -f "$SRC_ROOT/AcquisitionApp/Containerfile" \
                    "$SRC_ROOT/AcquisitionApp"
                ok "acquisition built"
                ;;
            analysis)
                info "Building analysis image ..."
                # Context is AnalysisApp/ — siblings are cloned from GitHub in Containerfile
                podman build -t "$ANALYSIS_IMAGE" \
                    -f "$SRC_ROOT/AnalysisApp/Containerfile" \
                    "$SRC_ROOT/AnalysisApp"
                ok "analysis built"
                ;;
            scanner)
                info "Building scanner image ..."
                podman build -t "$SCANNER_IMAGE" \
                    -f "$SRC_ROOT/SdrScanner/Containerfile" \
                    "$SRC_ROOT/SdrScanner"
                ok "scanner built"
                ;;
            *) warn "Unknown service for build: $svc" ;;
        esac
    done
}

# ── CLI spectrum scan ─────────────────────────────────────────────────────────

cmd_scan() {
    local start_mhz="${1:-80}"
    local end_mhz="${2:-200}"
    local scan_script="$SRC_ROOT/hw-test/scan_80_200.py"
    [[ -f "$scan_script" ]] || die "Scan script not found: $scan_script"

    if ! is_running "$ARTEMIS_CTR" || ! is_running "$CONTROLLER_CTR"; then
        die "Stack not running — start first: ./sdr.sh start"
    fi

    info "Scanning ${start_mhz}–${end_mhz} MHz ..."
    podman run --rm --network=host \
        -v "$scan_script:/scan.py:ro,z" \
        sdr-controller:test-integ \
        bash -c "apt-get update -qq && apt-get install -y -qq python3-numpy 2>/dev/null && \
                 python3 /scan.py"
}

# ── Hardware test suite ───────────────────────────────────────────────────────

cmd_test() {
    local test_script="$SRC_ROOT/hw-test/test_all_task_types.py"
    [[ -f "$test_script" ]] || die "Test script not found: $test_script"

    if ! is_running "$ARTEMIS_CTR"; then
        die "Broker not running — start the stack first: ./sdr.sh start"
    fi
    if ! is_running "$CONTROLLER_CTR"; then
        die "Controller not running — start the stack first: ./sdr.sh start"
    fi

    # Stop acquisition and analysis while testing.
    # AcquisitionApp holds UDP port 30000; AnalysisApp subscribes to
    # sdr.task.response (ANYCAST) and can steal responses meant for the test.
    local acq_was_running=false
    local ana_was_running=false
    if is_running "$ACQUISITION_CTR"; then
        info "Pausing AcquisitionApp (UDP port pool) ..."
        podman stop "$ACQUISITION_CTR" >/dev/null
        acq_was_running=true
    fi
    if is_running "$ANALYSIS_CTR"; then
        info "Pausing AnalysisApp (sdr.task.response consumer) ..."
        podman stop "$ANALYSIS_CTR" >/dev/null
        ana_was_running=true
    fi

    info "Running hardware test suite against live PlutoSDR ..."
    podman run --rm --network=host \
        -v "$test_script:/test.py:ro,z" \
        sdr-controller:test-integ \
        bash -c "apt-get update -qq && apt-get install -y -qq python3-numpy 2>/dev/null && python3 /test.py"
    local rc=$?

    if $acq_was_running; then
        info "Restarting AcquisitionApp ..."
        cmd_start acquisition 2>/dev/null || true
    fi
    if $ana_was_running; then
        info "Restarting AnalysisApp ..."
        cmd_start analysis 2>/dev/null || true
    fi
    return $rc
}

# ── Scanner (interactive) ─────────────────────────────────────────────────────

cmd_scanner() {
    require_image "$SCANNER_IMAGE"
    [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && \
        die "No display set. Set DISPLAY (X11) or WAYLAND_DISPLAY (Wayland)."

    local run_args=("--rm" "--network=host" "--name" "sdr-scanner")

    if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
        run_args+=(
            -e "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
            -e "XDG_RUNTIME_DIR=/run/user/$(id -u)"
            -v "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/$WAYLAND_DISPLAY:/run/user/$(id -u)/$WAYLAND_DISPLAY:z"
            -e "QT_QPA_PLATFORM=wayland"
        )
    else
        run_args+=(
            -e "DISPLAY=$DISPLAY"
            -v "/tmp/.X11-unix:/tmp/.X11-unix:z"
            -e "QT_QPA_PLATFORM=xcb"
        )
    fi

    info "Launching SDR Scanner GUI ..."
    podman run "${run_args[@]}" "$SCANNER_IMAGE"
}

# ── Service resolution ────────────────────────────────────────────────────────

ALL_SERVICES=(broker controller acquisition analysis)

resolve_services() {
    if [[ $# -eq 0 ]]; then
        echo "${ALL_SERVICES[@]}"
    else
        echo "$@"
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────

cmd="${1:-help}"; shift || true
services=($(resolve_services "$@"))

case "$cmd" in
    start)
        for svc in "${services[@]}"; do
            case "$svc" in
                broker)      start_broker ;;
                controller)  start_controller ;;
                acquisition) start_acquisition ;;
                analysis)    start_analysis ;;
                *)           warn "Unknown service: $svc" ;;
            esac
        done
        echo ""
        cmd_status
        ;;
    stop)
        # Stop in reverse order
        for svc in $(echo "${services[@]}" | tr ' ' '\n' | tac); do
            case "$svc" in
                broker)      stop_one "$ARTEMIS_CTR"     "broker" ;;
                controller)  stop_one "$CONTROLLER_CTR"  "controller" ;;
                acquisition) stop_one "$ACQUISITION_CTR" "acquisition" ;;
                analysis)    stop_one "$ANALYSIS_CTR"    "analysis" ;;
                *)           warn "Unknown service: $svc" ;;
            esac
        done
        ;;
    restart)
        "$0" stop  "${services[@]}"
        sleep 2
        "$0" start "${services[@]}"
        ;;
    status)  cmd_status ;;
    logs)
        svc="${services[0]:-}"
        case "$svc" in
            broker)      podman logs -f "$ARTEMIS_CTR" ;;
            controller)  podman logs -f "$CONTROLLER_CTR" ;;
            acquisition) podman logs -f "$ACQUISITION_CTR" ;;
            analysis)    podman logs -f "$ANALYSIS_CTR" ;;
            *)           die "Usage: $0 logs <broker|controller|acquisition|analysis>" ;;
        esac
        ;;
    build)   cmd_build "${services[@]}" ;;
    scan)    cmd_scan "${services[@]}" ;;
    test)    cmd_test ;;
    scanner) cmd_scanner ;;
    help|--help|-h)
        cat <<EOF

  ${CYN}sdr.sh${RST} — SDR stack lifecycle manager

  ${YLW}Commands:${RST}
    start  [services...]   Start all services (or specific ones)
    stop   [services...]   Stop all services (or specific ones)
    restart                Stop then start all
    status                 Show container states and PlutoSDR reachability
    logs   <service>       Tail logs for a service
    scan   [start] [end]   CLI spectrum scan (MHz, default 80–200); prints signals
    scanner                Launch Qt GUI scanner (needs DISPLAY/WAYLAND_DISPLAY)
    test                   Run hardware test suite (8 task types vs live Pluto)
    build  [services...]   Rebuild images from source

  ${YLW}Services:${RST}  broker  controller  acquisition  analysis

  ${YLW}Examples:${RST}
    ./sdr.sh start                     # bring up full stack
    ./sdr.sh start broker controller   # bring up just core services
    ./sdr.sh stop                      # tear down everything
    ./sdr.sh logs controller           # tail controller logs
    ./sdr.sh scanner                   # open Qt band scanner UI
    PLUTO_IP=192.168.2.1 ./sdr.sh start

  ${YLW}Config XMLs${RST} are resolved in order:
    1. $SCRIPT_DIR/*.xml  (drop overrides here)
    2. hw-test/*.xml      (defaults)

EOF
        ;;
    *) die "Unknown command: $cmd — run './sdr.sh help'" ;;
esac
