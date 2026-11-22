#!/bin/zsh
# Installs FocusGuard for the CURRENT macOS user only. No sudo, no Homebrew needed.
# Copies itself into ~/Library/Application Support/FocusGuard, so the folder you
# installed from can be deleted afterwards.
set -euo pipefail

SRC="${0:A:h}"
LABEL="com.focusguard.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SUPPORT="$HOME/Library/Application Support/FocusGuard"
CODE="$SUPPORT/app"
VENV="$SUPPORT/venv"
APP="$HOME/Applications/FocusGuard.app"
MODEL="qwen2.5:7b"

say_step() { print -P "\n%B==> $1%b"; }

# ---------------------------------------------------------------- 1. Python
say_step "Checking Python"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, tkinter; sys.exit(sys.version_info < (3,9))' >/dev/null 2>&1; then
    PY="$(command -v "$c")"; break
  fi
done
if [[ -z "$PY" ]]; then
  echo "Python isn't set up on this Mac yet. A window will pop up asking to install"
  echo "Apple's Command Line Tools: click Install, wait for it to finish,"
  echo "then double-click 'Install FocusGuard.command' again."
  xcode-select --install 2>/dev/null || true
  exit 1
fi
echo "Using $PY"

# ---------------------------------------------------------------- 2. Copy app + Python packages
say_step "Installing FocusGuard"
mkdir -p "$SUPPORT" "$HOME/Library/LaunchAgents" "$HOME/Applications"
rm -rf "$CODE"
mkdir -p "$CODE"
cp -R "$SRC/focusguard" "$SRC/assets" "$SRC/requirements.txt" "$CODE/"
[[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
# Only needed if you later choose the optional Claude AI; failure is OK.
"$VENV/bin/pip" install --quiet -r "$CODE/requirements.txt" || echo "(Optional Claude support not installed; the free AI still works.)"
VPY="$VENV/bin/python"

# ---------------------------------------------------------------- 3. Free local AI (Ollama)
say_step "Setting up the free local AI (Ollama)"
OLLAMA="$(command -v ollama || true)"
for p in "/Applications/Ollama.app" "$HOME/Applications/Ollama.app"; do
  [[ -z "$OLLAMA" && -x "$p/Contents/Resources/ollama" ]] && OLLAMA="$p/Contents/Resources/ollama"
done
if [[ -z "$OLLAMA" ]]; then
  echo "Downloading Ollama…"
  TMPZIP="$(mktemp -d)/Ollama.zip"
  curl -fL --progress-bar -o "$TMPZIP" "https://ollama.com/download/Ollama-darwin.zip"
  ditto -x -k "$TMPZIP" "$HOME/Applications"
  OLLAMA="$HOME/Applications/Ollama.app/Contents/Resources/ollama"
fi
# Make sure the Ollama server is running
if ! "$OLLAMA" list >/dev/null 2>&1; then
  if [[ -d "$HOME/Applications/Ollama.app" ]]; then open -g "$HOME/Applications/Ollama.app"
  elif [[ -d "/Applications/Ollama.app" ]]; then open -g "/Applications/Ollama.app"
  else ("$OLLAMA" serve >/dev/null 2>&1 &)
  fi
  for i in {1..30}; do "$OLLAMA" list >/dev/null 2>&1 && break; sleep 1; done
fi
echo "Downloading the AI model $MODEL (about 4.7 GB, only the first time)…"
"$OLLAMA" pull "$MODEL" || echo "Couldn't download it now. Later, open Terminal and run: $OLLAMA pull $MODEL"

# ---------------------------------------------------------------- 4. Background blocker (this user only)
say_step "Starting the background blocker"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$VPY</string><string>-m</string><string>focusguard.daemon</string></array>
  <key>WorkingDirectory</key><string>$CODE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$SUPPORT/daemon.out.log</string>
  <key>StandardErrorPath</key><string>$SUPPORT/daemon.err.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# ---------------------------------------------------------------- 5. App launcher
rm -rf "$APP"
osacompile -o "$APP" -e "do shell script \"cd \" & quoted form of \"$CODE\" & \" && \" & quoted form of \"$VPY\" & \" -m focusguard.gui > /dev/null 2>&1 &\""

say_step "Done!"
echo "FocusGuard is installed for $(whoami) only. Opening it now to finish setup."
echo "If macOS asks to let Python control Safari/Chrome or System Events, click Allow."
echo "You can delete the folder you installed from."
open "$APP"
