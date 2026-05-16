#!/usr/bin/env bash
# Thin wrapper — delegates to read_signals.py which uses Python's built-in sqlite3
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/read_signals.py" "$@"
