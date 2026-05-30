#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  scan.sh — Start the full SDR scan pipeline and keep it running.
#
#  Starts: Artemis broker → SdrResourceManager (controller) →
#          AcquisitionApp (sweep detector) → AnalysisApp (classifier) →
#          signal_logger.py (persist to SQLite)
#          [optional] DemodApp (on-demand demodulation)
#          [optional] DfApp (direction finding — requires multi-SDR array)
#
#  Usage:
#    ./scan.sh <start>-<stop>                    # e.g. ./scan.sh 88-108
#    ./scan.sh <start>-<stop> --db signals.db    # custom DB path
#    ./scan.sh <start>-<stop> --onnx             # force ONNX classifier
#    ./scan.sh <start>-<stop> --no-analysis      # detections only (fastest)
#    ./scan.sh <start>-<stop> --no-pause         # concurrent scan+analysis (may stall)
#    ./scan.sh <start>-<stop> --demod            # start DemodApp (on-demand demod)
#    ./scan.sh <start>-<stop> --df               # start DfApp (MUSIC direction finding)
#
#  Range is in MHz.  Examples:
#    ./scan.sh 80-1000           # full sweep
#    ./scan.sh 88-108            # FM broadcast band only
#    ./scan.sh 400-800 --demod   # UHF/LTE with demodulation
#
#  Phased operation (default with analysis):
#    AcquisitionApp does ONE sweep (rank 2, ~8s), then pauses 3s so
#    AnalysisApp (rank 1) can classify up to 3 signals.  Fast-path signals
#    (ONNX on embedded snapshot) finish in ~5ms; slow-path full collection
#    takes ~165ms.  Worst case 3 × 165ms = 500ms, well within the 3s window.
#    When the pause ends, SCAN re-submits and preempts any running analysis.
#    Cycle: ~8s scan + ~5s drain + 3s analysis window + ~5s reconnect ≈ 21s.
#
#  For bulk classification of a pre-scanned DB, stop scan and run:
#    python3 replay_detections.py --db signals.db --count 100
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
NO_PAUSE=false
USE_DEMOD=false
USE_DF=false
USE_TEMP_LOGGER=false

BROKER_USER="sdr_ctrl"
BROKER_PASS="sdr_hw_test"
BROKER_URL="amqp://localhost:5672"

CONTROLLER_IMAGE="sdr-controller-hw:2.0"
ACQUISITION_IMAGE="sdr-acquisition:hw-test"
ANALYSIS_IMAGE="sdr-analysis:hw-test"
ANALYSIS_ONNX_IMAGE="sdr-analysis:hw-onnx"
DEMOD_IMAGE="sdr-demod:1.0.0"
DF_IMAGE="sdr-df:1.0.0"
TEMP_LOGGER_IMAGE="sdr-temp-logger:1.0.0"

DEVICES_XML="$HW_TEST_DIR/devices-direct-eth.xml"
ARTEMIS_CTR="sdr-artemis"
POSTGRES_CTR="sdr-postgres"
CONTROLLER_CTR="sdr-controller"
ACQUISITION_CTR="sdr-acquisition"
ANALYSIS_CTR="sdr-analysis"
DEMOD_CTR="sdr-demod"
DF_CTR="sdr-df"
TEMP_LOGGER_CTR="sdr-temp-logger"

PG_USER="sdr"
PG_PASS="sdr_hw_test"
PG_DB="sdr_scanner"
PG_DATA_DIR="$SCRIPT_DIR/.pgdata"
DEMOD_OUTPUT_DIR="$SCRIPT_DIR/.demod-output"

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
    echo -e "  --no-analysis    Skip AnalysisApp — detections only, fastest sweep"
    echo -e "  --no-pause       Disable analysis pause window (may stall SCAN)"
    echo -e "  --demod          Start DemodApp for on-demand signal demodulation"
    echo -e "  --df             Start DfApp for MUSIC direction finding (multi-SDR)"
    echo -e "  --temp-logger    Start SdrTempLogger to record hardware temperatures"
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
        --no-pause)    NO_PAUSE=true;    shift ;;
        --demod)       USE_DEMOD=true;       shift ;;
        --df)          USE_DF=true;          shift ;;
        --temp-logger) USE_TEMP_LOGGER=true; shift ;;
        *) die "Unknown argument: $1  (run ${0##*/} for usage)" ;;
    esac
