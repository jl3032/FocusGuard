"""Automatic updates, run by the root service.

Checks GitHub for a new commit on the main branch, downloads it, makes sure the
new code at least compiles and imports, swaps it in (keeping the previous copy
for rollback) and restarts. Settings and state are untouched. Changes to the
installer itself (launchd plists, the Python environment) still need a reinstall.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .common import AGENT_LABEL, APP_DIR, REPO_DIR, load_config, log

REPO = "jl3032/FocusGuard"
BRANCH = "main"
VERSION_PATH = APP_DIR / "version.txt"
TOKEN_PATH = APP_DIR / "github_token"      # root-only; needed while the repo is private
CHECK_EVERY = 6 * 3600
FIRST_CHECK_AFTER = 120
NEEDED = ("focusguard/rootd.py", "focusguard/agent.py", "focusguard/gui.py",
          "focusguard/admin.py", "launch/root.py", "assets/blocked.html")


def _headers(**extra) -> dict:
    h = {"User-Agent": "FocusGuard-updater", **extra}
    try:
        token = TOKEN_PATH.read_text().strip()
    except OSError:
        token = ""
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class UpdateError(Exception):
    pass


def installed_version() -> str:
    try:
        return VERSION_PATH.read_text().strip()
    except OSError:
        return ""


def latest_version() -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                 headers=_headers(Accept="application/vnd.github.sha"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            sha = r.read().decode().strip()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise UpdateError("GitHub refused: the repository is private and no valid access "
                              "token is installed") from e
        raise UpdateError(f"GitHub error {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"couldn't reach GitHub: {e}") from e
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise UpdateError("GitHub gave an unexpected answer")
    return sha


def _safe_extract(tar_path: Path, dest: Path) -> Path:
    """Extract only plain files and folders, never outside dest. Returns the top folder."""
    with tarfile.open(tar_path) as tar:
        tops = set()
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if not parts or m.name.startswith("/") or ".." in parts:
                raise UpdateError(f"unsafe path in download: {m.name}")
            if not (m.isfile() or m.isdir()):
                continue   # skip links and special files
            tops.add(parts[0])
            target = dest.joinpath(*parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o100 else 0o644)
    if len(tops) != 1:
        raise UpdateError("download had an unexpected layout")
    return dest / tops.pop()


def _check_new_code(new: Path):
    missing = [p for p in NEEDED if not (new / p).is_file()]
    if missing:
        raise UpdateError(f"download is missing {', '.join(missing)}")
    test = ("import sys, glob; sys.path.insert(0, sys.argv[1]); "
            "[compile(open(f).read(), f, 'exec') for f in glob.glob(sys.argv[1] + '/launch/*.py')]; "
            "import focusguard.rootd, focusguard.agent, focusguard.admin, focusguard.updater")
    r = subprocess.run([sys.executable, "-I", "-c", test, str(new)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise UpdateError("new version doesn't load: " + r.stderr.strip()[-300:])


def _install(new: Path):
    """Swap the new code in place of the running copy; keep the old one as app.prev."""
    app = REPO_DIR                       # .../FocusGuard/app
    staged = app.with_name("app.new")
    prev = app.with_name("app.prev")
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir()
    for item in ("focusguard", "assets", "launch", "requirements.txt"):
        src = new / item
        if src.is_dir():
            shutil.copytree(src, staged / item)
        elif src.is_file():
            shutil.copy2(src, staged / item)
    subprocess.run([sys.executable, "-m", "compileall", "-q", str(staged)], capture_output=True)
    if (new / "uninstall_root.sh").is_file():
        shutil.copy2(new / "uninstall_root.sh", APP_DIR / "uninstall_root.sh")
    if os.geteuid() == 0:
        subprocess.run(["chown", "-R", "root:wheel", str(staged)], capture_output=True)
    subprocess.run(["chmod", "-R", "go-w", str(staged)], capture_output=True)
    old_reqs = (app / "requirements.txt").read_text() if (app / "requirements.txt").exists() else ""
    shutil.rmtree(prev, ignore_errors=True)
    os.rename(app, prev)
    os.rename(staged, app)
    if (app / "requirements.txt").read_text() != old_reqs:
        pip = Path(sys.executable).with_name("pip")
        subprocess.run([str(pip), "install", "--quiet", "-r", str(app / "requirements.txt")],
                       capture_output=True, timeout=600)


def update(force: bool = False) -> str:
    """Install the latest version if there is one. Returns a short status message.
    On success the root service must restart to run the new code."""
    latest = latest_version()
    if latest == installed_version() and not force:
        return "up to date"
    with tempfile.TemporaryDirectory(prefix="focusguard-update-") as tmp:
        tmp = Path(tmp)
        os.chmod(tmp, 0o700)
        tar_path = tmp / "src.tar.gz"
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/tarball/{latest}",
                                     headers=_headers())
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(tar_path, "wb") as out:
                shutil.copyfileobj(r, out)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise UpdateError(f"download failed: {e}") from e
        new = _safe_extract(tar_path, tmp / "x")
        _check_new_code(new)
        _install(new)
    VERSION_PATH.write_text(latest + "\n")
    os.chmod(VERSION_PATH, 0o644)
    log(f"updated to {latest[:7]}")
    return f"updated to {latest[:7]}"


def restart_everything():
    """Restart the helper and then this root service (launchd starts it again)."""
    cfg = load_config()
    if cfg.get("uid"):
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{cfg['uid']}/{AGENT_LABEL}"],
                       capture_output=True)
    os._exit(0)


def check_and_apply(force: bool = False) -> str:
    try:
        msg = update(force)
    except UpdateError as e:
        log(f"update check failed: {e}")
        return f"Update failed: {e}"
    except Exception as e:  # noqa: BLE001
        log(f"update crashed: {e!r}")
        return f"Update failed: {e}"
    if msg.startswith("updated"):
        # Give the caller a moment to get its reply before the service restarts.
        threading.Timer(2, restart_everything).start()
    return msg


def loop():
    time.sleep(FIRST_CHECK_AFTER)
    while True:
        check_and_apply()
        time.sleep(CHECK_EVERY)


def status() -> dict:
    return {"version": installed_version()[:7] or "unknown"}


if __name__ == "__main__":
    print(json.dumps({"result": check_and_apply()}))
