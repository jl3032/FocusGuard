"""User-session helper (LaunchAgent). Reports the front app and browser tab to the
root service once a second and carries out what it says (quit an app, redirect a
tab, show a notification). It makes no decisions itself: if it's killed, launchd
and the root service restart it, and meanwhile the root service closes blocked
apps and browsers on its own.
"""
from __future__ import annotations

import getpass
import sys
import time
import traceback
from dataclasses import asdict

from .common import ServiceError, load_config, request
from .macos import (BROWSERS, AutomationDenied, FrontApp, browser_tab, frontmost_app,
                    is_system_process, notify, quit_app, redirect_tab, window_title)

POLL_SECONDS = 1.0


def observe(want_titles: set) -> tuple:
    front = frontmost_app()
    obs = {"app": asdict(front) if front else None}
    if front and front.name in BROWSERS and not is_system_process(front):
        try:
            tab = browser_tab(front.name)
            obs["tab"] = list(tab) if tab else None
        except AutomationDenied:
            obs["tab_denied"] = True
    if front and front.name in want_titles:
        obs["title"] = window_title(front.name)
    return front, obs


def perform(action: dict):
    kind = action.get("do")
    if kind == "quit":
        quit_app(FrontApp(action["name"], action.get("bundle_id", ""), int(action["pid"])))
    elif kind == "redirect":
        redirect_tab(action["app"], action["url"])
    elif kind == "notify":
        notify(action["title"], action["msg"])


def main():
    cfg = load_config()
    if not cfg["onboarded"] or getpass.getuser() != cfg["user"]:
        sys.exit(0)   # not the blocked user (or not set up yet): nothing to do
    want_titles = set()
    while True:
        started = time.time()
        try:
            _, obs = observe(want_titles)
            reply = request("observe", obs=obs)
            want_titles = set(reply.get("want_titles", []))
            for action in reply.get("actions", []):
                perform(action)
        except ServiceError:
            pass   # the root service is restarting
        except Exception:
            traceback.print_exc()
        time.sleep(max(0.2, POLL_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
