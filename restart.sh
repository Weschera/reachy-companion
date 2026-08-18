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
    echo awake > /tmp/reachy-desired.state
    # if the robot was just switched on, wait for it to finish booting
    ROBOT=$(awk '/^robot:/{r=1} r && /host:/{print $2; exit}' config.yaml)
    for i in {1..40}; do
        curl -s -m 3 -o /dev/null "http://$ROBOT:8000/api/daemon/status" && break
        sleep 3
    done
    nohup uv run python -m companion.main >> /tmp/reachy-companion.log 2>&1 &
    echo "companion starting (log: /tmp/reachy-companion.log)"
else
    echo asleep > /tmp/reachy-desired.state
    # make sure the robot physically goes to its sleep pose, however the
    # app process died
    ROBOT=$(awk '/^robot:/{r=1} r && /host:/{print $2; exit}' config.yaml)
    curl -s -m 5 -X POST "http://$ROBOT:8000/api/move/play/goto_sleep" >/dev/null
    echo "companion stopped"
fi
