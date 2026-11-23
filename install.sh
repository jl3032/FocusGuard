#!/bin/zsh
# Installs FocusGuard. Run it logged in as the user who should be blocked.
# The protected part (root service, settings, code) is installed system-wide and
# needs an administrator's password once; it only enforces for this user.
set -euo pipefail

SRC="${0:A:h}"
TARGET_USER="$(id -un)"
TARGET_UID="$(id -u)"
MODEL="qwen2.5vl:7b"
OLD_LABEL="com.focusguard.daemon"
OLD_DIR="$HOME/Library/Application Support/FocusGuard"

say_step() { print -P "\n%B==> $1%b"; }

if [[ "$TARGET_UID" -eq 0 ]]; then
  echo "Don't run this with sudo. Log in as the user who should be blocked and run it there."
  exit 1
fi

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
  echo "then run the installer again."
  xcode-select --install 2>/dev/null || true
  exit 1
fi
echo "Using $PY"

# ---------------------------------------------------------------- 2. Stop the old version
# The old per-user blocker closed macOS's password windows, so stop it before asking
# for the administrator password.
say_step "Stopping the old version (if any)"
launchctl bootout "gui/$TARGET_UID/$OLD_LABEL" 2>/dev/null || true
OLD_CONFIG=""
[[ -f "$OLD_DIR/config.json" ]] && OLD_CONFIG="$OLD_DIR/config.json"

# ---------------------------------------------------------------- 3. Free local AI (Ollama)
say_step "Setting up the free local AI (Ollama)"
OLLAMA="$(command -v ollama || true)"
for p in "/Applications/Ollama.app" "$HOME/Applications/Ollama.app"; do
  [[ -z "$OLLAMA" && -x "$p/Contents/Resources/ollama" ]] && OLLAMA="$p/Contents/Resources/ollama"
done
if [[ -z "$OLLAMA" ]]; then
  echo "Downloading Ollama…"
  TMPZIP="$(mktemp -d)/Ollama.zip"
  if curl -fL --progress-bar -o "$TMPZIP" "https://ollama.com/download/Ollama-darwin.zip" \
     && ditto -x -k "$TMPZIP" "$HOME/Applications"; then
    OLLAMA="$HOME/Applications/Ollama.app/Contents/Resources/ollama"
  else
    echo "Couldn't download Ollama. Get it from https://ollama.com/download later;"
    echo "FocusGuard will download the AI model by itself once Ollama is open."
  fi
fi
# Make sure the Ollama server is running
if [[ -n "$OLLAMA" && -x "$OLLAMA" ]] && ! "$OLLAMA" list >/dev/null 2>&1; then
  if [[ -d "$HOME/Applications/Ollama.app" ]]; then open -g "$HOME/Applications/Ollama.app"
  elif [[ -d "/Applications/Ollama.app" ]]; then open -g "/Applications/Ollama.app"
  else ("$OLLAMA" serve >/dev/null 2>&1 &)
  fi
  for i in {1..30}; do "$OLLAMA" list >/dev/null 2>&1 && break; sleep 1; done
fi
# Pick the model that fits this Mac's chip and memory (same rule FocusGuard uses).
PICK="$("$PY" -I -c 'import sys; sys.path.insert(0, sys.argv[1])
from focusguard.ai import MODEL_SIZES, auto_size
from focusguard.macos import hardware
hw = hardware(); m = MODEL_SIZES[auto_size(hw)]
print(m["model"], m["vision"], m["gb"], hw["chip"].replace(" ", "_"), hw["memory_gb"])' "$SRC" 2>/dev/null || true)"
VISION=""
if [[ -n "$PICK" ]]; then
  read -r MODEL VISION MODEL_GB CHIP MEM <<< "$PICK"
  echo "This Mac: ${CHIP//_/ }, $MEM GB memory, so using $MODEL."
  [[ "$VISION" == "$MODEL" ]] && VISION=""
else
  MODEL_GB="6"
fi
if [[ -n "$OLLAMA" && -x "$OLLAMA" ]]; then
  echo "Downloading the AI model $MODEL (about $MODEL_GB GB, only the first time)…"
  "$OLLAMA" pull "$MODEL" || echo "Couldn't download it now; FocusGuard will retry by itself."
  if [[ -n "$VISION" ]]; then
    echo "Downloading the screen-reading model $VISION…"
    "$OLLAMA" pull "$VISION" || echo "Couldn't download it now; FocusGuard will retry by itself."
  fi
fi

# ---------------------------------------------------------------- 4. Protected part (administrator)
say_step "Installing the protected part"
STAGE="$(mktemp -d)"
cp -R "$SRC/focusguard" "$SRC/assets" "$SRC/launch" "$SRC/requirements.txt" \
      "$SRC/install_root.sh" "$SRC/uninstall_root.sh" "$STAGE/"
echo "A macOS password window will appear."
echo "Enter an ADMINISTRATOR's name and password (e.g. a parent's account)."
if ! osascript -e "do shell script \"/bin/zsh \" & quoted form of \"$STAGE/install_root.sh\" & \" \" & quoted form of \"$STAGE\" & \" \" & quoted form of \"$TARGET_USER\" & \" $TARGET_UID \" & quoted form of \"$PY\" & \" \" & quoted form of \"$OLD_CONFIG\" with prompt \"FocusGuard needs an administrator to install its protected blocker for $TARGET_USER.\" with administrator privileges"; then
  echo
  echo "Not installed: the administrator password was cancelled or wrong."
  echo "Run the installer again when an administrator can type their password."
  rm -rf "$STAGE"
  exit 1
fi
rm -rf "$STAGE"

# ---------------------------------------------------------------- 5. Remove the old per-user copy
rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
rm -rf "$HOME/Applications/FocusGuard.app" "$OLD_DIR"

say_step "Done!"
echo "FocusGuard now protects $TARGET_USER. Only an administrator can change its settings,"
echo "pause it or uninstall it. Opening it now."
echo "If macOS asks to let python control Chrome/Safari or System Events, click Allow."
echo "You can delete the folder you installed from."
open "/Applications/FocusGuard.app"
