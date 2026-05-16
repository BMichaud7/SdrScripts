#!/usr/bin/env bash
# Thin wrapper — delegates to clear_signals.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/clear_signals.py" "$@"
