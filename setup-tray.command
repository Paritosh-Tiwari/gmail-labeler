#!/bin/zsh
# QuickLabel -- enable the system-tray icon (macOS)
#
# Double-click this file in Finder. Idempotent.
#
# Registers a launchd agent ~/Library/LaunchAgents/com.quicklabel.tray.plist
# that starts the tray at every login. The tray polls /healthz, shows
# status in the menu bar, and gives you a menu to open the queue /
# settings / restart the server.
#
# To remove: double-click uninstall-tray.command.

set -euo pipefail
PROJECT_ROOT="${0:A:h}"
LABEL="com.quicklabel.tray"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
cd "$PROJECT_ROOT"

if [[ ! -x ".venv/bin/python" ]]; then
    print "QuickLabel doesn't seem installed. Run setup.command first."
    exit 1
fi

# Verify pystray is in the venv
if ! .venv/bin/python -c "import pystray, PIL" 2>/dev/null; then
    print "Tray dependencies not in venv. Installing..."
    .venv/bin/python -m pip install pystray pillow --quiet
fi

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_ROOT}/.venv/bin/python</string>
        <string>-m</string>
        <string>quicklabel</string>
        <string>tray</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>${PROJECT_ROOT}/data/quicklabel.tray.launchd.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST_EOF

# Reload (idempotent: bootout fails silently if not loaded)
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

print ""
print "Tray enabled."
print "  Plist: $PLIST"
print "  The tray icon should appear in your menu bar shortly."
print ""
print "  To remove the tray:  ./uninstall-tray.command"
