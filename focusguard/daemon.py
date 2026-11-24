"""Decision engine. Runs inside the root service (rootd).

It never touches the user's session itself: the user-session helper (agent.py)
reports what is on screen, and this returns what to do (quit an app, redirect a
tab, show a notification). Because the decisions and all state live in a root
process, the blocked user can't switch them off or edit them.
"""
from __future__ import annotations

import time
import traceback
from urllib.parse import urlparse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from . import ai
from .common import (find_item, host_of, limited_items, load_config, load_state, log,
                     match_site, paused, seconds_left, update_state, usage_key)
from .macos import (BROWSERS, UNMONITORABLE_BROWSERS, FrontApp, always_allowed,
                    block_page_url, hardware, is_system_process)

DWELL_SECONDS = 4      # be on a site this long before the AI starts judging its pages
SETTLE_CHECKS = 2      # see the same address + title this many checks in a row before judging
                       # (YouTube changes the address before the title catches up)
STRIKE_WINDOW = 30 * 60   # a 2nd distracting page on a site within this long blocks the whole site
NOTIFY_COOLDOWN = 20
DENIED_GRACE = 10      # seconds a browser must keep refusing before FocusGuard closes it
# How often the AI looks at the screen while a page stays open (a page can turn into a game
# without its address or title changing). Looking at a picture is slow on Intel Macs.
SCREEN_EVERY_APPLE, SCREEN_EVERY_INTEL = 45, 120
BLOCK_NOW_SECONDS = 120   # a verdict still blocks the user's current tab on that site this long
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
        self.page = ("", "", 0)             # (url, title, checks seen in a row)
        self.strikes = defaultdict(list)    # site -> times a page on it was judged distracting
        self.block_now = {}                 # host -> (reason, ts): block the current tab there
        self.samples = defaultdict(lambda: deque(maxlen=12))  # grant id -> activity
        self.last_check = {}                # grant id -> ts
        self.last_notify = {}
        self.last_tick = time.time()
        self.warned = set()                 # (day, usage key, threshold)
        self.was_paused = False
        self.actions = []
        self.want_titles = set()            # apps whose window title we want next time
        self.want_screen = False            # ask the helper for a screenshot next time
        self.denied_since = {}              # browser -> when it started refusing us
        self.screen_seen = {}               # page key -> when the AI last looked at it
        self.obs = {}
        self.screen_every = (SCREEN_EVERY_APPLE if hardware()["apple_silicon"]
                             else SCREEN_EVERY_INTEL)

    # ------------------------------------------------------------ entry point
    def tick(self, obs: dict) -> list:
        """One check. obs = {"app": {...FrontApp fields} | None, "tab": [url, title] | None,
        "tab_denied": bool, "title": str, "no_agent": bool}. Returns actions to carry out."""
        self.actions = []
        self.obs, self.want_screen = obs, False
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
            # Fail closed (otherwise turning the permission off would open every site),
            # but not on a single hiccup: warn first, close only if it keeps refusing.
            since = self.denied_since.setdefault(app, time.time())
            self.no_permission(app)
            if time.time() - since >= DENIED_GRACE:
                self.block_app(front, "FocusGuard isn't allowed to check its websites. Turn it "
                                      "on in System Settings > Privacy & Security > Automation")
            return
        self.denied_since.pop(app, None)
        if app in st["no_permission"]:
            update_state(lambda s: s["no_permission"].pop(app, None))
        if obs.get("screen_denied") and time.time() - self.last_notify.get("perm:screen", 0) > 600:
            self.last_notify["perm:screen"] = time.time()
            self.notify("FocusGuard can't see the screen",
                        "Turn on Screen Recording for python: System Settings > Privacy & "
                        "Security > Screen Recording.")
            log("screen recording permission missing; judging by page titles only")
        tab = obs.get("tab")
        if not tab:
            return
        url, title = tab
        host = host_of(url)
        if not host or host in ("localhost", "127.0.0.1"):
            return

        penalty = find_item(st["penalties"], "site", host)
        grant = find_item(st["grants"], "site", host)
        # An unlock the AI gave after the block (an appeal: "that was a TED talk") wins.
        if penalty and not (grant and grant["granted_at"] > penalty.get("at", 0)):
            until = time.strftime("%H:%M", time.localtime(penalty["until"]))
            self.block_site(app, host, f"Distracting use — blocked until {until}. {penalty['reason']} "
                                       "If this was a mistake, explain it to the AI in FocusGuard.")
            return
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
            if self.check_distraction(app, host, limited["target"], url, title, cfg, limited=True):
                return   # judged distracting; the tab was redirected
            self.spend(limited, st)
            return
        self.block_site(app, host, "Not on your allowed list. Open FocusGuard and tell it why you need this.")

    def check_distraction(self, app, host, allowed_domain, url, title, cfg, limited=False) -> bool:
        """Have the AI judge every page on a work or limited site. A distracting page is
        blocked; a second one on the same site soon after blocks the whole site.
        On limited sites the AI judges by what the site is for. Returns True if blocked."""
        # A verdict for a page the user has already scrolled past (Shorts, Reels, TikTok
        # change page every few seconds): block whatever they're on now on that site.
        hit = self.block_now.pop(host, None)
        if hit and time.time() - hit[1] < BLOCK_NOW_SECONDS:
            self.block_site(app, host, f"This looks distracting: {hit[0]} Another distracting "
                                       "page here will block the whole site for a while.")
            return True
        key = (url[:400], title[:200])
        if is_navigation_page(url):
            # Home and search pages: the title says little, so don't judge it. But a site's
            # front page can itself be a game, so still look at the screen.
            self.look_at_screen(key, host, allowed_domain, url, title, cfg, limited)
            return False
        cached = self.distraction_cache.get(key)
        if cached is not None:
            if cached:   # a page already judged distracting
                self.block_site(app, host, f'This page looks distracting ("{title[:80]}"). '
                                           "Another distracting page here will block the whole "
                                           "site for a while.")
                return True
            # The title looked fine; now look at what's actually on the screen.
            self.look_at_screen(key, host, allowed_domain, url, title, cfg, limited)
            return False
        now = time.time()
        # The wait is per site, not per page, so fast scrolling (Shorts, Reels)
        # can't dodge the check by changing page every few seconds.
        if host != self.current_host:
            self.current_host, self.host_since = host, now
        seen = self.page[2] + 1 if (url, title) == self.page[:2] else 1
        self.page = (url, title, seen)
        feed = is_feed_page(url)   # Shorts/Reels/TikTok: judge right away, one check only
        if (now - self.host_since < DWELL_SECONDS and not feed) or seen < SETTLE_CHECKS:
            return False   # also wait until the title matches the page
        if ai.chat_active.is_set():
            return False   # the user is waiting for the chat; check this page afterwards
        if any(k == "distraction" and i["host"] == host for k, i, _ in self.pending.values()):
            return False   # one check per site at a time; the next page is judged when it's done
        fut = self.pool.submit(ai.check_distraction, url, title, cfg["allowed_sites"],
                               allowed_domain if limited else "", quick=feed)
        self.pending[("distraction", key)] = ("distraction",
                                              {"domain": allowed_domain, "key": key, "host": host,
                                               "title": title},
                                              fut)
        return False

    def look_at_screen(self, key, host, domain, url, title, cfg, limited):
        if not cfg.get("screen_checks", True) or ai.chat_active.is_set():
            return
        if any(k == "screen" for k, _, _ in self.pending.values()):
            return   # one screenshot at a time
        if time.time() - self.screen_seen.get(key, 0) < self.screen_every:
            return
        shot = self.obs.get("screen")
        if not shot:
            self.want_screen = True   # the helper sends one with its next report
            return
        self.screen_seen[key] = time.time()
        if len(self.screen_seen) > 500:
            self.screen_seen = {key: time.time()}
        fut = self.pool.submit(ai.check_screen, shot, url, title, domain if limited else "")
        self.pending[("screen", key)] = ("screen", {"domain": domain, "key": key, "host": host,
                                                    "title": title}, fut)

    def no_permission(self, app: str):
        update_state(lambda s: s["no_permission"].__setitem__(app, time.time()))
        key = "perm:" + app
        if time.time() - self.last_notify.get(key, 0) > 300:
            self.last_notify[key] = time.time()
            self.notify("FocusGuard can't check websites",
                        f"Allow it to control {app}: System Settings > Privacy & Security > "
                        "Automation > python > turn on " + app)
            log(f"automation permission denied for {app}; closing it in {DENIED_GRACE}s "
                "unless allowed")

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
        if (time.time() - last < cfg["on_task_check_seconds"] or gid in self.pending
                or ai.chat_active.is_set()):
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

            if kind in ("distraction", "screen"):
                if len(self.distraction_cache) > 500:
                    self.distraction_cache.clear()
                if kind == "screen" and not result["distracting"]:
                    continue   # keep the title verdict; look again later
                self.distraction_cache[info["key"]] = result["distracting"]
                if result["distracting"]:
                    if kind == "screen":
                        result["reason"] = f'On screen: {result["topic"]}. {result["reason"]}' 
                    reason = f'{result["reason"]} (page: "{info["title"][:80]}")'
                    # Strikes and hour-long blocks hit only the exact site, so a game on
                    # one *.vercel.app site doesn't block his homework sites there.
                    domain, now = info["host"], time.time()
                    self.strikes[domain] = [t for t in self.strikes[domain]
                                            if now - t < STRIKE_WINDOW] + [now]
                    if len(self.strikes[domain]) >= 2:
                        self.strikes.pop(domain)
                        self.add_penalty(domain, reason, cfg)
                    else:
                        self.block_now[info["host"]] = (reason, now)
                        self.notify("FocusGuard — warning", f"Blocked one page: {reason} "
                                    f"Another distracting page will block {domain} for a while.")
                        log(f"warning on {domain}: {reason}")
                    # The next tick sees the verdict (or penalty) and redirects the tab.

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
                                       "at": time.time(), "until": time.time() + minutes * 60})
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


SEARCH_SEGMENTS = {"search", "results"}
NAV_PATHS = {"/feed/subscriptions", "/feed/you", "/feed/history", "/feed/library", "/feed/playlists"}


FEED_PATHS = ("youtube.com/shorts/", "instagram.com/reel/", "instagram.com/reels/",
              "facebook.com/reel/", "tiktok.com/@", "tiktok.com/foryou")


def is_feed_page(url: str) -> bool:
    """A clip in a swipe-to-the-next feed. These are judged without the usual waits,
    because each clip is only on screen for a few seconds."""
    u = url.lower().split("://", 1)[-1]
    u = u[4:] if u.startswith("www.") else u
    u = u[2:] if u.startswith("m.") else u
    return any(u.startswith(p) for p in FEED_PATHS)


def is_navigation_page(url: str) -> bool:
    """A site's home page or search results. Nothing is being watched or read there yet,
    and the AI kept guessing what they "might lead to"."""
    path = urlparse(url).path.rstrip("/").lower()
    return (path == "" or path in NAV_PATHS
            or any(seg in SEARCH_SEGMENTS for seg in path.split("/")))
