"""Background enforcer. Runs as a per-user LaunchAgent, so it only sees and
blocks things in the macOS account it was installed in."""
from __future__ import annotations

import time
import traceback
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from . import ai
from .common import (domain_matches, find_item, host_of, limited_items, load_config,
                     load_state, log, seconds_left, update_state, usage_key)
from .macos import (ALWAYS_ALLOWED, AutomationDenied, BROWSERS, UNMONITORABLE_BROWSERS, block_page_url,
                    browser_tab, frontmost_app, notify, quit_app, redirect_tab,
                    window_title)

POLL_SECONDS = 2.5
DWELL_SECONDS = 8      # stay on a page this long before the AI judges it
NOTIFY_COOLDOWN = 20
MAX_TICK_CREDIT = 5    # seconds counted per tick at most (ignores sleep/wake gaps)
WARN_AT = (300, 60)    # warn when this many seconds of a daily allowance are left

# Obvious time sinks on otherwise-useful sites: penalised instantly, no AI call.
QUICK_DISTRACTIONS = [
    "youtube.com/shorts", "instagram.com/reels", "instagram.com/explore",
    "facebook.com/watch", "facebook.com/reel", "reddit.com/r/all",
    "reddit.com/r/popular", "tiktok.com", "twitch.tv",
]


