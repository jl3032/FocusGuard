"""Thin wrappers around macOS command-line tools (lsappinfo, osascript)."""
from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from .common import BLOCK_PAGE

SAFARI_LIKE = {"Safari", "Safari Technology Preview"}
CHROME_LIKE = {"Google Chrome", "Google Chrome Canary", "Brave Browser",
               "Microsoft Edge", "Arc", "Vivaldi", "Chromium"}
BROWSERS = SAFARI_LIKE | CHROME_LIKE
# Browsers that don't expose the current URL to AppleScript, so websites
# can't be checked in them. FocusGuard always blocks these during a session.
UNMONITORABLE_BROWSERS = {"Firefox", "Firefox Developer Edition", "Opera",
                          "Zen", "Zen Browser", "Orion", "LibreWolf", "Tor Browser"}

# System UI and FocusGuard itself are never blocked.
ALWAYS_ALLOWED = {
    "Finder", "FocusGuard", "Python", "python3", "Python Launcher", "loginwindow",
    "Dock", "SystemUIServer", "Control Center", "Notification Center", "Spotlight",
    "System Settings", "System Preferences", "ScreenSaverEngine", "SecurityAgent",
    "UserNotificationCenter", "CoreServicesUIAgent", "WindowManager",
    "universalAccessAuthWarn", "Software Update", "Installer", "Screenshot",
    "Keychain Access", "Wallpaper", "AirPlayUIAgent",
}


class OSAError(Exception):
    pass


def _q(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def osa(script: str, timeout: float = 5) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise OSAError("osascript timed out") from e
    if r.returncode != 0:
        raise OSAError(r.stderr.strip())
    return r.stdout.rstrip("\n")


@dataclass
class FrontApp:
    name: str
    bundle_id: str
    pid: int


def frontmost_app():
    """Frontmost app of *this* user's GUI session (no special permission needed)."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        if not asn:
            return None
        info = subprocess.run(["lsappinfo", "info", asn], capture_output=True,
                              text=True, timeout=3).stdout
    except subprocess.TimeoutExpired:
        return None
    name = re.search(r'^"(.+?)" ASN', info, re.M)
    bundle = re.search(r'bundleID="([^"]*)"', info)
    pid = re.search(r"pid = (\d+)", info)
    if not name:
        return None
    return FrontApp(name.group(1), bundle.group(1) if bundle else "",
                    int(pid.group(1)) if pid else 0)


def window_title(app_name: str) -> str:
    """Title of the app's front window. Needs Accessibility permission; '' if unavailable."""
    try:
        return osa('tell application "System Events" to tell process '
                   f'{_q(app_name)} to if (count of windows) > 0 then '
                   'return name of front window', timeout=3)
    except OSAError:
        return ""


def browser_tab(app_name: str):
    """(url, title) of the active tab, or None."""
    if app_name in SAFARI_LIKE:
        script = (f"tell application {_q(app_name)} to if (count of windows) > 0 then "
                  "return (URL of current tab of front window) & linefeed & "
                  "(name of current tab of front window)")
    elif app_name in CHROME_LIKE:
        script = (f"tell application {_q(app_name)} to if (count of windows) > 0 then "
                  "return (URL of active tab of front window) & linefeed & "
                  "(title of active tab of front window)")
    else:
        return None
    try:
        out = osa(script)
    except OSAError:
        return None
    if not out:
        return None
    url, _, title = out.partition("\n")
    return url.strip(), title.strip()


def block_page_url(target: str, reason: str) -> str:
    return BLOCK_PAGE.as_uri() + "?" + urlencode({"site": target, "reason": reason})


def redirect_tab(app_name: str, url: str) -> None:
    tab = "current tab" if app_name in SAFARI_LIKE else "active tab"
    try:
        osa(f"tell application {_q(app_name)} to set URL of {tab} of front window to {_q(url)}")
    except OSAError:
        # Fall back to closing the tab if navigation is refused.
        try:
            osa(f"tell application {_q(app_name)} to close {tab} of front window")
        except OSAError:
            pass


def quit_app(app: FrontApp) -> None:
    """Ask the app to quit (it may still prompt to save work); force only if that fails."""
    try:
        osa(f"tell application {_q(app.name)} to quit", timeout=4)
    except OSAError:
        if app.pid > 0:
            try:
                os.kill(app.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def notify(title: str, message: str) -> None:
    try:
        osa(f"display notification {_q(message)} with title {_q(title)}", timeout=3)
    except OSAError:
        pass


def list_installed_apps() -> list:
    dirs = [Path("/Applications"), Path("/Applications/Utilities"),
            Path("/System/Applications"), Path("/System/Applications/Utilities"),
            Path.home() / "Applications"]
    names = set()
    for d in dirs:
        if d.is_dir():
            for p in d.iterdir():
                if p.suffix == ".app":
                    names.add(p.stem)
    return sorted(names - ALWAYS_ALLOWED, key=str.lower)
