#!/usr/bin/env bash
# generate.sh — Build Doxygen HTML docs for this repo.
# Run from the repo root:  ./docs/generate.sh
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v doxygen &>/dev/null; then
    doxygen docs/Doxyfile
elif command -v podman &>/dev/null; then
    podman run --rm -v "$(pwd):/workspace:z" -w /workspace docker.io/hrektts/doxygen doxygen docs/Doxyfile
elif command -v docker &>/dev/null; then
    docker run --rm -v "$(pwd):/workspace" -w /workspace hrektts/doxygen doxygen docs/Doxyfile
else
    echo "ERROR: doxygen not found. Install: sudo dnf install doxygen"; exit 1
fi
echo "Done: file://$(pwd)/docs/html/index.html"
