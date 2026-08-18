#!/bin/bash
# Runs every 30s via launchd: if Reachy should be awake but the companion
# died (crash, WiFi drop), bring it back. Respects the Sleep button.
cd "$(dirname "$0")"
[ "$(cat /tmp/reachy-desired.state 2>/dev/null)" = "awake" ] || exit 0
pgrep -f "companion.main" >/dev/null && exit 0
# don't thrash if the robot itself is offline
ROBOT=$(awk '/^robot:/{r=1} r && /host:/{print $2; exit}' config.yaml)
curl -s -m 3 -o /dev/null "http://$ROBOT:8000/api/daemon/status" || exit 0
echo "$(date '+%F %T') supervisor: companion was down, restarting" >> /tmp/reachy-companion.log
./restart.sh
