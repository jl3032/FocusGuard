"""Root service (LaunchDaemon). The blocked user can't stop, change or fool it.

- Holds all decisions and state (daemon.Daemon); only root can write the files.
- Accepts on-screen reports only from the genuine user-session helper (agent.py),
  verified by process ID against launchd.
- Runs the AI unblock chat, so grants can only come from the AI's decision.
- Watchdog: if the helper is killed or unloaded, restarts it and, meanwhile,
  enforces by itself (closes blocked apps; allowed browsers stay open).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from . import ai, auth, updater
from .common import (AGENT_LABEL, AGENT_PLIST, DAYS, DINNER_MINUTES, GUI_SCRIPT, SOCKET_PATH,
                     TASK_MAX_LINKS, TASK_MAX_MINUTES, find_item, limited_items, load_config,
                     load_state, log, mode, new_id, normalize_domain, parse_hhmm, save_config,
                     seconds_left, site_covers, today, update_state)
from .daemon import Daemon
from .macos import frontmost_app, kill_pid, notify, user_prefix

SOL_LOCAL, LOCAL_PEERPID = 0, 0x002          # from <sys/un.h>
TESTING = os.environ.get("FOCUSGUARD_TEST") == "1"
TASK_REVIEW_DEADLINE = 50   # the AI's check of a task's links
CHAT_DEADLINE = 60           # the chat answers (or says it can't) within this many seconds
AGENT_QUIET_RESTART = 6      # seconds without a helper report before restarting it
AGENT_QUIET_ENFORCE = 25     # ... before the root service enforces by itself
                             # (launchd brings a killed helper back within a few seconds)
SLEPT_GAP = 10               # a watchdog pause this long means the Mac was asleep


def run(cmd: list, timeout: float = 5) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "timed out")


def fmt_minutes(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


class RootService:
    def __init__(self):
        self.lock = threading.Lock()           # guards the policy engine
        self.chat_lock = threading.Lock()
        self.update_lock = threading.Lock()
        self.policy = Daemon(trusted_gui=self.is_gui_pid)
        self.chat = []                         # the unblock conversation lives here, not in the GUI
        self.chat_asks = 0                     # questions in a row the AI has asked
        self.last_agent = time.time()
        self.last_restart = 0.0
        self.agent_cache = (0, 0.0)            # (pid, checked_at)
        self.chat_pool = ThreadPoolExecutor(max_workers=2)
        self.pw_failures = []                  # times of wrong account passwords

    # ------------------------------------------------------------ who is talking to us
    @staticmethod
    def proc_args(pid: int) -> str:
        return run(["ps", "-o", "args=", "-p", str(pid)]).stdout.strip()

    def is_gui_pid(self, pid: int) -> bool:
        """Is this 'Python' window FocusGuard's own, not some other Python program?"""
        args = self.proc_args(pid)
        return (args.endswith(" -I " + str(GUI_SCRIPT))
                and " -c " not in args and " -m " not in args)

    def agent_job(self, uid: int):
        """(pid, loaded from our root-owned plist?) for the helper job in the user's session."""
        out = run(["launchctl", "print", f"gui/{uid}/{AGENT_LABEL}"]).stdout
        if not out:
            return 0, False
        genuine = re.search(r"^\s*path = (.+)$", out, re.M)
        genuine = bool(genuine and genuine.group(1).strip() == str(AGENT_PLIST))
        pid = re.search(r"^\s*pid = (\d+)", out, re.M)
        return (int(pid.group(1)) if pid else 0), genuine

    def agent_pid(self, uid: int) -> int:
        pid, at = self.agent_cache
        if time.time() - at > 3:
            pid, genuine = self.agent_job(uid)
            pid = pid if genuine else 0
            self.agent_cache = (pid, time.time())
        return pid

    # ------------------------------------------------------------ socket server
    def serve(self):
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)            # the user's GUI and helper may connect
        srv.listen(16)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn: socket.socket):
        reply = {"error": "Something went wrong inside FocusGuard."}
        try:
            conn.settimeout(10)
            data = b""
            while not data.endswith(b"\n") and len(data) < 8_000_000:   # screenshots
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            conn.settimeout(None)
            reply = self.dispatch(json.loads(data), conn)
        except Exception:
            log("request error:\n" + traceback.format_exc())
        try:
            conn.sendall(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()

    def dispatch(self, req: dict, conn: socket.socket) -> dict:
        op = req.get("op")
        cfg = load_config()
        if op == "observe":
            if not TESTING:
                peer = conn.getsockopt(SOL_LOCAL, LOCAL_PEERPID)
                if not cfg["uid"] or peer != self.agent_pid(cfg["uid"]):
                    log(f"ignored a report from a process that isn't FocusGuard's helper (pid {peer})")
                    return {"error": "Not FocusGuard's helper."}
            with self.lock:
                self.last_agent = time.time()
                actions = self.policy.tick(req.get("obs") or {})
                titles = sorted(self.policy.want_titles)
                screen = self.policy.want_screen
            return {"actions": actions, "want_titles": titles, "want_screen": screen}
        if op == "chat":
            return self.chat_turn(str(req.get("text", ""))[:2000])
        if op == "chat_reset":
            with self.chat_lock:
                self.chat, self.chat_asks = [], 0
            return {"ok": True}
        if op == "resume":
            update_state(lambda s: s.update(paused_until=0))
            log("pause ended early by user")
            return {"ok": True}
        if op in ("clock_in", "dinner", "dinner_end", "task_start", "task_end", "set_schedule"):
            return self.schedule_op(op, req, cfg)
        if op == "warm":
            threading.Thread(target=ai.warm_up, daemon=True).start()
            return {"ok": True}
        if op == "ping":
            return {"ok": True, **updater.status()}
        if op == "update":
            with self.update_lock:
                return {"message": updater.check_and_apply()}
        return {"error": "Unknown request."}

    # ------------------------------------------------------------ schedule
    def schedule_op(self, op: str, req: dict, cfg: dict) -> dict:
        st, now = load_state(), time.time()
        m = mode(cfg, st, now)
        if op == "clock_in":
            if m["name"] != "school":
                return {"error": "You're already clocked in."}
            update_state(lambda s: s.update(clocked_in_day=today()))
            log("clocked in early")
            return {"ok": True}
        if op == "dinner":
            if m["name"] == "dinner":
                return {"error": "You're already on your dinner break."}
            if m["name"] != "home":
                return {"error": "The dinner break is for after you've clocked in."}
            if st.get("dinner_day") == today():
                return {"error": "You've already had today's dinner break."}
            update_state(lambda s: s.update(dinner_day=today(),
                                            dinner_until=now + DINNER_MINUTES * 60))
            log("dinner break started")
            return {"ok": True}
        if op == "dinner_end":
            update_state(lambda s: s.update(dinner_until=0))
            log("dinner break ended early")
            return {"ok": True}
        if op == "task_end":
            update_state(lambda s: s.update(task=None))
            return {"ok": True}
        if op == "task_start":
            purpose = str(req.get("purpose", "")).strip()[:200]
            links = [expand_link(str(x).strip()[:500]) for x in req.get("links", [])
                     if str(x).strip()]
            apps = [app_info(str(p)) for p in req.get("apps", []) if str(p).strip()]
            if any(a is None for a in apps):
                return {"error": "One of the apps couldn't be found. Pick it from the list."}
            try:
                minutes = int(req.get("minutes", 0))
            except (TypeError, ValueError):
                minutes = 0
            if not purpose:
                return {"error": "Say what you're working on."}
            if not 1 <= len(links) + len(apps) <= TASK_MAX_LINKS:
                return {"error": f"Give 1 or {TASK_MAX_LINKS} links or apps."}
            if any(not normalize_domain(x) or "." not in normalize_domain(x) for x in links):
                return {"error": "Each link should be a website address, e.g. docs.google.com."}
            if not 1 <= minutes <= TASK_MAX_MINUTES:
                return {"error": f"A task period can be 1 to {TASK_MAX_MINUTES} minutes."}
            items = ", ".join(links + [a["name"] for a in apps])
            verdict = self.review_task(purpose, links, apps)
            if not verdict["approve"]:
                log(f"task not approved: {purpose} — {items}: {verdict['reply']}")
                return {"error": verdict["reply"], "rejected": True}
            task = {"purpose": purpose, "links": links, "apps": apps, "started": now,
                    "until": now + minutes * 60}
            update_state(lambda s: s.update(task=task))
            log(f"task period {minutes}m: {purpose} — {items}")
            return {"ok": True}
        # set_schedule: needs the account password of the user FocusGuard is for
        self.pw_failures = [t for t in self.pw_failures if now - t < 300]
        if len(self.pw_failures) >= 5:
            return {"error": "Too many wrong passwords. Try again in a few minutes."}
        if not auth.check_password(cfg["user"], str(req.get("password", ""))):
            self.pw_failures.append(now)
            log("clock-in times not changed: wrong account password")
            return {"error": f"That isn't the password of the Mac account \"{cfg['user']}\"."}
        times = req.get("times") or {}
        clean = {}
        for d in DAYS:
            hm = parse_hhmm(times.get(d, ""))
            if not hm:
                return {"error": "Use times like 16:00 for every day."}
            clean[d] = f"{hm[0]:02d}:{hm[1]:02d}"
        new_cfg = load_config()
        new_cfg["clock_in"] = clean
        save_config(new_cfg)
        log("clock-in times changed: " + ", ".join(f"{d} {t}" for d, t in clean.items()))
        return {"ok": True}

    def review_task(self, purpose: str, links: list, apps: list = ()) -> dict:
        """Have the AI check that the links make sense for the task. If the AI can't answer,
        the task doesn't start (otherwise quitting the AI app would approve anything)."""
        titles = {link: page_title(link) for link in links}
        ai.chat_active.set()   # pause page checks so the AI answers this first
        try:
            fut = self.chat_pool.submit(ai.review_task, purpose, links, titles, list(apps))
            return fut.result(timeout=TASK_REVIEW_DEADLINE)
        except FutureTimeout:
            return {"approve": False, "reply": "The AI didn't check the links in time. "
                                               "Try again in a moment."}
        except ai.AIError as e:
            return {"approve": False, "reply": f"The AI couldn't check the links: {e}"}
        except Exception:  # noqa: BLE001
            log("task review crashed:\n" + traceback.format_exc())
            return {"approve": False, "reply": "Something went wrong checking the links."}
        finally:
            ai.chat_active.clear()

    # ------------------------------------------------------------ AI unblock chat
    def context(self) -> str:
        cfg, st = load_config(), load_state()
        lines = [f"Time now: {time.strftime('%A %H:%M')}"]
        lines.append("Work apps (always allowed): " + ", ".join(cfg["allowed_apps"]))
        lines.append("Work sites (always allowed): " + (", ".join(cfg["allowed_sites"]) or "none"))
        lines.append("Limited items (daily allowance): " + (", ".join(
            f"{it['kind']} {it['target']} ({it['minutes']} min/day, "
            + (f"{fmt_minutes(seconds_left(it, st))} left today)" if seconds_left(it, st) > 0
               else "used up today)")
            for it in limited_items(cfg)) or "none"))
        lines.append("Currently unlocked: " + (", ".join(
            f"{g['target']} ({g['purpose']})" for g in st["grants"]) or "nothing"))
        lines.append("Distraction penalties: " + (", ".join(
            f"{p['target']} until {time.strftime('%H:%M', time.localtime(p['until']))} "
            f"(reason: {p['reason']})"
            for p in st["penalties"]) or "none"))
        lines.append("Recently blocked: " + (", ".join(
            f"{b['kind']} {b['target']} ({b['reason']})" for b in st["recent_blocks"][-8:]) or "nothing"))
        return "\n".join(lines)

    def chat_turn(self, text: str) -> dict:
        if not text.strip():
            return {"error": "Type what you need first."}
        with self.chat_lock:
            self.chat.append({"role": "user", "content": text})
            self.chat = self.chat[-20:]
            while self.chat and self.chat[0]["role"] != "user":
                self.chat = self.chat[1:]
            ai.chat_active.set()
            started = time.time()
            try:
                # Hard deadline for the whole answer, however the AI gets stuck (starting
                # up, loading the model, waiting behind a page check).
                fut = self.chat_pool.submit(ai.gatekeeper, list(self.chat), self.context(),
                                            self.chat_asks)
                r = fut.result(timeout=CHAT_DEADLINE)
                log(f"chat answered in {time.time() - started:.0f}s")
            except FutureTimeout:
                self.chat.pop()
                log(f"chat gave up after {CHAT_DEADLINE}s")
                return {"error": "The AI didn't answer within a minute. It may still be "
                                 "starting up or too slow on this Mac. Try again, or use a "
                                 "task period for the links you need."}
            except ai.AIError as e:
                self.chat.pop()
                return {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                self.chat.pop()
                log("chat crashed:\n" + traceback.format_exc())
                return {"error": f"Unexpected error: {e}"}
            finally:
                ai.chat_active.clear()
            self.chat.append({"role": "assistant", "content": r["reply"]})
            self.chat_asks = self.chat_asks + 1 if r["decision"] == "ask" else 0
        return {"reply": r["reply"], "note": self.apply_grant(r)}

    def apply_grant(self, r: dict) -> str:
        if r["decision"] != "grant" or r["target_kind"] == "none" or not r["target"].strip():
            return ""
        kind = r["target_kind"]
        target = normalize_domain(r["target"]) if kind == "site" else r["target"].strip()
        if not target:
            return ""
        minutes = r["duration_minutes"]

        def grant(s):
            s["grants"] = [g for g in s["grants"] if not (g["kind"] == kind and g["target"] == target)]
            if kind == "site":   # an appeal: the AI agreed the distraction block was wrong
                s["penalties"] = [p for p in s["penalties"]
                                  if not (find_item([p], "site", target) or site_covers(p["target"], target))]
            s["grants"].append({"id": new_id(), "kind": kind, "target": target,
                                "purpose": r["purpose"], "granted_at": time.time(),
                                "expires_at": time.time() + minutes * 60})
        update_state(grant)
        log(f"granted {kind} {target} for {minutes}m: {r['purpose']}")
        return f"Unlocked {target} for {minutes} min. Activity on it is being monitored."

    # ------------------------------------------------------------ watchdog
    def console_uid(self) -> int:
        try:
            return os.stat("/dev/console").st_uid
        except OSError:
            return -1

    def restart_agent(self, cfg: dict):
        if time.time() - self.last_restart < 10:
            return
        self.last_restart = time.time()
        uid = cfg["uid"]
        target = f"gui/{uid}/{AGENT_LABEL}"
        pid, genuine = self.agent_job(uid)
        if pid or run(["launchctl", "print", target]).stdout:
            if not genuine:
                run(["launchctl", "bootout", target])        # someone loaded a fake one
                run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT_PLIST)])
            run(["launchctl", "kickstart", "-k", target])
        else:
            run(["launchctl", "bootstrap", f"gui/{uid}", str(AGENT_PLIST)])
        self.agent_cache = (0, 0.0)
        log("FocusGuard's helper wasn't checking in; restarted it")

    def enforce_without_agent(self, cfg: dict):
        """The helper is gone: check the front app ourselves and close blocked apps.
        Allowed browsers stay open (their tabs can't be checked until the helper is back)."""
        front = frontmost_app(prefix=("launchctl", "asuser", str(cfg["uid"])))
        obs = {"app": asdict(front) if front else None, "no_agent": True}
        with self.lock:
            actions = self.policy.tick(obs)
        for a in actions:
            if a["do"] == "quit":
                kill_pid(a["pid"])
            elif a["do"] == "notify":
                notify(a["title"], a["msg"], prefix=user_prefix(cfg))

    def watchdog(self):
        last_loop = time.time()
        while True:
            time.sleep(2)
            now = time.time()
            if now - last_loop > SLEPT_GAP:
                # The Mac was asleep: the helper was paused too, not killed. Give it a
                # fresh start instead of closing the browser the moment the lid opens.
                self.last_agent = now
                log(f"Mac woke up after {int(now - last_loop)}s; waiting for the helper")
            last_loop = now
            try:
                cfg = load_config()
                if not cfg["onboarded"] or not cfg["uid"]:
                    continue
                if self.console_uid() != cfg["uid"]:
                    self.last_agent = time.time()   # that user isn't at the screen
                    continue
                quiet = time.time() - self.last_agent
                if quiet > AGENT_QUIET_RESTART:
                    self.restart_agent(cfg)
                if quiet > AGENT_QUIET_ENFORCE:
                    self.enforce_without_agent(cfg)
            except Exception:
                log("watchdog error:\n" + traceback.format_exc())


