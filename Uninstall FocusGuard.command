#!/bin/zsh
# Double-click to remove FocusGuard (needs an administrator password).
cd "${0:A:h}" && zsh ./uninstall.sh
echo
read "?Press Return to close this window."