done

# Auto-enable ONNX if model is ready and image exists
MODEL_ONNX="$ML_MODEL_DIR/modulation_classifier.onnx"
if [[ "$USE_ONNX" == "false" && -f "$MODEL_ONNX" ]]; then
    if podman image exists "$ANALYSIS_ONNX_IMAGE" 2>/dev/null; then
        USE_ONNX=true
        info "ONNX model found — using $ANALYSIS_ONNX_IMAGE"
    fi
fi

# ── Generate scanner config ───────────────────────────────────────────────────
ANALYSIS_PAUSE_MS=3000
[[ "$NO_ANALYSIS" == "true" || "$NO_PAUSE" == "true" ]] && ANALYSIS_PAUSE_MS=0

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
    <start_hz>$(( START_MHZ * 1000000 ))</start_hz>
    <stop_hz>$(( STOP_MHZ * 1000000 ))</stop_hz>
    <dwell_samples>131072</dwell_samples>
    <fft_size>8192</fft_size>
    <usable_bw_fraction>0.80</usable_bw_fraction>
    <threshold_db>6.0</threshold_db>
    <min_signal_bw_hz>12000</min_signal_bw_hz>
    <settle_samples>256</settle_samples>
    <dc_guard_hz>75000</dc_guard_hz>
    <!-- CA-CFAR: 8 guard + 32 reference cells each side (O(N) via prefix sum) -->
    <cfar_guard_bins>8</cfar_guard_bins>
    <cfar_ref_bins>32</cfar_ref_bins>
    <!-- Reject candidate runs with peak-to-mean ratio below this (dB) -->
    <min_papr_db>3.0</min_papr_db>
    <!-- Asymmetric EMA for per-frequency noise floor: rise=4×alpha, fall=alpha -->
    <noise_floor_alpha>0.08</noise_floor_alpha>
  </sweep>
  <rank>2</rank>
  <analysis_pause_ms>${ANALYSIS_PAUSE_MS}</analysis_pause_ms>
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
      <model_path>/models/modulation_classifier.onnx</model_path>
      <classes_path>/models/classes.json</classes_path>
      <input_len>512</input_len>
      <use_gpu>false</use_gpu>
      <max_batch>8</max_batch>
      <fallback_confidence_threshold>0.60</fallback_confidence_threshold>
      <fallback_on_unknown>true</fallback_on_unknown>
    </onnx>
    <snr_model_split_db>8.0</snr_model_split_db>
    <onnx_low_snr>
      <model_path>/models/amr_low_snr_denoised.onnx</model_path>
      <classes_path>/models/classes.json</classes_path>
      <input_len>512</input_len>
      <use_gpu>false</use_gpu>
      <max_batch>8</max_batch>
      <fallback_confidence_threshold>0.45</fallback_confidence_threshold>
      <fallback_on_unknown>true</fallback_on_unknown>
    </onnx_low_snr>"
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
    <demod_commands_queue>sdr.demod.commands</demod_commands_queue>
    <demod_request_queue>rf.demod.request</demod_request_queue>
  </amqp>
  <database>
    <host>localhost</host><port>5432</port><name>sdr_scanner</name>
    <user>sdr</user><password>sdr_hw_test</password>
  </database>
  <collector>
    <analysis_sample_rate_sps>2000000</analysis_sample_rate_sps>
    <!-- 65536 samples at 2 MSPS = 32 ms — sufficient for cumulants and OFDM detection.
         The fast path (ONNX on embedded snapshot) bypasses collection entirely. -->
    <collect_samples>65536</collect_samples>
    <analysis_timeout_ms>10000</analysis_timeout_ms>
  </collector>
  <engine>
    <fft_size>4096</fft_size>
    <snr_threshold_db>5.0</snr_threshold_db>
    <rank>2</rank>
    ${ONNX_BLOCK}
  </engine>
</sdr_analysis>
XML