class Daemon:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.pending = {}                   # key -> (kind, info, future)
        self.distraction_cache = {}         # (host, title) -> bool
        self.first_seen = {}                # (host, title) -> ts
        self.samples = defaultdict(lambda: deque(maxlen=12))  # grant id -> activity
        self.last_check = {}                # grant id -> ts
        self.last_notify = {}
        self.last_tick = time.time()
        self.warned = set()                 # (day, usage key, threshold)

    # ------------------------------------------------------------ main loop
    def run(self):
        log("daemon started")
        while True:
            try:
                self.tick()
            except Exception:
                log("tick error:\n" + traceback.format_exc())
            time.sleep(POLL_SECONDS)

    def tick(self):
        cfg = load_config()
        if not cfg["onboarded"]:
            return
        now = time.time()
        self.elapsed = min(max(0.0, now - self.last_tick), MAX_TICK_CREDIT)
        self.last_tick = now
        self.collect_ai_results(cfg)
        st = load_state()

        front = frontmost_app()
        if not front or front.name in ALWAYS_ALLOWED:
            return
        name = front.name

        if name in UNMONITORABLE_BROWSERS:
            self.block_app(front, f"{name} can't be monitored; use Safari or Chrome")
            return
        if find_item(st["penalties"], "app", name):
            self.block_app(front, "blocked for distracting use")
            return
        grant = find_item(st["grants"], "app", name)
        allowed = name.lower() in {a.lower() for a in cfg["allowed_apps"]}
        if not allowed and not grant:
            limited = find_item(limited_items(cfg), "app", name)
            if not limited:
                self.block_app(front, "not on your allowed list")
            elif not self.spend(limited, st):
                self.block_app(front, "today's time is used up; "
                                      "it unblocks at midnight")
            return
        if grant:
            title = window_title(name)
            self.watch_grant(grant, f"App {name}" + (f" — window: {title}" if title else ""), cfg)
        if name in BROWSERS:
            self.handle_browser(name, cfg, st)

    # ------------------------------------------------------------ browsers
    def handle_browser(self, app: str, cfg: dict, st: dict):
        try:
            tab = browser_tab(app)
        except AutomationDenied:
            self.no_permission(app)
            return
        if app in st["no_permission"]:
            update_state(lambda s: s["no_permission"].pop(app, None))
        if not tab:
            return
        url, title = tab
        host = host_of(url)
        if not host or host in ("localhost", "127.0.0.1"):
            return

        penalty = find_item(st["penalties"], "site", host)
        if penalty:
            until = time.strftime("%H:%M", time.localtime(penalty["until"]))
            self.block_site(app, host, f"Distracting use — blocked until {until}. {penalty['reason']}")
            return
        grant = find_item(st["grants"], "site", host)
        if grant:
            self.watch_grant(grant, f"{title} — {url}", cfg)
            return
        allowed_domain = next((d for d in cfg["allowed_sites"] if domain_matches(host, d)), None)
        if allowed_domain:
            self.check_distraction(app, host, allowed_domain, url, title, cfg)
            return
        limited = find_item(limited_items(cfg), "site", host)
        if limited:
            if not self.spend(limited, st):
                self.block_site(app, host, "Today's time on this site is used up. "
                                           "It unblocks at midnight.")
            return
        self.block_site(app, host, "Not on your allowed list. Open FocusGuard and tell it why you need this.")

    def check_distraction(self, app, host, allowed_domain, url, title, cfg):
        lowered = url.lower()
        if any(p in lowered for p in QUICK_DISTRACTIONS):
            self.add_penalty(allowed_domain, "That part of the site is a known time sink.", cfg)
            self.block_site(app, host, "Distracting content — site blocked for an hour.")
            return
        key = (host, title.strip()[:200])
        if key in self.distraction_cache:
            if self.distraction_cache[key]:   # came back after a penalty expired
                self.add_penalty(allowed_domain, "You went back to a distracting page.", cfg)
                self.block_site(app, host, "Distracting content — site blocked for an hour.")
            return
        if key in self.pending:
            return
        now = time.time()
        self.first_seen = {k: t for k, t in self.first_seen.items() if now - t < 600}
        start = self.first_seen.setdefault(key, now)
        if now - start < DWELL_SECONDS:
            return
        fut = self.pool.submit(ai.check_distraction, url, title, cfg["allowed_sites"])
        self.pending[key] = ("distraction", {"domain": allowed_domain, "key": key}, fut)

    def no_permission(self, app: str):
        """Websites can't be checked in this browser until the user allows Automation."""
        update_state(lambda s: s["no_permission"].__setitem__(app, time.time()))
        key = "perm:" + app
        if time.time() - self.last_notify.get(key, 0) > 300:
            self.last_notify[key] = time.time()
            notify("FocusGuard can't check websites",
                   f"Allow it to control {app}: System Settings > Privacy & Security > "
                   "Automation > python > turn on " + app)
            log(f"automation permission denied for {app}; websites are not being checked")

    # ------------------------------------------------------------ limited items
    def spend(self, item: dict, st: dict) -> bool:
        """Count this tick against a limited item's daily allowance.
        Returns False when the allowance is already used up."""
        if seconds_left(item, st) <= 0:
            return False
        key = usage_key(item["kind"], item["target"])
        secs = self.elapsed

        def add(s):
            s["usage"]["seconds"][key] = s["usage"]["seconds"].get(key, 0) + secs
        st = update_state(add)
        left = seconds_left(item, st)
        day = st["usage"]["day"]
        crossed = [(day, key, t) for t in WARN_AT
                   if left <= t and (day, key, t) not in self.warned]
        if crossed:
            self.warned.update(crossed)
            if left > 0:
                notify("FocusGuard", f"{item['target']}: {max(1, round(left / 60))} min "
                                     "left for today.")
        if len(self.warned) > 200:
            self.warned = {m for m in self.warned if m[0] == day}
        if left <= 0:
            log(f"daily limit reached for {item['kind']} {item['target']}")
        return True

    # ------------------------------------------------------------ unlocked items
    def watch_grant(self, grant: dict, activity: str, cfg: dict):
        gid = grant["id"]
        dq = self.samples[gid]
        if not dq or dq[-1] != activity:
            dq.append(activity)
        last = self.last_check.get(gid, grant["granted_at"])
        if time.time() - last < cfg["on_task_check_seconds"] or gid in self.pending:
            return
        self.last_check[gid] = time.time()
        fut = self.pool.submit(ai.check_on_task, grant, list(dq))
        self.pending[gid] = ("grant", {"grant": grant}, fut)

    # ------------------------------------------------------------ AI results
    def collect_ai_results(self, cfg: dict):
        for key in [k for k, (_, _, f) in self.pending.items() if f.done()]:
            kind, info, fut = self.pending.pop(key)
            try:
                result = fut.result()
            except ai.AIError as e:
                log(f"AI check skipped ({kind}): {e}")
                continue
            except Exception:
                log("AI check crashed:\n" + traceback.format_exc())
                continue

            if kind == "distraction":
                if len(self.distraction_cache) > 500:
                    self.distraction_cache.clear()
                self.distraction_cache[info["key"]] = result["distracting"]
                if result["distracting"]:
                    self.add_penalty(info["domain"], result["reason"], cfg)
                    # The next tick sees the penalty and redirects the tab.

            elif kind == "grant" and not result["on_task"]:
                g = info["grant"]

                def revoke(s, gid=g["id"]):
                    s["grants"] = [x for x in s["grants"] if x["id"] != gid]
                    s["recent_blocks"].append({"kind": g["kind"], "target": g["target"],
                                               "at": time.time(),
                                               "reason": "off task: " + result["reason"]})
                update_state(revoke)
                self.samples.pop(g["id"], None)
                notify("FocusGuard — access revoked", f"{g['target']}: {result['reason']}")
                log(f"revoked {g['kind']} {g['target']}: {result['reason']}")
                # The next tick blocks it because the grant is gone.

    # ------------------------------------------------------------ actions
    def add_penalty(self, domain: str, reason: str, cfg: dict):
        minutes = cfg["distraction_penalty_minutes"]

        def add(s):
            if not find_item(s["penalties"], "site", domain):
                s["penalties"].append({"kind": "site", "target": domain, "reason": reason,
                                       "until": time.time() + minutes * 60})
        update_state(add)
        notify("FocusGuard", f"{domain} blocked for {minutes} min: {reason}")
        log(f"penalty {domain} {minutes}m: {reason}")

    def record_block(self, kind: str, target: str, reason: str):
        def add(s):
            last = s["recent_blocks"][-1] if s["recent_blocks"] else None
            if last and last["target"] == target and time.time() - last["at"] < 30:
                return
            s["recent_blocks"].append({"kind": kind, "target": target,
                                       "at": time.time(), "reason": reason})
        update_state(add)

    def maybe_notify(self, target: str, msg: str):
        if time.time() - self.last_notify.get(target, 0) > NOTIFY_COOLDOWN:
            self.last_notify[target] = time.time()
            notify("FocusGuard", msg)

    def block_app(self, front, reason: str):
        quit_app(front)
        self.record_block("app", front.name, reason)
        self.maybe_notify(front.name, f"{front.name} is blocked ({reason}). "
                                      "Open FocusGuard if you need it.")
        log(f"blocked app {front.name}: {reason}")

    def block_site(self, app: str, host: str, reason: str):
        redirect_tab(app, block_page_url(host, reason))
        self.record_block("site", host, reason)
        self.maybe_notify(host, f"{host} is blocked. {reason}")
        log(f"blocked site {host}: {reason}")


if __name__ == "__main__":
    Daemon().run()
