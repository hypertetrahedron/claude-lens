#!/usr/bin/env bash
# Generate the Claude Code usage dashboard (Linux/macOS).
#
# One-shot: parses every Claude Code transcript it can find into a local SQLite
# DB, renders dashboard.html and index.html next to this script, and opens the
# dashboard when a desktop session is available. Re-running is incremental and
# always safe. Requires Python 3.9+ (stdlib only).
#
# By default it reads ~/.claude (or $CLAUDE_CONFIG_DIR), any sibling .claude*
# directory, and Claude Desktop's Cowork sessions if installed. Standing
# configuration for extra locations and remote machines lives in sources.json
# (see sources.example.json); the flags below add to it for a single run.
#
#   ./generate-dashboard.sh                    # ingest + rebuild + open
#   ./generate-dashboard.sh --no-open          # skip the browser
#   ./generate-dashboard.sh --index            # open index.html instead
#   ./generate-dashboard.sh --force            # re-parse all transcripts
#   ./generate-dashboard.sh --extra-dir ~/bkp  # also search a location
#   ./generate-dashboard.sh --remote box1      # also collect over SSH
#   ./generate-dashboard.sh --ssh-config       # ...every ~/.ssh/config host
#   ./generate-dashboard.sh --no-cowork        # skip Cowork sessions
set -euo pipefail
cd "$(dirname "$0")"

NO_OPEN=0
OPEN_INDEX=0
INGEST_ARGS=()

usage() {
    # the header comment block, minus the shebang, verbatim
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-open)      NO_OPEN=1 ;;
        --index)        OPEN_INDEX=1 ;;
        --force|--no-siblings|--no-cowork|--ssh-config|--remote-full)
                        INGEST_ARGS+=("$1") ;;
        --extra-dir|--cowork-dir|--remote|--depth|--ssh-timeout)
                        [ $# -ge 2 ] || { echo "$1 needs a value" >&2; exit 2; }
                        INGEST_ARGS+=("$1" "$2"); shift ;;
        -h|--help)      usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
    shift
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
"$PY" jsonl_ingest.py ${INGEST_ARGS+"${INGEST_ARGS[@]}"}

echo "Building dashboard..."
"$PY" build_dashboard.py

DASH="$(pwd)/dashboard.html"
IDX="$(pwd)/index.html"
echo "Dashboard ready: $DASH"
echo "All reports:     $IDX"
TARGET="$DASH"
[ "$OPEN_INDEX" -eq 1 ] && TARGET="$IDX"
if [ "$NO_OPEN" -eq 0 ]; then
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$TARGET" >/dev/null 2>&1 || true
    elif [ "$(uname)" = "Darwin" ]; then
        open "$TARGET" || true
    fi
fi
