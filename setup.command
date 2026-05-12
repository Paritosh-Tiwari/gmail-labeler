#!/bin/zsh
# QuickLabel -- macOS installer
#
# Double-click this file in Finder. It will open in Terminal.
#
# What it does:
#   1. Verifies you have Python 3.11+ (offers to install via Homebrew)
#   2. Creates a project-local .venv and installs Python deps
#   3. Verifies you have Ollama (offers to install via Homebrew)
#   4. Asks which local LLM you want and pulls it
#   5. Registers a launchd agent so the server auto-starts at login
#   6. Starts the server now and opens the browser to it
#
# Idempotent -- re-run any time. Skips steps that are already done.

set -euo pipefail

PROJECT_ROOT="${0:A:h}"
LABEL="com.quicklabel"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
cd "$PROJECT_ROOT"

# ----------------------------- helpers -----------------------------

step()  { print -P "\n%F{cyan}[$1] $2%f"; }
ok()    { print -P "      %F{green}$1%f"; }
info()  { print -P "      %F{white}$1%f"; }
warn()  { print -P "      %F{yellow}$1%f"; }
die()   { print -P "      %F{red}error: $1%f"; exit 1; }

confirm_yes() {
    local prompt="$1"
    print -n "      $prompt [Y/n]: "
    read -r resp
    [[ -z "$resp" || "$resp" == "y" || "$resp" == "Y" ]]
}

find_python() {
    # Look for python3.12, python3.11, python3 in that order.
    for cmd in python3.12 python3.11 python3; do
        if command -v "$cmd" >/dev/null 2>&1; then
            local ver
            ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
            if [[ "$ver" =~ ^3\.(1[1-9]|[2-9][0-9])$ ]]; then
                "$cmd" -c 'import sys; print(sys.executable)'
                return 0
            fi
        fi
    done
    return 1
}

select_model() {
    print ""
    print "      Pick a local LLM. (Pull = multi-GB download via Ollama.)"
    print ""
    print "        [1] gpt-oss:20b           Best quality. ~13 GB. Needs ~16 GB RAM/VRAM."
    print "                                    Recommended on Apple Silicon w/ 32 GB+ unified memory."
    print "        [2] qwen2.5:7b-instruct   Good quality. ~5 GB. Needs ~8 GB RAM."
    print "        [3] qwen2.5:3b-instruct   Decent + fastest. ~2 GB. Runs on any Mac."
    print "        [4] Skip -- I'll set this later via /settings"
    print ""
    while true; do
        print -n "      Choice [1-4]: "
        read -r c
        case "$c" in
            1) echo "gpt-oss:20b"; return 0 ;;
            2) echo "qwen2.5:7b-instruct"; return 0 ;;
            3) echo "qwen2.5:3b-instruct"; return 0 ;;
            4) echo ""; return 0 ;;
            *) warn "Please enter 1, 2, 3, or 4." ;;
        esac
    done
}

save_model_choice() {
    local model="$1"
    .venv/bin/python - <<PYEOF
from quicklabel.settings import load_settings, save_settings
s = load_settings()
s.llm_model = "$model"
save_settings(s)
print(f"Saved llm_model={s.llm_model} to settings.json")
PYEOF
}

write_plist() {
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
        <string>serve</string>
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
    <string>${PROJECT_ROOT}/data/quicklabel.launchd.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST_EOF
}

register_autostart() {
    write_plist
    # Unload first in case a previous version is registered (idempotent re-run).
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
}

# ----------------------------- main -----------------------------

print -P "\n%F{cyan}QuickLabel installer (macOS)%f"
print "Project: $PROJECT_ROOT"

step "1/8" "Checking project layout"
[[ -f "src/quicklabel/server.py" ]] || die "src/quicklabel/server.py not found. Run from QuickLabel project root."
ok "Looks like a QuickLabel checkout."

step "2/8" "Looking for Python 3.11+"
if PY=$(find_python); then
    ok "Using $PY"
else
    warn "No suitable Python found."
    if command -v brew >/dev/null 2>&1; then
        if confirm_yes "Install Python 3.12 via Homebrew?"; then
            brew install python@3.12
            PY=$(find_python) || die "Install succeeded but python3 still not found in PATH."
        else
            die "Install Python 3.11+ from https://www.python.org/downloads/ and re-run."
        fi
    else
        warn "Homebrew not found."
        die "Install Homebrew (https://brew.sh) or Python 3.11+ from python.org, then re-run."
    fi
fi

step "3/8" "Creating .venv (project-local)"
if [[ ! -x ".venv/bin/python" ]]; then
    "$PY" -m venv .venv
    ok "Created $PROJECT_ROOT/.venv"
else
    ok "Already exists."
fi

step "4/8" "Installing Python dependencies (pip install -e .)"
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -e . --quiet
ok "Done."

step "5/8" "Looking for Ollama"
if ! command -v ollama >/dev/null 2>&1; then
    warn "Ollama not found."
    if command -v brew >/dev/null 2>&1; then
        if confirm_yes "Install Ollama via Homebrew?"; then
            brew install ollama
            # brew installs the CLI, but the Ollama service needs to be started
            ok "Starting Ollama service..."
            brew services start ollama || true
        fi
    fi
    if ! command -v ollama >/dev/null 2>&1; then
        die "Install Ollama from https://ollama.com/download/mac and re-run."
    fi
else
    ok "Ollama is installed."
fi

step "6/8" "Local LLM model"
MODEL=$(select_model)
if [[ -n "$MODEL" ]]; then
    info "Pulling $MODEL via Ollama (this may take a while)..."
    ollama pull "$MODEL"
    save_model_choice "$MODEL"
    ok "Model ready."
else
    info "Skipped. Set llm_model later at http://127.0.0.1:8765/settings"
fi

step "7/8" "Registering auto-start at login (launchd)"
register_autostart
ok "Registered ${LABEL}. Will start at next login + immediately."

step "8/8" "Starting the server"
sleep 3
HEALTH="http://127.0.0.1:8765/healthz"
ok=0
for _ in 1 2 3 4 5; do
    if curl -fsS "$HEALTH" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
done
if [[ "$ok" -eq 1 ]]; then
    ok "Server is up. Opening http://127.0.0.1:8765"
    open "http://127.0.0.1:8765"
else
    warn "Server didn't respond within 10s. Check $PROJECT_ROOT/data/quicklabel.log"
    warn "or $PROJECT_ROOT/data/quicklabel.launchd.log for early launchd errors."
fi

print ""
print -P "%F{green}All set!%f"
print ""
print "  Landing page : http://127.0.0.1:8765"
print "  Settings     : http://127.0.0.1:8765/settings"
print "  Logs         : $PROJECT_ROOT/data/quicklabel.log"
print ""
print "  Lifecycle    : ./start.command   ./stop.command   ./restart.command"
print "  Uninstall    : ./uninstall.command   (removes auto-start; keeps your data)"
print ""
print "Optional: enable the system-tray status icon"
print "  ./setup-tray.command   (green/red dot in menu bar + menu)"
print ""
print "Next: drag the 'Label this' bookmark from the landing page into your bookmark bar."
print ""
print "(This window will stay open. Close it whenever.)"
