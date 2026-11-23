"""Decision engine. Runs inside the root service (rootd).

It never touches the user's session itself: the user-session helper (agent.py)
reports what is on screen, and this returns what to do (quit an app, redirect a
tab, show a notification). Because the decisions and all state live in a root
process, the blocked user can't switch them off or edit them.
"""
from __future__ import annotations

import time
import traceback
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from . import ai
from .common import (find_item, host_of, limited_items, load_config, load_state, log,
                     match_site, paused, seconds_left, update_state, usage_key)
from .macos import (BROWSERS, UNMONITORABLE_BROWSERS, FrontApp, always_allowed,
                    block_page_url, is_system_process)

DWELL_SECONDS = 4      # be on a site this long before the AI starts judging its pages
NOTIFY_COOLDOWN = 20
MAX_TICK_CREDIT = 5    # seconds counted per tick at most (ignores sleep/wake gaps)
WARN_AT = (300, 60)    # warn when this many seconds of a daily allowance are left
PYTHON_NAMES = {"Python", "python3", "python", "Python Launcher"}
# Folders a standard user can't write to: an app there with the right bundle ID is
# the real thing even if it moved (e.g. after an update).
PROTECTED_APP_DIRS = ("/Applications/", "/System/Applications/")


class Daemon:
    def __init__(self, trusted_gui=lambda pid: False):
        self.trusted_gui = trusted_gui      # pid -> is it FocusGuard's own window?
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.pending = {}                   # key -> (kind, info, future)
        self.distraction_cache = {}         # url -> bool
        self.current_host = None            # site the AI is currently watching
        self.host_since = 0.0
        self.samples = defaultdict(lambda: deque(maxlen=12))  # grant id -> activity
        self.last_check = {}                # grant id -> ts
        self.last_notify = {}
        self.last_tick = time.time()
        self.warned = set()                 # (day, usage key, threshold)
        self.was_paused = False
        self.actions = []
        self.want_titles = set()            # apps whose window title we want next time

    # ------------------------------------------------------------ entry point
    def tick(self, obs: dict) -> list:
        """One check. obs = {"app": {...FrontApp fields} | None, "tab": [url, title] | None,
        "tab_denied": bool, "title": str, "no_agent": bool}. Returns actions to carry out."""
        self.actions = []
        try:
            self._tick(obs)
        except Exception:
            log("check error:\n" + traceback.format_exc())
        return self.actions

    def _tick(self, obs: dict):
        cfg = load_config()
        if not cfg["onboarded"]:
            return
        now = time.time()
        self.elapsed = min(max(0.0, now - self.last_tick), MAX_TICK_CREDIT)
        self.last_tick = now
        self.collect_ai_results(cfg)
        st = load_state()
        if paused(st):
            self.was_paused = True
            return               # no blocking, no AI checks, limited time doesn't count
        if self.was_paused:
            self.was_paused = False
            self.pending.clear()
            self.notify("FocusGuard", "The pause is over. Blocking is back on.")
            log("pause ended")

        if not obs.get("app"):
            return
        front = FrontApp(**obs["app"])
        if is_system_process(front) or always_allowed(front):
            return
        name = front.name
        if name in PYTHON_NAMES and self.trusted_gui(front.pid):
            return

        if name in UNMONITORABLE_BROWSERS:
            self.block_app(front, f"{name} can't be monitored; use Safari or Chrome")
            return
        if find_item(st["penalties"], "app", name):
            self.block_app(front, "blocked for distracting use")
            return
        grant = find_item(st["grants"], "app", name)
        allowed = (name.lower() in {a.lower() for a in cfg["allowed_apps"]}
                   and self.is_real_app(front, cfg))
        if not allowed and not grant:
            limited = find_item(limited_items(cfg), "app", name)
            if not limited or not self.is_real_app(front, cfg):
                self.block_app(front, "not on your allowed list")
            elif not self.spend(limited, st):
                self.block_app(front, "today's time is used up; it unblocks at midnight")
            return
        if grant:
            self.want_titles.add(name)
            title = obs.get("title") or ""
            self.watch_grant(grant, f"App {name}" + (f" — window: {title}" if title else ""), cfg)
        if name in BROWSERS:
            self.handle_browser(front, obs, cfg, st)

    def is_real_app(self, front: FrontApp, cfg: dict) -> bool:
        """Is this the app that was picked in setup, not a renamed copy of something else?"""
        rec = cfg["app_ids"].get(front.name.lower())
        if not rec:
            return True          # typed in by hand: only the name is known
        if rec.get("path") and front.path == rec["path"]:
            return True
        return bool(rec.get("bundle_id") and front.bundle_id == rec["bundle_id"]
                    and (front.path.startswith(PROTECTED_APP_DIRS)
                         or "/AppTranslocation/" in front.path))

    # ------------------------------------------------------------ browsers
    def handle_browser(self, front: FrontApp, obs: dict, cfg: dict, st: dict):
        app = front.name
        if obs.get("no_agent"):
            # The helper that reads tabs isn't running (was it killed?). Fail closed.
            self.block_app(front, "FocusGuard can't see its websites right now")
            return
        if obs.get("tab_denied"):
            self.no_permission(app)
            self.block_app(front, "FocusGuard isn't allowed to check its websites. Turn it on "
                                  "in System Settings > Privacy & Security > Automation")
            return
        if app in st["no_permission"]:
            update_state(lambda s: s["no_permission"].pop(app, None))
        tab = obs.get("tab")
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
        allowed_domain = match_site(host, cfg["allowed_sites"])
        if allowed_domain:
            self.check_distraction(app, host, allowed_domain, url, title, cfg)
            return
        limited = find_item(limited_items(cfg), "site", host)
        if limited:
            if seconds_left(limited, st) <= 0:
                self.block_site(app, host, "Today's time on this site is used up. "
                                           "It unblocks at midnight.")
                return
            self.check_distraction(app, host, limited["target"], url, title, cfg, limited=True)
            if find_item(load_state()["penalties"], "site", host):
                return   # judged distracting; the tab was redirected
            self.spend(limited, st)
            return
        self.block_site(app, host, "Not on your allowed list. Open FocusGuard and tell it why you need this.")

    def check_distraction(self, app, host, allowed_domain, url, title, cfg, limited=False):
        """Have the AI judge every page on a work or limited site; block the site if it's
        distracting. On limited sites the AI judges by what the site is for."""
        key = url[:400]
        cached = self.distraction_cache.get(key)
        if cached is not None:
            if cached:   # came back to a page already judged distracting
                self.add_penalty(allowed_domain, "You went back to a distracting page.", cfg)
                self.block_site(app, host, "Distracting content — site blocked for an hour.")
            return
        now = time.time()
        # The wait is per site, not per page, so fast scrolling (Shorts, Reels)
        # can't dodge the check by changing page every few seconds.
        if host != self.current_host:
            self.current_host, self.host_since = host, now
        if now - self.host_since < DWELL_SECONDS:
            return
        if any(k == "distraction" and i["host"] == host for k, i, _ in self.pending.values()):
            return   # one check per site at a time; the next page is judged when it's done
        fut = self.pool.submit(ai.check_distraction, url, title, cfg["allowed_sites"],
                               allowed_domain if limited else "")
        self.pending[("distraction", key)] = ("distraction",
                                              {"domain": allowed_domain, "key": key, "host": host},
                                              fut)

    def no_permission(self, app: str):
        update_state(lambda s: s["no_permission"].__setitem__(app, time.time()))
        key = "perm:" + app
        if time.time() - self.last_notify.get(key, 0) > 300:
            self.last_notify[key] = time.time()
            self.notify("FocusGuard can't check websites",
                        f"Allow it to control {app}: System Settings > Privacy & Security > "
                        "Automation > python > turn on " + app)
            log(f"automation permission denied for {app}; closing it until allowed")

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
                self.notify("FocusGuard", f"{item['target']}: {max(1, round(left / 60))} min "
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
                self.want_titles.discard(g["target"])
                self.notify("FocusGuard — access revoked", f"{g['target']}: {result['reason']}")
                log(f"revoked {g['kind']} {g['target']}: {result['reason']}")
                # The next tick blocks it because the grant is gone.

    # ------------------------------------------------------------ actions
    def notify(self, title: str, msg: str):
        self.actions.append({"do": "notify", "title": title, "msg": msg})

    def add_penalty(self, domain: str, reason: str, cfg: dict):
        minutes = cfg["distraction_penalty_minutes"]

        def add(s):
            if not find_item(s["penalties"], "site", domain):
                s["penalties"].append({"kind": "site", "target": domain, "reason": reason,
                                       "until": time.time() + minutes * 60})
        update_state(add)
        self.notify("FocusGuard", f"{domain} blocked for {minutes} min: {reason}")
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
            self.notify("FocusGuard", msg)

    def block_app(self, front: FrontApp, reason: str):
        self.actions.append({"do": "quit", "name": front.name, "pid": front.pid,
                             "bundle_id": front.bundle_id})
        self.record_block("app", front.name, reason)
        self.maybe_notify(front.name, f"{front.name} is blocked ({reason}). "
                                      "Open FocusGuard if you need it.")
        log(f"blocked app {front.name}: {reason}")

    def block_site(self, app: str, host: str, reason: str):
        self.actions.append({"do": "redirect", "app": app, "url": block_page_url(host, reason)})
        self.record_block("site", host, reason)
        self.maybe_notify(host, f"{host} is blocked. {reason}")
        log(f"blocked site {host}: {reason}")
