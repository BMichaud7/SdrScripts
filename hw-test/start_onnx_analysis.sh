#!/usr/bin/env bash
# ========================================================================
# Project: OpenRFStack
# Author:  Brendan Michaud
# Year:    2026
# Part of OpenRFStack (https://github.com/OpenRFStack)
#
# Licensed under the Personal Use License.
# Do not use for commercial, organizational, or military purposes.
# ========================================================================

# Wait for the ONNX model to finish training, then start the ONNX-enabled
# AnalysisApp and run a live scan so the CNN classifies real signals.
set -euo pipefail

MODEL_DIR="/home/brendan/AnalysisApp/tools/ml/models"
MODEL_ONNX="$MODEL_DIR/amr_cnn_28class.onnx"
MODEL_JSON="$MODEL_DIR/amr_cnn_28class.classes.json"
ANALYSIS_XML="/home/brendan/hw-test/analysis-onnx.xml"
SCAN_CFG="/tmp/scanner-thresh.xml"   # 8 MHz BW, 10 dB threshold

echo "Waiting for training to complete: $MODEL_ONNX"
until [[ -f "$MODEL_ONNX" && -f "$MODEL_JSON" ]]; do
    epoch=$(grep -oP "Epoch \K[0-9]+" /tmp/train_extended.log 2>/dev/null | tail -1 || echo "?")
    val=$(grep -oP "val_acc=\K[0-9.]+" /tmp/train_extended.log 2>/dev/null | tail -1 || echo "?")
    echo "  Training epoch $epoch/30  val_acc=$val  (checking every 60s)"
    sleep 60
done

echo "Model ready! Starting ONNX analysis pipeline..."

# Ensure services are up
cd /home/brendan/SdrScripts
./sdr.sh start broker controller 2>&1 | tail -3

# Start ONNX-enabled AnalysisApp
podman stop sdr-analysis 2>/dev/null || true
podman run -d --rm --name sdr-analysis --network=host \
    -v "$ANALYSIS_XML:/etc/sdr-analysis/analysis.xml:ro,z" \
    -v "$MODEL_DIR:/models:ro,z" \
    -e SDR_LOG_LEVEL=info \
    sdr-analysis:hw-onnx

# Start AcquisitionApp
podman stop sdr-acquisition 2>/dev/null || true
podman run -d --rm --name sdr-acquisition --network=host \
    -v "$SCAN_CFG:/etc/sdr-acquisition/scanner.xml:ro,z" \
    -e SDR_LOG_LEVEL=info \
    sdr-acquisition:hw-test

echo "Waiting for scan to start..."
until podman logs sdr-acquisition 2>&1 | grep -q "source open"; do sleep 3; done
until podman logs sdr-analysis 2>&1 | grep -q "OnnxClassifier: loaded\|disabled"; do sleep 2; done
podman logs sdr-analysis 2>&1 | grep -E "OnnxClassifier|classes|loaded|disabled" | head -3

echo ""
echo "Live scan + ONNX classification active. Running for 3 minutes..."
PYTHONPATH=/tmp/proton_pkg python3 /home/brendan/hw-test/listen_results.py --duration 180
