"""Thin wrappers around macOS command-line tools (lsappinfo, osascript)."""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from .common import ADMIN_SCRIPT, BLOCK_PAGE, GUI_APP, VENV_PYTHON, log

SAFARI_LIKE = {"Safari", "Safari Technology Preview"}
CHROME_LIKE = {"Google Chrome", "Google Chrome Canary", "Brave Browser",
               "Microsoft Edge", "Arc", "Vivaldi", "Chromium"}
BROWSERS = SAFARI_LIKE | CHROME_LIKE
# Browsers that don't expose the current URL to AppleScript, so websites
# can't be checked in them. FocusGuard always blocks these.
UNMONITORABLE_BROWSERS = {"Firefox", "Firefox Developer Edition", "Opera",
                          "Zen", "Zen Browser", "Orion", "LibreWolf", "Tor Browser"}

# System UI is never blocked, but only the real thing: these names count only for
# apps inside /System, so a game renamed "Finder.app" is still blocked.
ALWAYS_ALLOWED = {
    "Finder", "loginwindow",
    "Dock", "SystemUIServer", "Control Center", "Notification Center", "Spotlight",
    "System Settings", "System Preferences", "ScreenSaverEngine", "SecurityAgent",
    "UserNotificationCenter", "CoreServicesUIAgent", "WindowManager",
    "universalAccessAuthWarn", "Software Update", "Installer", "Screenshot",
    "Keychain Access", "Wallpaper", "AirPlayUIAgent",
}


class OSAError(Exception):
    pass


class AutomationDenied(Exception):
    """macOS refused to let FocusGuard control this app (Automation permission)."""


def _q(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def osa(script: str, timeout: float = 5) -> str:
    started = time.time()
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        log(f"AppleScript gave up after {timeout}s: {script[:70]}")
        raise OSAError("osascript timed out") from e
    took = time.time() - started
    if took > 1:
        log(f"slow AppleScript ({took:.1f}s): {script[:70]}")
    if r.returncode != 0:
        raise OSAError(r.stderr.strip())
    return r.stdout.rstrip("\n")


@dataclass
class FrontApp:
    name: str
    bundle_id: str
    pid: int
    path: str = ""     # the .app bundle it was launched from
    kind: str = ""     # "Foreground" for normal apps; "UIElement"/"BackgroundOnly" for agents


def frontmost_app(prefix: tuple = ()):
    """Frontmost app of the GUI session. `prefix` lets the root service run this inside
    the user's session (launchctl asuser ...)."""
    try:
        asn = subprocess.run([*prefix, "lsappinfo", "front"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        if not asn:
            return None
        info = subprocess.run([*prefix, "lsappinfo", "info", asn], capture_output=True,
                              text=True, timeout=3).stdout
    except subprocess.TimeoutExpired:
        return None
    name = re.search(r'^"(.+?)" ASN', info, re.M)
    bundle = re.search(r'bundleID="([^"]*)"', info)
    pid = re.search(r"pid = (\d+)", info)
    path = re.search(r'bundle path="([^"]*)"', info)
    kind = re.search(r'type="([^"]*)"', info)
    if not name:
        return None
    return FrontApp(name.group(1), bundle.group(1) if bundle else "",
                    int(pid.group(1)) if pid else 0,
                    path.group(1) if path else "", kind.group(1) if kind else "")


SYSTEM_PREFIXES = ("/System/Library/", "/usr/", "/Library/Apple/")


def is_system_process(app: FrontApp) -> bool:
    """macOS's own helpers: password prompts, permission dialogs, the Dock, etc.
    Never block these; doing so made the admin password window vanish."""
    if app.path:
        return app.path.startswith(SYSTEM_PREFIXES)
    return bool(app.kind) and app.kind != "Foreground"   # unknown location: only agents


def always_allowed(app: FrontApp) -> bool:
    if app.path == str(GUI_APP):
        return True   # FocusGuard's own launcher (root-owned, in /Applications)
    return app.name in ALWAYS_ALLOWED and app.path.startswith("/System/")


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
        out = osa(script, timeout=3)
    except OSAError as e:
        msg = str(e)
        if "-1743" in msg or "Not authorized" in msg or "not allowed" in msg.lower():
            raise AutomationDenied(app_name) from e
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
    """Ask the app to quit (it may still prompt to save work); force it if that fails.
    Targets the app by bundle ID, so a renamed copy can't make us quit the real one."""
    target = f"application id {_q(app.bundle_id)}" if app.bundle_id else f"application {_q(app.name)}"
    try:
        osa(f"tell {target} to quit", timeout=4)
    except OSAError:
        kill_pid(app.pid)


def run_admin(args: list, reason: str):
    """Run FocusGuard's admin tool as root, behind macOS's own administrator password
    dialog (FocusGuard never sees the password). Returns (ok, message)."""
    cmd = " ".join(shlex.quote(str(a)) for a in [VENV_PYTHON, "-I", ADMIN_SCRIPT, *args])
    script = f"do shell script {_q(cmd)} with prompt {_q(reason)} with administrator privileges"
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           timeout=600)
    except subprocess.TimeoutExpired:
        return False, "Timed out waiting for the administrator password."
    if r.returncode == 0:
        return True, r.stdout.strip()
    if "-128" in r.stderr:
        return False, "Cancelled."
    return False, (r.stderr.strip() or "The administrator action failed.")[-300:]


def notify(title: str, message: str, prefix: tuple = ()) -> None:
    script = f"display notification {_q(message)} with title {_q(title)}"
    try:
        if prefix:
            subprocess.run([*prefix, "osascript", "-e", script], capture_output=True, timeout=5)
        else:
            osa(script, timeout=3)
    except (OSAError, subprocess.TimeoutExpired):
        pass


def user_prefix(cfg: dict) -> tuple:
    """Command prefix the root service uses to run something as the blocked user,
    inside their login session."""
    return ("launchctl", "asuser", str(cfg["uid"]), "sudo", "-u", cfg["user"])


def kill_pid(pid: int) -> None:
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def installed_apps(home: Path = None) -> dict:
    """{app name: {"path", "bundle_id"}} for apps in the usual folders."""
    home = home or Path.home()
    dirs = [Path("/Applications"), Path("/Applications/Utilities"),
            Path("/System/Applications"), Path("/System/Applications/Utilities"),
            home / "Applications"]
    apps = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix != ".app" or p.stem in ALWAYS_ALLOWED or p.stem in apps:
                continue
            bundle_id = ""
            try:
                with open(p / "Contents" / "Info.plist", "rb") as f:
                    bundle_id = plistlib.load(f).get("CFBundleIdentifier", "")
            except (OSError, plistlib.InvalidFileException, ValueError):
                pass
            apps[p.stem] = {"path": str(p), "bundle_id": bundle_id}
    return dict(sorted(apps.items(), key=lambda kv: kv[0].lower()))


def list_installed_apps() -> list:
    return list(installed_apps())
