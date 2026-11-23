#!/bin/zsh
# Runs as root (behind the administrator password dialog). Removes FocusGuard completely.
set -uo pipefail
[[ "$EUID" -eq 0 ]] || { echo "This must run as root." >&2; exit 1; }
SUPPORT="/Library/Application Support/FocusGuard"
UID_ARG="${1:-}"
launchctl bootout system/com.focusguard.root 2>/dev/null
if [[ -n "$UID_ARG" ]]; then launchctl bootout "gui/$UID_ARG/com.focusguard.agent" 2>/dev/null; fi
rm -f /Library/LaunchDaemons/com.focusguard.root.plist /Library/LaunchAgents/com.focusguard.agent.plist
rm -rf /Applications/FocusGuard.app "$SUPPORT"
echo "FocusGuard removed."
