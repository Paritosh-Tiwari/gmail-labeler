#!/bin/zsh
# Remove the QuickLabel system-tray auto-start. Server is unaffected.
set -euo pipefail
LABEL="com.quicklabel.tray"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PLIST" ]]; then
    print "Tray auto-start is not installed. Nothing to remove."
    exit 0
fi

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
rm -f "$PLIST"

# Catch any orphaned tray process.
pkill -f 'quicklabel.*tray' 2>/dev/null || true

print "Tray removed."
print "  Server auto-start (com.quicklabel) is unchanged."
print "  Re-enable the tray any time with ./setup-tray.command"
