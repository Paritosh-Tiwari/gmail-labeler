#!/bin/zsh
# Remove the QuickLabel auto-start. Does NOT delete data/, .venv/, or
# the macOS Keychain OAuth token -- those are kept so you can re-install
# without losing queue history, applies, or having to re-authorize Gmail.
#
# To fully remove QuickLabel from your machine:
#   1. Run this script (removes the auto-start)
#   2. Delete the project folder
#   3. (Optional) Open Keychain Access -> search "quicklabel" -> delete
set -euo pipefail
LABEL="com.quicklabel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ ! -f "$PLIST" ]]; then
    print "QuickLabel auto-start is not installed. Nothing to remove."
    exit 0
fi

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
rm -f "$PLIST"

# Same orphan cleanup as stop.command.
pkill -f 'quicklabel.*serve' 2>/dev/null || true

print "Auto-start removed."
print ""
print "Kept (delete by hand if you want a full clean slate):"
print "  data/                              -- queue, applies, audit log, settings"
print "  .venv/                             -- Python environment"
print "  Keychain entry 'quicklabel'        -- OAuth refresh token"