def app_info(path: str):
    """{"name", "path", "bundle_id"} of an app, read from the app itself (not from what the
    window sent), or None if it isn't a real app."""
    import plistlib
    path = os.path.realpath(path)
    if not path.endswith(".app"):
        return None
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    name = (info.get("CFBundleDisplayName") or info.get("CFBundleName")
            or os.path.basename(path)[:-4])
    return {"name": str(name)[:100], "path": path,
            "bundle_id": str(info.get("CFBundleIdentifier", ""))[:200]}


def expand_link(link: str) -> str:
    """youtu.be/ID -> youtube.com/watch?v=ID, so it matches what the browser shows."""
    m = re.match(r"^(?:https?://)?(?:www\.)?youtu\.be/([\w-]+)", link)
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else link


def page_title(link: str) -> str:
    """The page's title, fetched the way a browser would (first part of the page only)."""
    url = link if "://" in link else "https://" + link
    host = normalize_domain(url)

    def fetch(u, limit=1_000_000):
        try:
            r = subprocess.run(["/usr/bin/curl", "-sL", "--max-time", "6", "--max-redirs", "5",
                                "-A", "Mozilla/5.0 (Macintosh) FocusGuard",
                                "-H", "Accept-Language: en", "--", u],
                               capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout[:limit].decode("utf-8", errors="replace")
    if host in ("youtube.com", "m.youtube.com", "youtu.be") and ("watch" in url or host == "youtu.be"
                                                                 or "/shorts/" in url):
        # YouTube's pages are huge; its oEmbed lookup gives the title and channel directly.
        from urllib.parse import quote
        try:
            info = json.loads(fetch("https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")))
            return f"{info.get('title', '')} (channel: {info.get('author_name', '')})"[:200]
        except ValueError:
            return ""
    html = fetch(url)
    import html as htmllib
    for pattern in (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
                    r"<title[^>]*>([^<]+)</title>"):
        m = re.search(pattern, html, re.I)
        if m:
            return htmllib.unescape(" ".join(m.group(1).split()))[:200]
    return ""


def main():
    if os.geteuid() != 0 and not TESTING:
        sys.exit("The FocusGuard service must run as root.")
    log("root service started")
    svc = RootService()
    threading.Thread(target=svc.serve, daemon=True).start()
    if not TESTING:
        threading.Thread(target=updater.loop, daemon=True).start()
    svc.watchdog()


if __name__ == "__main__":
    main()
