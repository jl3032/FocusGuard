"""Admin tool. Runs as root, only behind macOS's administrator password dialog
(see macos.run_admin). This is the only way to change settings or pause blocking."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .common import (AGENT_LABEL, DEFAULT_CONFIG, EDITABLE_KEYS, KEY_PATH, load_config, log,
                     normalize_domain, save_config, update_state)

TESTING = os.environ.get("FOCUSGUARD_TEST") == "1"


def _names(v) -> list:
    return sorted({str(x).strip() for x in v if str(x).strip()}, key=str.lower) if isinstance(v, list) else []


def _limited(v, site: bool) -> list:
    out = {}
    for it in v if isinstance(v, list) else []:
        try:
            target = str(it["target"]).strip()
            minutes = max(1, min(600, int(it["minutes"])))
        except (KeyError, TypeError, ValueError):
            continue
        target = normalize_domain(target) if site else target
        if target:
            out[target.lower()] = {"target": target, "minutes": minutes}
    return sorted(out.values(), key=lambda x: x["target"].lower())


def clean_config(data: dict, cfg: dict) -> dict:
    """Merge settings from the setup window into cfg, keeping only valid values."""
    cfg = dict(cfg)
    if "allowed_apps" in data:
        cfg["allowed_apps"] = _names(data["allowed_apps"])
    if "allowed_sites" in data:
        cfg["allowed_sites"] = sorted({normalize_domain(str(s)) for s in data["allowed_sites"]} - {""})
    if "limited_apps" in data:
        cfg["limited_apps"] = _limited(data["limited_apps"], site=False)
    if "limited_sites" in data:
        cfg["limited_sites"] = _limited(data["limited_sites"], site=True)
    if isinstance(data.get("app_ids"), dict):
        cfg["app_ids"] = {str(k).lower(): {"path": str(v.get("path", "")),
                                           "bundle_id": str(v.get("bundle_id", ""))}
                          for k, v in data["app_ids"].items() if isinstance(v, dict)}
    for key, lo, hi in (("distraction_penalty_minutes", 5, 600), ("max_grant_minutes", 5, 240),
                        ("on_task_check_seconds", 20, 600)):
        if key in data:
            try:
                cfg[key] = max(lo, min(hi, int(data[key])))
            except (TypeError, ValueError):
                pass
    if data.get("provider") in ("ollama", "anthropic"):
        cfg["provider"] = data["provider"]
    for key in ("ollama_model", "claude_model"):
        if isinstance(data.get(key), str) and data[key].strip():
            cfg[key] = data[key].strip()
    unknown = set(data) - EDITABLE_KEYS - {"_api_key"}
    if unknown:
        log(f"admin: ignored unknown settings {sorted(unknown)}")
    cfg["onboarded"] = True
    return cfg


def kick_agent(cfg: dict):
    if cfg.get("uid"):
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{cfg['uid']}/{AGENT_LABEL}"],
                       capture_output=True)


def main(argv: list) -> int:
    if os.geteuid() != 0 and not TESTING:
        print("This needs administrator rights.", file=sys.stderr)
        return 1
    if len(argv) < 2:
        print("usage: admin.py set-config FILE | pause MINUTES", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "set-config" and len(argv) == 3:
        src = Path(argv[2])
        data = json.loads(src.read_text())
        cfg = clean_config(data, {**DEFAULT_CONFIG, **load_config()})
        save_config(cfg)
        key = data.get("_api_key")
        if isinstance(key, str) and key.strip():
            fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key.strip())
            os.chmod(KEY_PATH, 0o600)
        try:
            src.unlink()
        except OSError:
            pass
        log("settings changed (administrator approved)")
        kick_agent(cfg)
        print("saved")
        return 0
    if cmd == "pause" and len(argv) == 3:
        minutes = max(1, min(240, int(argv[2])))
        update_state(lambda s: s.update(paused_until=time.time() + minutes * 60))
        log(f"blocking paused for {minutes}m (administrator approved)")
        print("paused")
        return 0
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
