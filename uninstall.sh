#!/bin/zsh
# Removes FocusGuard for the current user. Pass --purge to also delete settings and the API key.
set -uo pipefail
LABEL="com.focusguard.daemon"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Applications/FocusGuard.app"
if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$HOME/Library/Application Support/FocusGuard"
  security delete-generic-password -a "$(whoami)" -s FocusGuard-Anthropic >/dev/null 2>&1
fi
echo "FocusGuard removed for $(whoami)."