# ── Generate demod config (if --demod) ───────────────────────────────────────
if [[ "$USE_DEMOD" == "true" ]]; then
    DEMOD_CFG=$(mktemp /tmp/demod_config.XXXXXX.xml)
    chmod 644 "$DEMOD_CFG"
    trap 'rm -f "$SCAN_CFG" "$ANALYSIS_CFG" "$DEMOD_CFG"' EXIT

    cat > "$DEMOD_CFG" << XML
<?xml version="1.0" encoding="UTF-8"?>
<demod_config>
  <broker>
    <url>${BROKER_URL}</url>
    <username>${BROKER_USER}</username>
    <password>${BROKER_PASS}</password>
    <demod_request_queue>rf.demod.request</demod_request_queue>
    <task_request_queue>sdr.task.request</task_request_queue>
    <demod_topic>rf.demod</demod_topic>
  </broker>
  <streaming>
    <local_ip>127.0.0.1</local_ip>
  </streaming>
  <output>
    <output_dir>/demod-output</output_dir>
    <publish_amqp>true</publish_amqp>
  </output>
  <engine>
    <rank>4</rank>
    <audio_duration_ms>5000</audio_duration_ms>
    <digital_duration_ms>2000</digital_duration_ms>
    <audio_sample_rate_hz>48000</audio_sample_rate_hz>
  </engine>
</demod_config>
XML
fi

# ── Generate df config (if --df) ─────────────────────────────────────────────
if [[ "$USE_DF" == "true" ]]; then
    DF_CFG=$(mktemp /tmp/df_config.XXXXXX.xml)
    chmod 644 "$DF_CFG"
    trap 'rm -f "$SCAN_CFG" "$ANALYSIS_CFG" "${DEMOD_CFG:-}" "$DF_CFG"' EXIT

    # Antenna array config — update x/y positions to match your physical layout.
    # scanner_id values must match the AcquisitionApp scanner_id in each device's config.
    # x = East (m), y = North (m) from antenna 0 (reference).
    DF_ARRAY_XML="$HW_TEST_DIR/df_array.xml"
    if [[ -f "$DF_ARRAY_XML" ]]; then
        DF_ARRAY_BLOCK=$(cat "$DF_ARRAY_XML")
    else
        warn "No $DF_ARRAY_XML found — using placeholder 4-element L-array (UPDATE BEFORE USE)"
        DF_ARRAY_BLOCK='    <element scanner_id="scanner-0"><x>0.00</x><y>0.00</y></element>
    <element scanner_id="scanner-1"><x>0.00</x><y>0.50</y></element>
    <element scanner_id="scanner-2"><x>0.50</x><y>0.00</y></element>
    <element scanner_id="scanner-3"><x>1.00</x><y>0.00</y></element>'
    fi

    cat > "$DF_CFG" << XML
<?xml version="1.0" encoding="UTF-8"?>
<df_config>
  <scanner_id>df-0</scanner_id>
  <amqp>
    <url>${BROKER_URL}</url>
    <username>${BROKER_USER}</username>
    <password>${BROKER_PASS}</password>
    <detections_topic>rf.detections</detections_topic>
    <df_results_topic>rf.df_results</df_results_topic>
  </amqp>
  <database>
    <host>localhost</host><port>5432</port><name>${PG_DB}</name>
    <user>${PG_USER}</user><password>${PG_PASS}</password>
  </database>
  <array>
${DF_ARRAY_BLOCK}
  </array>
  <engine>
    <aggregation_window_sec>5</aggregation_window_sec>
    <freq_bin_hz>100000</freq_bin_hz>
    <min_elements>3</min_elements>
    <bearing_step_deg>0.5</bearing_step_deg>
  </engine>
</df_config>
XML
fi

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
    for ctr in "$TEMP_LOGGER_CTR" "$DF_CTR" "$DEMOD_CTR" "$ANALYSIS_CTR" "$ACQUISITION_CTR" "$CONTROLLER_CTR" "$ARTEMIS_CTR" "$POSTGRES_CTR"; do
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
info "Analysis pause: $( [[ "$ANALYSIS_PAUSE_MS" -gt 0 ]] && echo "${ANALYSIS_PAUSE_MS}ms per sweep" || echo "disabled" )"
info "Demodulation: $USE_DEMOD"
info "Direction finding: $USE_DF"
info "Temp logger: $USE_TEMP_LOGGER"
info "Database: $DB_PATH"
echo ""

