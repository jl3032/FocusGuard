"""Shared config/state storage, matching helpers and the client for the root service.

FocusGuard is installed system-wide but only enforces for one Mac user (the
"user" in config.json). Its files live in /Library/Application Support/FocusGuard
and are owned by root: the blocked user can read them but not change them.
Only the root service (rootd) writes state; config changes go through the admin
tool, which needs an administrator password.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path(os.environ.get("FOCUSGUARD_DIR", "/Library/Application Support/FocusGuard"))
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
LOCK_PATH = APP_DIR / ".lock"
LOG_PATH = APP_DIR / "activity.log"
KEY_PATH = APP_DIR / "anthropic_key"          # root-only (0600)
SOCKET_PATH = os.environ.get("FOCUSGUARD_SOCKET", str(APP_DIR / "focusguard.sock"))
REPO_DIR = Path(__file__).resolve().parent.parent
BLOCK_PAGE = REPO_DIR / "assets" / "blocked.html"
VENV_PYTHON = APP_DIR / "venv" / "bin" / "python"
LAUNCH_DIR = REPO_DIR / "launch"
GUI_SCRIPT = LAUNCH_DIR / "gui.py"
ADMIN_SCRIPT = LAUNCH_DIR / "admin.py"
GUI_APP = Path("/Applications/FocusGuard.app")
AGENT_LABEL = "com.focusguard.agent"
ROOT_LABEL = "com.focusguard.root"
AGENT_PLIST = Path("/Library/LaunchAgents") / f"{AGENT_LABEL}.plist"

DEFAULT_CONFIG = {
    "onboarded": False,
    "user": "",                  # the one Mac user FocusGuard enforces for
    "uid": 0,
    # Work items: always allowed.
    "allowed_apps": [],          # app display names, e.g. "Safari", "Notion"
    "allowed_sites": [],         # bare domains, e.g. "github.com"
    # Limited items: allowed for a few minutes a day, then blocked until midnight.
    "limited_apps": [],          # [{"target": "Steam", "minutes": 15}]
    "limited_sites": [],         # [{"target": "chess.com", "minutes": 10}]
    # Where each picked app really lives, so a renamed copy (a game called
    # "Notes.app") doesn't count: {"notes": {"path": ..., "bundle_id": ...}}
    "app_ids": {},
    # Everything else is always blocked unless the AI unlocks it.
    "distraction_penalty_minutes": 60,
    "max_grant_minutes": 90,
    "on_task_check_seconds": 60,  # how often unlocked items are re-checked
    # AI provider: "ollama" (free, runs locally, no key) or "anthropic" (Claude API key)
    "provider": "ollama",
    # Size of the free local model: "auto" picks one that fits this Mac's chip and memory;
    # or "small" / "medium" / "large" (see ai.MODEL_SIZES).
    "ai_model_size": "auto",
    "screen_checks": True,          # the AI also looks at screenshots of work/limited sites
    "ollama_model": "qwen2.5:7b",   # only used if ai_model_size isn't one of the above
    "ollama_url": "http://localhost:11434",
    "claude_model": "claude-opus-5-5",
}
# Keys the admin tool accepts from the setup window.
EDITABLE_KEYS = set(DEFAULT_CONFIG) - {"onboarded", "user", "uid"}

DEFAULT_STATE = {
    "grants": [],         # temporary AI unlocks
    "penalties": [],      # 1-hour distraction blocks
    "recent_blocks": [],  # what was blocked lately (gives the AI context)
    "usage": {"day": "", "seconds": {}},  # time spent today on limited items
    "paused_until": 0,    # blocking is off until this time (needs an admin password)
    "no_permission": {},  # browser -> last time macOS refused to let us read its tabs
    "agent_seen": 0,      # last time the user-session helper checked in
}


# ---------------------------------------------------------------- storage
@contextlib.contextmanager
def _locked():
    with open(LOCK_PATH, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path, default: dict) -> dict:
    merged = copy.deepcopy(default)
    try:
        merged.update(json.loads(path.read_text()))
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        pass
    return merged


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _prune(st: dict) -> dict:
    now = time.time()
    st["grants"] = [g for g in st["grants"] if g["expires_at"] > now]
    st["penalties"] = [p for p in st["penalties"] if p["until"] > now]
    st["recent_blocks"] = st["recent_blocks"][-15:]
    st["no_permission"] = {k: t for k, t in st["no_permission"].items() if now - t < 30}
    st.pop("session", None)   # left over from the old focus-session version
    if st["usage"].get("day") != today():
        st["usage"] = {"day": today(), "seconds": {}}
    return st


# Reads need no lock (writes are atomic renames), so the GUI and the user-session
# helper can read the root-owned files without being able to write anything.
def load_config() -> dict:
    return _read(CONFIG_PATH, DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with _locked():
        _write(CONFIG_PATH, cfg)


def load_state() -> dict:
    return _prune(_read(STATE_PATH, DEFAULT_STATE))


def update_state(fn) -> dict:
    """Read-modify-write the state under a lock (root service only)."""
    with _locked():
        st = _prune(_read(STATE_PATH, DEFAULT_STATE))
        fn(st)
        _write(STATE_PATH, st)
        return st


def paused(st: dict) -> bool:
    return st.get("paused_until", 0) > time.time()


def today() -> str:
    return time.strftime("%Y-%m-%d")


def usage_key(kind: str, target: str) -> str:
    return f"{kind}:{target.lower()}"


def limited_items(cfg: dict) -> list:
    """All limited items as [{"kind", "target", "minutes"}]."""
    return ([{"kind": "app", **x} for x in cfg["limited_apps"]] +
            [{"kind": "site", **x} for x in cfg["limited_sites"]])


def seconds_left(item: dict, st: dict) -> float:
    used = st["usage"]["seconds"].get(usage_key(item["kind"], item["target"]), 0)
    return item["minutes"] * 60 - used


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass   # the GUI and helper can't write the root-owned log; that's fine


# ---------------------------------------------------------------- matching
def normalize_domain(text: str) -> str:
    text = text.strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    host = urlparse(text).hostname or ""
    return host[4:] if host.startswith("www.") else host


def host_of(url: str):
    """Hostname of a real web page, or None for internal pages (about:, file:, chrome:)."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    h = p.hostname.lower()
    return h[4:] if h.startswith("www.") else h


def domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def site_covers(host: str, domain: str) -> bool:
    """Does a listed domain cover this host? "vercel.app" covers "x.vercel.app"."""
    return bool(domain) and domain_matches(host, domain)


def match_site(host: str, domains: list):
    return next((d for d in domains if site_covers(host, d)), None)


def find_item(items: list, kind: str, target: str):
    for it in items:
        if it["kind"] != kind:
            continue
        if kind == "site" and site_covers(target, it["target"]):
            return it
        if kind == "app" and it["target"].lower() == target.lower():
            return it
    return None


# ---------------------------------------------------------------- API key (root-only file)
def get_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        return KEY_PATH.read_text().strip() or None
    except OSError:
        return None


def api_key_saved() -> bool:
    return KEY_PATH.exists()


# ---------------------------------------------------------------- talking to the root service
class ServiceError(Exception):
    pass


def request(op: str, timeout: float = 5, **payload) -> dict:
    """Send one request to the root service and return its JSON reply."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(SOCKET_PATH)
            s.sendall(json.dumps({"op": op, **payload}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout) as e:
        raise ServiceError("FocusGuard's background service isn't responding. "
                           "Try again in a few seconds.") from e
    try:
        reply = json.loads(buf)
    except json.JSONDecodeError as e:
        raise ServiceError("Got a garbled reply from FocusGuard's service.") from e
    if reply.get("error"):
        raise ServiceError(reply["error"])
    return reply
