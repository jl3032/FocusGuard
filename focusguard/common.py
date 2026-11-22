"""Shared config/state storage, matching helpers and API-key storage.

Everything lives in the current user's home folder, so FocusGuard only ever
affects the macOS user account it was installed in.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import getpass
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path.home() / "Library" / "Application Support" / "FocusGuard"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
LOCK_PATH = APP_DIR / ".lock"
LOG_PATH = APP_DIR / "activity.log"
REPO_DIR = Path(__file__).resolve().parent.parent
BLOCK_PAGE = REPO_DIR / "assets" / "blocked.html"
KEYCHAIN_SERVICE = "FocusGuard-Anthropic"

DEFAULT_CONFIG = {
    "onboarded": False,
    "allowed_apps": [],          # app display names, e.g. "Safari", "Notion"
    "allowed_sites": [],         # bare domains, e.g. "github.com"
    "default_session_minutes": 120,
    "distraction_penalty_minutes": 60,
    "max_grant_minutes": 90,
    "on_task_check_seconds": 60,  # how often unlocked items are re-checked
    # AI provider: "ollama" (free, runs locally, no key) or "anthropic" (Claude API key)
    "provider": "ollama",
    "ollama_model": "qwen2.5:7b",
    "ollama_url": "http://localhost:11434",
    "claude_model": "claude-opus-5-5",
}

DEFAULT_STATE = {
    "session": None,      # {"started_at": ts, "ends_at": ts}
    "grants": [],         # temporary AI unlocks
    "penalties": [],      # 1-hour distraction blocks
    "recent_blocks": [],  # what was blocked lately (gives the AI context)
}


# ---------------------------------------------------------------- storage
@contextlib.contextmanager
def _locked():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path, default: dict) -> dict:
    merged = copy.deepcopy(default)
    try:
        merged.update(json.loads(path.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return merged


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _prune(st: dict) -> dict:
    now = time.time()
    st["grants"] = [g for g in st["grants"] if g["expires_at"] > now]
    st["penalties"] = [p for p in st["penalties"] if p["until"] > now]
    st["recent_blocks"] = st["recent_blocks"][-15:]
    return st


def load_config() -> dict:
    with _locked():
        return _read(CONFIG_PATH, DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    with _locked():
        _write(CONFIG_PATH, cfg)


def load_state() -> dict:
    with _locked():
        return _prune(_read(STATE_PATH, DEFAULT_STATE))


def update_state(fn) -> dict:
    """Read-modify-write the state under a lock (daemon and GUI share it)."""
    with _locked():
        st = _prune(_read(STATE_PATH, DEFAULT_STATE))
        fn(st)
        _write(STATE_PATH, st)
        return st


def session_active(st: dict) -> bool:
    s = st.get("session")
    return bool(s and s["ends_at"] > time.time())


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def log(msg: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


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


def find_item(items: list, kind: str, target: str):
    for it in items:
        if it["kind"] != kind:
            continue
        if kind == "site" and domain_matches(target, it["target"]):
            return it
        if kind == "app" and it["target"].lower() == target.lower():
            return it
    return None


# ---------------------------------------------------------------- API key (Keychain)
def get_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    r = subprocess.run(
        ["security", "find-generic-password", "-a", getpass.getuser(),
         "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def set_api_key(key: str) -> bool:
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", getpass.getuser(),
         "-s", KEYCHAIN_SERVICE, "-w", key.strip()],
        capture_output=True, text=True,
    )
    return r.returncode == 0
