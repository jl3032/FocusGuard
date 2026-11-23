"""Root service (LaunchDaemon). The blocked user can't stop, change or fool it.

- Holds all decisions and state (daemon.Daemon); only root can write the files.
- Accepts on-screen reports only from the genuine user-session helper (agent.py),
  verified by process ID against launchd.
- Runs the AI unblock chat, so grants can only come from the AI's decision.
- Watchdog: if the helper is killed or unloaded, restarts it and, meanwhile,
  enforces by itself (closes blocked apps and browsers, since it can't see tabs).
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

from . import ai, updater
from .common import (AGENT_LABEL, AGENT_PLIST, GUI_SCRIPT, SOCKET_PATH, find_item,
                     limited_items, load_config, load_state, log, new_id, normalize_domain,
                     seconds_left, update_state)
from .daemon import Daemon
from .macos import frontmost_app, kill_pid, notify, user_prefix

SOL_LOCAL, LOCAL_PEERPID = 0, 0x002          # from <sys/un.h>
TESTING = os.environ.get("FOCUSGUARD_TEST") == "1"
AGENT_QUIET_RESTART = 6      # seconds without a helper report before restarting it
AGENT_QUIET_ENFORCE = 12     # ... before the root service enforces by itself


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
        self.last_agent = time.time()
        self.last_restart = 0.0
        self.agent_cache = (0, 0.0)            # (pid, checked_at)

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
            while not data.endswith(b"\n") and len(data) < 1_000_000:
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
            return {"actions": actions, "want_titles": titles}
        if op == "chat":
            return self.chat_turn(str(req.get("text", ""))[:2000])
        if op == "chat_reset":
            with self.chat_lock:
                self.chat = []
            return {"ok": True}
        if op == "resume":
            update_state(lambda s: s.update(paused_until=0))
            log("pause ended early by user")
            return {"ok": True}
        if op == "ping":
            return {"ok": True, **updater.status()}
        if op == "update":
            with self.update_lock:
                return {"message": updater.check_and_apply()}
        return {"error": "Unknown request."}

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
            try:
                r = ai.gatekeeper(list(self.chat), self.context())
            except ai.AIError as e:
                self.chat.pop()
                return {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                self.chat.pop()
                log("chat crashed:\n" + traceback.format_exc())
                return {"error": f"Unexpected error: {e}"}
            self.chat.append({"role": "assistant", "content": r["reply"]})
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
            s["grants"].append({"id": new_id(), "kind": kind, "target": target,
                                "purpose": r["purpose"], "granted_at": time.time(),
                                "expires_at": time.time() + minutes * 60})
        st = update_state(grant)
        log(f"granted {kind} {target} for {minutes}m: {r['purpose']}")
        if kind == "site" and find_item(st["penalties"], "site", target):
            return f"Note: {target} is still under a distraction block until it expires."
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
        """The helper is gone: check the front app ourselves and close what's blocked.
        Browsers are closed too, because their tabs can't be checked without the helper."""
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
        while True:
            time.sleep(2)
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
