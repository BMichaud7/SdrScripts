#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  scan.sh — Start the full SDR scan pipeline and keep it running.
#
#  Starts: Artemis broker → SdrResourceManager (controller) →
#          AcquisitionApp (sweep detector) → AnalysisApp (classifier) →
#          signal_logger.py (persist to SQLite)
#
#  Usage:
#    ./scan.sh <start>-<stop>                    # e.g. ./scan.sh 88-108
#    ./scan.sh <start>-<stop> --db signals.db    # custom DB path
#    ./scan.sh <start>-<stop> --onnx             # force ONNX classifier
#    ./scan.sh <start>-<stop> --no-analysis      # detections only
#
#  Range is in MHz.  Examples:
#    ./scan.sh 80-1000     # full sweep
#    ./scan.sh 88-108      # FM broadcast band only
#    ./scan.sh 400-800     # UHF/LTE
#
#  Ctrl+C stops all services cleanly.
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HW_TEST_DIR="$SCRIPT_DIR/../hw-test"
ML_MODEL_DIR="$SCRIPT_DIR/../AnalysisApp/tools/ml/models"

# ── Defaults ──────────────────────────────────────────────────────────────────
DB_PATH="$SCRIPT_DIR/signals.db"
START_MHZ=""
STOP_MHZ=""
USE_ONNX=false
NO_ANALYSIS=false

BROKER_USER="sdr_ctrl"
BROKER_PASS="sdr_hw_test"
BROKER_URL="amqp://localhost:5672"

CONTROLLER_IMAGE="sdr-controller-hw:2.0"
ACQUISITION_IMAGE="sdr-acquisition:hw-test"
ANALYSIS_IMAGE="sdr-analysis:hw-test"
ANALYSIS_ONNX_IMAGE="sdr-analysis:hw-onnx"

DEVICES_XML="$HW_TEST_DIR/devices-direct.xml"
ARTEMIS_CTR="sdr-artemis"
CONTROLLER_CTR="sdr-controller"
ACQUISITION_CTR="sdr-acquisition"
ANALYSIS_CTR="sdr-analysis"

PROTON_PATH="${PROTON_PATH:-/tmp/proton_pkg}"

# ── Colours ───────────────────────────────────────────────────────────────────
GRN='\033[0;32m'; BLU='\033[0;34m'; YLW='\033[0;33m'; RED='\033[0;31m'; RST='\033[0m'
info()  { echo -e "${BLU}[scan]${RST} $*"; }
ok()    { echo -e "${GRN}[scan]${RST} $*"; }
warn()  { echo -e "${YLW}[scan]${RST} $*"; }
die()   { echo -e "${RED}[scan]${RST} $*" >&2; exit 1; }

# ── Arg parse ─────────────────────────────────────────────────────────────────
usage() {
    echo -e "Usage: ${0##*/} <start_mhz>-<stop_mhz> [options]"
    echo -e "  e.g.  ${0##*/} 80-1000"
    echo -e "        ${0##*/} 88-108 --no-analysis"
    echo -e "        ${0##*/} 400-800 --db /data/signals.db"
    echo -e ""
    echo -e "Options:"
    echo -e "  --db PATH        SQLite database path (default: signals.db)"
    echo -e "  --onnx           Force ONNX classifier (auto-enabled if model ready)"
    echo -e "  --no-analysis    Skip AnalysisApp — detections only, faster sweep"
    exit 1
}

# First positional arg must be the range
if [[ $# -eq 0 || "$1" == --* ]]; then
    usage
fi

RANGE="$1"; shift
if [[ ! "$RANGE" =~ ^[0-9]+-[0-9]+$ ]]; then
    die "Range must be <start>-<stop> in MHz, e.g. 80-1000 (got: '$RANGE')"
fi
START_MHZ="${RANGE%-*}"
STOP_MHZ="${RANGE#*-}"

if (( START_MHZ >= STOP_MHZ )); then
    die "Start ($START_MHZ MHz) must be less than stop ($STOP_MHZ MHz)"
fi
if (( START_MHZ < 70 || STOP_MHZ > 6000 )); then
    die "Range must be within PlutoSDR limits: 70–6000 MHz"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)          DB_PATH="$2";     shift 2 ;;
        --onnx)        USE_ONNX=true;    shift ;;
        --no-analysis) NO_ANALYSIS=true; shift ;;
        *) die "Unknown argument: $1  (run ${0##*/} for usage)" ;;
    esac
done

# Auto-enable ONNX if model is ready and image exists
MODEL_ONNX="$ML_MODEL_DIR/amr_cnn_28class.onnx"
if [[ "$USE_ONNX" == "false" && -f "$MODEL_ONNX" ]]; then
    if podman image exists "$ANALYSIS_ONNX_IMAGE" 2>/dev/null; then
        USE_ONNX=true
        info "ONNX model found — using $ANALYSIS_ONNX_IMAGE"
    fi
