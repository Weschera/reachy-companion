#!/bin/bash
# Install Reachy autostart: dashboard always on + crash-guardian.
# Run once:  bash install-autostart.sh
set -e
launchctl unload ~/Library/LaunchAgents/com.reachy.dashboard.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.reachy.supervisor.plist 2>/dev/null || true
pkill -f "dashboard.py" 2>/dev/null || true
sleep 1
launchctl load ~/Library/LaunchAgents/com.reachy.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.reachy.supervisor.plist
echo awake > /tmp/reachy-desired.state
echo "✓ autostart installed — dashboard + guardian will start with the Mac"
