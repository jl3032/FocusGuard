#!/bin/zsh
# Removes FocusGuard. Needs an administrator's password, so the blocked user
# can't simply uninstall it.
set -uo pipefail
ROOT_SCRIPT="/Library/Application Support/FocusGuard/uninstall_root.sh"
if [[ -f "$ROOT_SCRIPT" ]]; then
  echo "A macOS password window will appear. An ADMINISTRATOR's name and password are needed."
  if ! osascript -e "do shell script \"/bin/zsh \" & quoted form of \"$ROOT_SCRIPT\" & \" $(id -u)\" with prompt \"An administrator must approve removing FocusGuard.\" with administrator privileges"; then
    echo "Not removed: the administrator password was cancelled or wrong."
    exit 1
  fi
fi
# Leftovers from the old per-user version.
launchctl bootout "gui/$(id -u)/com.focusguard.daemon" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.focusguard.daemon.plist"
rm -rf "$HOME/Applications/FocusGuard.app" "$HOME/Library/Application Support/FocusGuard"
echo "FocusGuard removed."