fi

# ── Generate scanner config ───────────────────────────────────────────────────
SCAN_CFG=$(mktemp /tmp/scan_config.XXXXXX.xml)
chmod 644 "$SCAN_CFG"
trap 'rm -f "$SCAN_CFG"' EXIT

cat > "$SCAN_CFG" << XML
<?xml version="1.0" encoding="UTF-8"?>
<sdr_acquisition version="2.0">
  <scanner_id>scanner-hw-0</scanner_id>
  <amqp>
    <url>${BROKER_URL}</url>
    <username>${BROKER_USER}</username>
    <password>${BROKER_PASS}</password>
    <detection_topic>rf.detections</detection_topic>
    <task_request_queue>sdr.task.request</task_request_queue>
    <task_response_queue>sdr.task.response</task_response_queue>
    <reconnect_interval_sec>5</reconnect_interval_sec>
  </amqp>
  <database>
    <host>localhost</host><port>5432</port><name>sdr_scanner</name>
    <user>sdr</user><password>sdr_hw_test</password>
  </database>
  <device>
    <rx_channels>1</rx_channels>
    <sample_rate_sps>20000000</sample_rate_sps>
    <rx_gain_db>50</rx_gain_db>
    <bandwidth_hz>20000000</bandwidth_hz>
  </device>
  <sweep>
    <start_hz>$(( START_MHZ * 1000000 + 8000000 ))</start_hz>
    <stop_hz>$(( STOP_MHZ * 1000000 + 8000000 ))</stop_hz>
    <dwell_samples>2097152</dwell_samples>
    <fft_size>8192</fft_size>
    <usable_bw_fraction>0.80</usable_bw_fraction>
    <threshold_db>10.0</threshold_db>
    <min_signal_bw_hz>12000</min_signal_bw_hz>
    <settle_samples>20000</settle_samples>
  </sweep>
  <rank>1</rank>
  <receiver>
    <local_ip>127.0.0.1</local_ip>
    <port>0</port>
  </receiver>
</sdr_acquisition>
XML

# ── Generate analysis config ──────────────────────────────────────────────────
ANALYSIS_CFG=$(mktemp /tmp/analysis_config.XXXXXX.xml)
chmod 644 "$ANALYSIS_CFG"
trap 'rm -f "$SCAN_CFG" "$ANALYSIS_CFG"' EXIT

if [[ "$USE_ONNX" == "true" ]]; then
    ONNX_BLOCK="
    <onnx>
      <model_path>/models/amr_cnn_28class.onnx</model_path>
      <classes_path>/models/amr_cnn_28class.classes.json</classes_path>
      <input_len>512</input_len>
      <use_gpu>false</use_gpu>
      <fallback_confidence_threshold>0.60</fallback_confidence_threshold>
      <fallback_on_unknown>true</fallback_on_unknown>
    </onnx>"
    FINAL_ANALYSIS_IMAGE="$ANALYSIS_ONNX_IMAGE"
else
    ONNX_BLOCK=""
    FINAL_ANALYSIS_IMAGE="$ANALYSIS_IMAGE"
fi

cat > "$ANALYSIS_CFG" << XML
<?xml version="1.0" encoding="UTF-8"?>
<sdr_analysis>
  <scanner_id>analysis-hw-0</scanner_id>
  <streaming_ip>127.0.0.1</streaming_ip>
  <amqp>
    <url>${BROKER_URL}</url>
    <username>${BROKER_USER}</username>
    <password>${BROKER_PASS}</password>
    <detections_topic>rf.detections</detections_topic>
    <analysis_topic>rf.analysis</analysis_topic>
    <task_request_queue>sdr.task.request</task_request_queue>
    <task_response_queue>sdr.task.response</task_response_queue>
  </amqp>
  <collector>
    <analysis_sample_rate_sps>2000000</analysis_sample_rate_sps>
    <collect_samples>200000</collect_samples>
    <analysis_timeout_ms>60000</analysis_timeout_ms>
  </collector>
  <engine>
    <fft_size>4096</fft_size>
    <snr_threshold_db>5.0</snr_threshold_db>
    <rank>2</rank>
    ${ONNX_BLOCK}
  </engine>
</sdr_analysis>
XML

