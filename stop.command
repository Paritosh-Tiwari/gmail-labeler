#!/bin/zsh
# Stop the QuickLabel launchd agent. Plist file stays on disk so it
# auto-starts again at next login.
set -euo pipefail
LABEL="com.quicklabel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PLIST" ]]; then
    print "QuickLabel auto-start is not installed. Nothing to stop."
    exit 0
fi

# bootout removes the service from launchd; the plist file stays so it
# auto-loads at next login. KeepAlive doesn't apply once the service
# is booted out.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

# Catch any orphaned python serving QuickLabel (e.g. started manually
# in a terminal -- launchd doesn't know about those).
pkill -f 'quicklabel.*serve' 2>/dev/null || true

print "Stopped."
