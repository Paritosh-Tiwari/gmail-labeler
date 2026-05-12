#!/bin/zsh
# Start the QuickLabel launchd agent.
set -euo pipefail
LABEL="com.quicklabel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PLIST" ]]; then
    print "QuickLabel auto-start is not installed. Run setup.command first."
    exit 1
fi

# bootstrap loads the plist (idempotent if already loaded -- exits 0).
# kickstart -k re-runs the program even if KeepAlive already started it.
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/${LABEL}"
print "Started."
print "  http://127.0.0.1:8765"