# 1. PostgreSQL
if ! is_running "$POSTGRES_CTR"; then
    info "Starting PostgreSQL …"
    mkdir -p "$PG_DATA_DIR"
    podman run -d --rm --name "$POSTGRES_CTR" --network=host \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASS" \
        -e POSTGRES_DB="$PG_DB" \
        -e PGDATA=/var/lib/postgresql/data \
        -v "$PG_DATA_DIR:/var/lib/postgresql/data:z" \
        docker.io/postgres:16-alpine >/dev/null
    # Wait for ready (up to 30s)
    for i in $(seq 1 30); do
        podman exec "$POSTGRES_CTR" pg_isready -U "$PG_USER" -d "$PG_DB" 2>/dev/null && break
        sleep 1
    done
    # Apply schemas
    podman exec -i "$POSTGRES_CTR" psql -U "$PG_USER" -d "$PG_DB" \
        < "$SCRIPT_DIR/../AcquisitionApp/schema/init.sql" >/dev/null
    podman exec -i "$POSTGRES_CTR" psql -U "$PG_USER" -d "$PG_DB" \
        < "$SCRIPT_DIR/../DfApp/schema/init.sql" >/dev/null
    ok "PostgreSQL ready"
else
    ok "PostgreSQL already running"
fi

# 2. Broker
if ! is_running "$ARTEMIS_CTR"; then
    info "Starting Artemis broker …"
    podman run -d --rm --name "$ARTEMIS_CTR" --network=host \
        -e ARTEMIS_USER="$BROKER_USER" \
        -e ARTEMIS_PASSWORD="$BROKER_PASS" \
        docker.io/apache/activemq-artemis:latest-alpine >/dev/null
    wait_port localhost 5672 "Artemis AMQP" 40
else
    ok "Broker already running"
fi

# 3. Controller — always restart to clear any stale device allocations
[[ -f "$DEVICES_XML" ]] || die "No devices.xml at $DEVICES_XML"
if is_running "$CONTROLLER_CTR"; then
    info "Restarting SdrResourceManager (clearing stale allocations) …"
    podman stop "$CONTROLLER_CTR" >/dev/null 2>&1 || true
fi
podman run -d --rm --replace --name "$CONTROLLER_CTR" --network=host \
    --privileged \
    -v "$DEVICES_XML:/etc/sdr-controller/devices.xml:ro,z" \
    "$CONTROLLER_IMAGE" >/dev/null
sleep 3
is_running "$CONTROLLER_CTR" || die "Controller failed to start"
ok "Controller running"

# 4. Signal logger — start BEFORE AcquisitionApp so it doesn't miss the first sweep
# Use the PostgreSQL backend (postgres container started in step 1) so signals
# land in both postgres (queryable via psql/Grafana) and the local SQLite file.
info "Starting signal_logger (→ $DB_PATH + postgres) …"
PYTHONPATH="$PROTON_PATH" python3 "$SCRIPT_DIR/signal_logger.py" \
    --db "$DB_PATH" --broker "$BROKER_URL" \
    --user "$BROKER_USER" --pass "$BROKER_PASS" \
    --pg-host localhost --pg-user "$PG_USER" --pg-pass "$PG_PASS" &
LOGGER_PID=$!
ok "Signal logger PID=$LOGGER_PID"
sleep 4   # give logger time to connect to AMQP before acquisition starts

# 5. AcquisitionApp
info "Starting AcquisitionApp (${START_MHZ}–${STOP_MHZ} MHz) …"
mkdir -p "$SCRIPT_DIR/.cache"
podman run -d --rm --replace --name "$ACQUISITION_CTR" --network=host \
    -v "$SCAN_CFG:/etc/sdr-acquisition/scanner.xml:ro,z" \
    -v "$SCRIPT_DIR/.cache:/var/cache/sdr-acquisition:z" \
    -e SDR_LOG_LEVEL=info \
    "$ACQUISITION_IMAGE" >/dev/null
sleep 3
is_running "$ACQUISITION_CTR" || die "AcquisitionApp failed to start"
ok "AcquisitionApp running"

# 6. AnalysisApp (optional)
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

