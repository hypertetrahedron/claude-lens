#!/usr/bin/env bash
# Generate the Claude Code usage dashboard (Linux/macOS).
#
# One-shot: parses every Claude Code transcript for the signed-in user
# (~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects if set) into a local
# SQLite DB, renders dashboard.html next to this script, and opens it when a
# desktop session is available. Re-running is incremental and always safe.
# Requires Python 3.9+ (stdlib only).
#
#   ./generate-dashboard.sh            # ingest new activity + rebuild + open
#   ./generate-dashboard.sh --no-open  # skip opening the browser
#   ./generate-dashboard.sh --force    # re-parse all transcripts from scratch
set -euo pipefail
cd "$(dirname "$0")"

NO_OPEN=0
FORCE=""
for arg in "$@"; do
    case "$arg" in
        --no-open) NO_OPEN=1 ;;
        --force)   FORCE="--force" ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "Python 3.9+ is required but was not found on PATH." >&2
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Python 3.9 or newer is required (found: $("$PY" --version 2>&1))." >&2
    exit 1
fi

echo "Ingesting Claude Code transcripts..."
"$PY" jsonl_ingest.py $FORCE

echo "Building dashboard..."
"$PY" build_dashboard.py

DASH="$(pwd)/dashboard.html"
echo "Dashboard ready: $DASH"
if [ "$NO_OPEN" -eq 0 ]; then
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$DASH" >/dev/null 2>&1 || true
    elif [ "$(uname)" = "Darwin" ]; then
        open "$DASH" || true
    fi
fi
