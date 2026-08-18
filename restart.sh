#!/bin/bash
# Restart the Reachy companion — guarantees only one copy runs.
cd "$(dirname "$0")"

# a sane environment even when launched from the dashboard or launchd
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
# clear a possibly-poisoned GStreamer plugin cache (libgstpython segfaults
# and is renamed .disabled in the venv — see README troubleshooting)
rm -f "$HOME/.cache/gstreamer-1.0"/registry.*.bin 2>/dev/null

# ask nicely, then insist
pkill -INT -f "companion.main" 2>/dev/null
for i in {1..10}; do
    pgrep -f "companion.main" >/dev/null || break
    sleep 1
done
pkill -9 -f "companion.main" 2>/dev/null
sleep 1

if [ "$1" != "stop" ]; then
    nohup uv run python -m companion.main >> /tmp/reachy-companion.log 2>&1 &
    echo "companion starting (log: /tmp/reachy-companion.log)"
else
    # make sure the robot physically goes to its sleep pose, however the
    # app process died
    ROBOT=$(awk '/^robot:/{r=1} r && /host:/{print $2; exit}' config.yaml)
    curl -s -m 5 -X POST "http://$ROBOT:8000/api/move/play/goto_sleep" >/dev/null
    echo "companion stopped"
fi