# 7. DemodApp (optional — on-demand signal demodulation)
if [[ "$USE_DEMOD" == "true" ]]; then
    info "Starting DemodApp ($DEMOD_IMAGE) …"
    mkdir -p "$DEMOD_OUTPUT_DIR"
    podman run -d --rm --replace --name "$DEMOD_CTR" --network=host \
        -v "$DEMOD_CFG:/etc/sdr-demod/demod.xml:ro,z" \
        -v "$DEMOD_OUTPUT_DIR:/demod-output:z" \
        -e SDR_LOG_LEVEL=info \
        "$DEMOD_IMAGE" >/dev/null
    sleep 2
    is_running "$DEMOD_CTR" || die "DemodApp failed to start"
    ok "DemodApp running (output → $DEMOD_OUTPUT_DIR)"
fi

# 8. DfApp (optional — MUSIC direction finding, requires multi-SDR array)
if [[ "$USE_DF" == "true" ]]; then
    info "Starting DfApp ($DF_IMAGE) …"
    podman run -d --rm --replace --name "$DF_CTR" --network=host \
        -v "$DF_CFG:/etc/sdr-df/df.xml:ro,z" \
        -e SDR_LOG_LEVEL=info \
        "$DF_IMAGE" >/dev/null
    sleep 2
    is_running "$DF_CTR" || die "DfApp failed to start"
    ok "DfApp running (bearings → rf.df_results / postgres df_results)"
fi

# 9. SdrTempLogger (optional — hardware temperature recording)
if [[ "$USE_TEMP_LOGGER" == "true" ]]; then
    TEMP_DB_DIR="$SCRIPT_DIR/.temp-data"
    mkdir -p "$TEMP_DB_DIR"
    info "Starting SdrTempLogger ($TEMP_LOGGER_IMAGE) …"
    podman run -d --rm --replace --name "$TEMP_LOGGER_CTR" --network=host \
        -v "$TEMP_DB_DIR:/data:z" \
        "$TEMP_LOGGER_IMAGE" \
        --broker "$BROKER_URL" \
        --config /etc/sdr-temp-logger/config.yaml \
        --db /data/sdr_temps.db >/dev/null
    sleep 2
    is_running "$TEMP_LOGGER_CTR" || die "SdrTempLogger failed to start"
    ok "SdrTempLogger running (→ $TEMP_DB_DIR/sdr_temps.db)"
fi

echo ""
ok "Pipeline running — press Ctrl+C to stop"
echo -e "  Monitor:  ${BLU}./read_signals.sh --db $DB_PATH${RST}"
[[ "$USE_DEMOD" == "true" ]] && \
    echo -e "  Demod:    ${BLU}$DEMOD_OUTPUT_DIR${RST} (WAV + bits files)"
[[ "$USE_DF" == "true" ]] && \
    echo -e "  Bearings: ${BLU}psql -U $PG_USER -d $PG_DB -c 'SELECT * FROM recent_df_results;'${RST}"
[[ "$USE_TEMP_LOGGER" == "true" ]] && \
    echo -e "  Temps:    ${BLU}$SCRIPT_DIR/.temp-data/sdr_temps.db${RST}"
echo ""

# Wait for Ctrl+C
wait "$LOGGER_PID" 2>/dev/null || true

# NOTE: Running with --no-analysis gives the best scanning performance (7.6s sweep).
# For phased scan+analysis: AcquisitionApp does one sweep (rank 2), then releases the
# SDR for 15s so AnalysisApp (rank 1) can classify up to 3 signals. When the window
# ends, SCAN re-submits (rank 2) and preempts any running analysis. Approx 33s cycle.
#
# Fast-path classification: AnalysisApp extracts a 1024-sample IQ snapshot from each
# RF_DETECTION message and runs ONNX in ~5 ms without re-acquiring the SDR. Signals
# with ONNX confidence ≥ 75% are published immediately; the remaining ~35% go through
# the full 32 ms SDR collection + feature extraction path.
#
# ONNX model: train with AnalysisApp/training/train_classifier.py, then copy
#   modulation_classifier.onnx + classes.json → $ML_MODEL_DIR
# For bulk classification of a pre-scanned DB: replay_detections.py
