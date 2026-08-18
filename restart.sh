#!/bin/bash
# Restart the Reachy companion — guarantees only one copy runs.
cd "$(dirname "$0")"

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
    echo "companion stopped"
fi