# ── Helpers ───────────────────────────────────────────────────────────────────
is_running() { podman inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

wait_port() {
    local host="$1" port="$2" label="$3" max="${4:-30}"
    for i in $(seq 1 "$max"); do
        if bash -c ">/dev/tcp/$host/$port" 2>/dev/null; then ok "$label ready"; return 0; fi
        sleep 1
    done
    die "$label did not become ready"
}

LOGGER_PID=""

cleanup() {
    echo ""
    info "Shutting down …"
    [[ -n "$LOGGER_PID" ]] && kill "$LOGGER_PID" 2>/dev/null || true
    for ctr in "$ANALYSIS_CTR" "$ACQUISITION_CTR" "$CONTROLLER_CTR" "$ARTEMIS_CTR"; do
        is_running "$ctr" && podman stop "$ctr" >/dev/null 2>&1 && info "Stopped $ctr" || true
    done
    ok "All services stopped"
}
trap cleanup INT TERM EXIT

# ── Start pipeline ────────────────────────────────────────────────────────────
echo ""
echo -e "${GRN}╔══════════════════════════════════════════════════════╗${RST}"
echo -e "${GRN}║          SDR Continuous Scan Pipeline                ║${RST}"
echo -e "${GRN}╚══════════════════════════════════════════════════════╝${RST}"
info "Range: ${START_MHZ}–${STOP_MHZ} MHz"
info "ONNX classifier: $USE_ONNX"
info "Analysis: $( [[ "$NO_ANALYSIS" == "true" ]] && echo "disabled" || echo "enabled ($FINAL_ANALYSIS_IMAGE)" )"
info "Database: $DB_PATH"
echo ""

# 1. Broker
if ! is_running "$ARTEMIS_CTR"; then
    info "Starting Artemis broker …"
    podman run -d --rm --name "$ARTEMIS_CTR" --network=host \
        -e ARTEMIS_USER="$BROKER_USER" \
        -e ARTEMIS_PASSWORD="$BROKER_PASS" \
        apache/activemq-artemis:latest-alpine >/dev/null
    wait_port localhost 5672 "Artemis AMQP" 40
else
    ok "Broker already running"
fi

# 2. Controller — always restart to clear any stale device allocations
[[ -f "$DEVICES_XML" ]] || die "No devices.xml at $DEVICES_XML"
if is_running "$CONTROLLER_CTR"; then
    info "Restarting SdrResourceManager (clearing stale allocations) …"
    podman stop "$CONTROLLER_CTR" >/dev/null 2>&1 || true
fi
podman run -d --rm --replace --name "$CONTROLLER_CTR" --network=host \
    -v "$DEVICES_XML:/etc/sdr-controller/devices.xml:ro,z" \
    "$CONTROLLER_IMAGE" >/dev/null
sleep 3
is_running "$CONTROLLER_CTR" || die "Controller failed to start"
ok "Controller running"

# 3. AcquisitionApp
info "Starting AcquisitionApp (${START_MHZ}–${STOP_MHZ} MHz) …"
podman run -d --rm --replace --name "$ACQUISITION_CTR" --network=host \
    -v "$SCAN_CFG:/etc/sdr-acquisition/scanner.xml:ro,z" \
    -e SDR_LOG_LEVEL=info \
    "$ACQUISITION_IMAGE" >/dev/null
sleep 3
is_running "$ACQUISITION_CTR" || die "AcquisitionApp failed to start"
ok "AcquisitionApp running"

# 4. AnalysisApp (optional)
if [[ "$NO_ANALYSIS" == "false" ]]; then
    info "Starting AnalysisApp ($FINAL_ANALYSIS_IMAGE) …"
    ANALYSIS_RUN_ARGS=(-d --rm --replace --name "$ANALYSIS_CTR" --network=host
        -v "$ANALYSIS_CFG:/etc/sdr-analysis/analysis.xml:ro,z"
        -e SDR_LOG_LEVEL=info)
    if [[ "$USE_ONNX" == "true" ]]; then
        ANALYSIS_RUN_ARGS+=(-v "$ML_MODEL_DIR:/models:ro,z")
    fi
    podman run "${ANALYSIS_RUN_ARGS[@]}" "$FINAL_ANALYSIS_IMAGE" >/dev/null
    sleep 2
    is_running "$ANALYSIS_CTR" || die "AnalysisApp failed to start"
    ok "AnalysisApp running"
fi

# 5. Signal logger
info "Starting signal_logger (→ $DB_PATH) …"
PYTHONPATH="$PROTON_PATH" python3 "$SCRIPT_DIR/signal_logger.py" \
    --db "$DB_PATH" --broker "$BROKER_URL" \
    --user "$BROKER_USER" --pass "$BROKER_PASS" &
LOGGER_PID=$!
ok "Signal logger PID=$LOGGER_PID"

echo ""
ok "Pipeline running — press Ctrl+C to stop"
echo -e "  Monitor:  ${BLU}./read_signals.sh --db $DB_PATH${RST}"
echo ""

# Wait for Ctrl+C
wait "$LOGGER_PID" 2>/dev/null || true
