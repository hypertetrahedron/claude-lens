#!/bin/sh
# Claude Code SessionEnd hook wrapper (Linux/macOS).
#
# Finds a Python and the hook script next to this file, so the settings.json
# entry can be a single path and the repository can live anywhere. Exits 0
# whatever happens: a missing interpreter must not colour the end of a session.
dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        exec "$py" "$dir/session_end_hook.py" "$@"
    fi
done
exit 0
