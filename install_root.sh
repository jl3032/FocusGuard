#!/bin/zsh
# Runs as root, started by install.sh behind macOS's administrator password dialog.
# Usage: install_root.sh STAGE_DIR USER UID PYTHON [OLD_CONFIG]
set -euo pipefail
[[ "$EUID" -eq 0 ]] || { echo "This must run as root." >&2; exit 1; }
STAGE="$1"; TUSER="$2"; TUID="$3"; PY="$4"; OLD_CONFIG="${5:-}"

SUPPORT="/Library/Application Support/FocusGuard"
CODE="$SUPPORT/app"
VENV="$SUPPORT/venv"
ROOT_LABEL="com.focusguard.root"
AGENT_LABEL="com.focusguard.agent"
DPLIST="/Library/LaunchDaemons/$ROOT_LABEL.plist"
APLIST="/Library/LaunchAgents/$AGENT_LABEL.plist"
APP="/Applications/FocusGuard.app"

# Stop running copies while files are replaced.
launchctl bootout "system/$ROOT_LABEL" 2>/dev/null || true
launchctl bootout "gui/$TUID/$AGENT_LABEL" 2>/dev/null || true

# ---- code + Python environment (root-owned: the blocked user can't change them)
mkdir -p "$SUPPORT"
rm -rf "$CODE"
mkdir -p "$CODE"
cp -R "$STAGE/focusguard" "$STAGE/assets" "$STAGE/launch" "$STAGE/requirements.txt" "$CODE/"
cp "$STAGE/uninstall_root.sh" "$SUPPORT/uninstall_root.sh"
find "$CODE" -name '__pycache__' -prune -exec rm -rf {} +
[[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/pip" install --quiet -r "$CODE/requirements.txt" >/dev/null 2>&1 \
  || echo "(Optional Claude support not installed; the free AI still works.)"
"$VENV/bin/python" -m compileall -q "$CODE" >/dev/null 2>&1 || true

# ---- settings: keep the current ones, or bring over the old per-user ones
"$VENV/bin/python" -I - "$SUPPORT" "$TUSER" "$TUID" "$OLD_CONFIG" <<'PYEOF'
import json, os, sys
support, user, uid, old = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
path = os.path.join(support, "config.json")
cfg = {}
for p in (path, old):
    if p and os.path.isfile(p):
        try:
            with open(p) as f:
                cfg = json.load(f)
            break
        except (OSError, ValueError):
            pass
cfg.pop("default_session_minutes", None)
cfg.update(user=user, uid=uid)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(cfg, f, indent=2)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PYEOF

chown -R root:wheel "$SUPPORT"
chmod -R go-w "$SUPPORT"
chmod 755 "$SUPPORT"
[[ -f "$SUPPORT/anthropic_key" ]] && chmod 600 "$SUPPORT/anthropic_key"

# ---- root service (can't be stopped by the blocked user)
cat > "$DPLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$ROOT_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$VENV/bin/python</string><string>-I</string><string>$CODE/launch/root.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$SUPPORT/root.err.log</string>
</dict>
</plist>
PLIST

# ---- user-session helper (reads the front app/tab; restarted if killed)
cat > "$APLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$AGENT_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$VENV/bin/python</string><string>-I</string><string>$CODE/launch/agent.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST
chown root:wheel "$DPLIST" "$APLIST"
chmod 644 "$DPLIST" "$APLIST"

launchctl bootstrap system "$DPLIST"
launchctl bootstrap "gui/$TUID" "$APLIST" 2>/dev/null || true

# ---- app launcher in /Applications (root-owned)
rm -rf "$APP"
osacompile -o "$APP" -e "do shell script quoted form of \"$VENV/bin/python\" & \" -I \" & quoted form of \"$CODE/launch/gui.py\" & \" > /dev/null 2>&1 &\""
chown -R root:wheel "$APP"
chmod -R go-w "$APP"
echo "Protected part installed for $TUSER."
