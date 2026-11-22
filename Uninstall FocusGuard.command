#!/bin/zsh
# Double-click to remove FocusGuard (and its settings) for the current Mac user.
cd "${0:A:h}" && zsh ./uninstall.sh --purge
echo
read "?Press Return to close this window."
