"""FocusGuard window: setup (work / limited lists), daily allowances, and the AI unblock chat."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .common import (DAY_NAMES, app_key, DAYS, DINNER_MINUTES, TASK_MAX_LINKS, TASK_MAX_MINUTES, mode,
                     today,
                     ServiceError, api_key_saved, gemini_key_saved, limited_items,
                     load_config, load_state, normalize_domain, paused, request, seconds_left)
from .ai import MODEL_SIZES, auto_size
from .macos import BROWSERS, UNMONITORABLE_BROWSERS, hardware, installed_apps, run_admin

COMMON_SITES = [
    "google.com", "docs.google.com", "drive.google.com", "mail.google.com",
    "calendar.google.com", "github.com", "stackoverflow.com", "wikipedia.org",
    "claude.ai", "notion.so", "canva.com", "office.com", "outlook.com",
    "slack.com", "zoom.us", "khanacademy.org", "youtube.com", "reddit.com",
]


def summarize_changes(old: dict, new: dict) -> str:
    """Short description of what a settings change allows, shown in the admin password
    dialog so whoever types the password knows what they're approving."""
    def names(cfg, key):
        return {(x["target"] if isinstance(x, dict) else x).lower() for x in cfg.get(key, [])}
    added = []
    for key in ("allowed_apps", "allowed_sites", "limited_apps", "limited_sites"):
        added += sorted(names(new, key) - names(old, key))
    more_time = [x["target"] for x in new.get("limited_apps", []) + new.get("limited_sites", [])
                 if any(o["target"].lower() == x["target"].lower() and x["minutes"] > o["minutes"]
                        for o in old.get("limited_apps", []) + old.get("limited_sites", []))]
    parts = []
    if added:
        parts.append("Newly allowed: " + ", ".join(added))
    if more_time:
        parts.append("More daily time for: " + ", ".join(more_time))
    text = ". ".join(parts) or "No new apps or sites are allowed."
    return text if len(text) < 300 else text[:297] + "…"


def fmt_minutes(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


class ScrollFrame(ttk.Frame):
    def __init__(self, parent, height=300):
        super().__init__(parent)
        canvas = tk.Canvas(self, height=height, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = ttk.Frame(canvas)
        self.inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta), "units"))


# ====================================================================== onboarding
class Onboarding(ttk.Frame):
    def __init__(self, app: "App"):
        super().__init__(app, padding=20)
        self.app = app
        self.cfg = load_config()
        self.step = 0
        self.app_vars = {}
        self.site_vars = {}
        self.extra_apps = tk.StringVar()
        self.key_var = tk.StringVar()
        self.gemini_key_var = tk.StringVar()
        self.app_info = installed_apps()     # name -> {"path", "bundle_id"}
        self.provider_var = tk.StringVar(value=self.cfg.get("provider", "ollama"))
        self.hw = hardware()
        self.size_names = {"auto": f"Automatic for this Mac: {MODEL_SIZES[auto_size(self.hw)]['label'].split(':')[0]} "
                                   f"({self.hw['chip']}, {self.hw['memory_gb']} GB) - recommended",
                           **{k: f"{v['label']} ({v['model']}"
                                 + (f" + {v['vision']}" if v['vision'] != v['model'] else "")
                                 + f", {v['gb']:g} GB download)"
                              for k, v in MODEL_SIZES.items()}}
        size = self.cfg.get("ai_model_size", "auto")
        self.size_var = tk.StringVar(value=self.size_names.get(size, self.size_names["auto"]))
        self.limited_apps = [dict(x) for x in self.cfg["limited_apps"]]
        self.limited_sites = [dict(x) for x in self.cfg["limited_sites"]]
        self.penalty_var = tk.IntVar(value=self.cfg["distraction_penalty_minutes"])
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self)
        nav.pack(fill="x", pady=(12, 0))
        self.back_btn = ttk.Button(nav, text="Back", command=self.back)
        self.back_btn.pack(side="left")
        if self.cfg["onboarded"]:
            ttk.Button(nav, text="Cancel", command=app.show_main).pack(side="left", padx=6)
        self.next_btn = ttk.Button(nav, text="Next", command=self.next)
        self.next_btn.pack(side="right")
        self.render()

    def clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def heading(self, title, sub):
        ttk.Label(self.body, text=title, font=("Helvetica", 20, "bold")).pack(anchor="w")
        ttk.Label(self.body, text=sub, wraplength=680, foreground="#555").pack(anchor="w", pady=(4, 12))

    def render(self):
        self.clear()
        self.back_btn.state(["disabled"] if self.step == 0 else ["!disabled"])
        self.next_btn.config(text="Finish" if self.step == 3 else "Next")
        [self.render_key, self.render_apps, self.render_sites, self.render_limited][self.step]()

    # step 0
    def render_key(self):
        self.heading("Welcome to FocusGuard",
                     "FocusGuard is always on. You pick the apps and websites you use for work; "
                     "those always work. Then you pick a few you're allowed for a limited time each "
                     "day (say chess.com for 10 minutes); after that they're blocked until midnight. "
                     "Everything else is blocked. If you need something that's blocked, tell the "
                     "built-in AI. It only applies to this Mac user account.\n\n"
                     "Choose which AI to use:")
        ttk.Radiobutton(self.body, variable=self.provider_var, value="gemini",
                        text="Free, recommended: Google Gemini online (nothing to install or "
                        "restart; page titles and screenshots go to Google)").pack(anchor="w", pady=2)
        ttk.Label(self.body, text="Free Gemini key from aistudio.google.com/apikey (sign in with "
                  "Google, click \"Create API key\"). If Gemini is offline or used up for the day, "
                  "the local AI below steps in when Ollama is installed:").pack(anchor="w", padx=24)
        ttk.Entry(self.body, textvariable=self.gemini_key_var, show="•", width=60
                  ).pack(anchor="w", padx=24)
        if gemini_key_saved():
            ttk.Label(self.body, text="A Gemini key is already saved. Leave this blank to keep it.",
                      foreground="#2a7").pack(anchor="w", padx=24, pady=4)
        ttk.Radiobutton(self.body, variable=self.provider_var, value="ollama",
                        text="Free: local AI on this Mac via Ollama (no key, nothing leaves your Mac)"
                        ).pack(anchor="w", pady=(10, 2))
        ttk.Label(self.body, text="Model size (bigger is smarter but slower; it downloads "
                  "by itself and the old one is used meanwhile):").pack(anchor="w", padx=24)
        ttk.Combobox(self.body, textvariable=self.size_var, state="readonly", width=70,
                     values=list(self.size_names.values())).pack(anchor="w", padx=24)
        if not self.hw["apple_silicon"]:
            ttk.Label(self.body, text="This is an Intel Mac, where the free AI is slow and "
                      "makes more mistakes. The Claude API below works much better here.",
                      foreground="#a60").pack(anchor="w", padx=24, pady=(2, 0))
        ttk.Radiobutton(self.body, variable=self.provider_var, value="anthropic",
                        text="Claude API (paid, smarter; needs a key from console.anthropic.com)"
                        ).pack(anchor="w", pady=(10, 2))
        ttk.Label(self.body, text="Claude API key (stored where only an administrator can read it):"
                  ).pack(anchor="w", padx=24)
        ttk.Entry(self.body, textvariable=self.key_var, show="•", width=60).pack(anchor="w", padx=24)
        if api_key_saved():
            ttk.Label(self.body, text="A key is already saved. Leave this blank to keep it.",
                      foreground="#2a7").pack(anchor="w", padx=24, pady=4)

    # step 1
    def render_apps(self):
        self.heading("Which apps do you use (or might use) for work?",
                     "These always work. Apps you leave unticked are blocked (you can give some a "
                     "daily time allowance on the last step). Tick your "
                     "browser (Safari, Chrome, Arc, Brave, Edge) if you want websites at all. "
                     "Firefox and Opera can't be monitored, so they're always blocked.")
        search = tk.StringVar()
        ttk.Entry(self.body, textvariable=search, width=40).pack(anchor="w", pady=(0, 6))
        sf = ScrollFrame(self.body, height=330)
        sf.pack(fill="both", expand=True)
        chosen = {a.lower() for a in self.cfg["allowed_apps"]}
        apps = [a for a in self.app_info if a not in UNMONITORABLE_BROWSERS]
        for name in apps:
            if name not in self.app_vars:
                self.app_vars[name] = tk.BooleanVar(value=name.lower() in chosen)
        rows = {}
        for i, name in enumerate(apps):
            label = name + ("  (browser)" if name in BROWSERS else "")
            cb = ttk.Checkbutton(sf.inner, text=label, variable=self.app_vars[name])
            cb.grid(row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)
            rows[name] = (cb, i)

        def refilter(*_):
            q = search.get().lower()
            visible = [n for n in apps if q in n.lower()]
            for cb, _ in rows.values():
                cb.grid_forget()
            for i, n in enumerate(visible):
                rows[n][0].grid(row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)
        search.trace_add("write", refilter)
        ttk.Label(self.body, text="Other app names (comma-separated, as shown in the menu bar):"
                  ).pack(anchor="w", pady=(8, 0))
        if not self.extra_apps.get():
            known = {a.lower() for a in apps}
            self.extra_apps.set(", ".join(a for a in self.cfg["allowed_apps"] if a.lower() not in known))
        ttk.Entry(self.body, textvariable=self.extra_apps, width=70).pack(anchor="w")

    # step 2
    def render_sites(self):
        self.heading("Which websites do you use for work?",
                     "These always work; other sites are blocked (you can give some a daily time "
                     "allowance on the next step). Allowed sites are still watched: if you start "
                     "doing something distracting on one (e.g. gaming videos on YouTube), that page "
                     "is blocked; do it twice and the site is blocked for an hour. Subdomains are included: "
                     "vercel.app covers every site ending in .vercel.app.")
        grid = ttk.Frame(self.body)
        grid.pack(anchor="w", fill="x")
        chosen = set(self.cfg["allowed_sites"])
        for i, site in enumerate(COMMON_SITES):
            if site not in self.site_vars:
                self.site_vars[site] = tk.BooleanVar(value=site in chosen)
            ttk.Checkbutton(grid, text=site, variable=self.site_vars[site]).grid(
                row=i // 3, column=i % 3, sticky="w", padx=6, pady=2)
        ttk.Label(self.body, text="More sites, one per line:").pack(anchor="w", pady=(10, 2))
        if not hasattr(self, "sites_text_value"):
            self.sites_text_value = "\n".join(s for s in self.cfg["allowed_sites"] if s not in COMMON_SITES)
        self.sites_text = tk.Text(self.body, height=9, width=60)
        self.sites_text.insert("1.0", self.sites_text_value)
        self.sites_text.pack(anchor="w", fill="x")

    # step 3
    def render_limited(self):
        self.heading("Allowed for a limited time each day",
                     "These work until their daily time runs out, then they're blocked until "
                     "midnight. Time only counts while the app or site is on screen. You get a "
                     "warning 5 minutes and 1 minute before.")
        cols = ttk.Frame(self.body)
        cols.pack(fill="both", expand=True)
        apps = [a for a in self.app_info
                if a not in BROWSERS and a not in UNMONITORABLE_BROWSERS]
        self.limited_editor(cols, "Apps (e.g. Steam)", self.limited_apps, apps, 0)
        self.limited_editor(cols, "Websites (e.g. chess.com)", self.limited_sites, None, 1)
        cols.columnconfigure(0, weight=1)
        cols.columnconfigure(1, weight=1)
        row = ttk.Frame(self.body)
        row.pack(anchor="w", pady=(12, 0))
        ttk.Label(row, text="If you use a work site for distraction, block it for (minutes):"
                  ).pack(side="left")
        ttk.Spinbox(row, from_=10, to=240, increment=10, textvariable=self.penalty_var,
                    width=6).pack(side="left", padx=6)

    def limited_editor(self, parent, title, items: list, choices, col: int):
        box = ttk.LabelFrame(parent, text=title, padding=8)
        box.grid(row=0, column=col, sticky="nsew", padx=4)
        name = tk.StringVar()
        minutes = tk.IntVar(value=10)
        add_row = ttk.Frame(box)
        add_row.pack(fill="x")
        if choices is None:
            ttk.Entry(add_row, textvariable=name, width=20).pack(side="left")
        else:
            ttk.Combobox(add_row, textvariable=name, values=choices, width=18).pack(side="left")
        ttk.Spinbox(add_row, from_=1, to=240, increment=5, textvariable=minutes,
                    width=4).pack(side="left", padx=4)
        ttk.Label(add_row, text="min/day").pack(side="left")
        listbox = tk.Listbox(box, height=10)
        listbox.pack(fill="both", expand=True, pady=6)

        def redraw():
            listbox.delete(0, "end")
            for it in items:
                listbox.insert("end", f"{it['target']} — {it['minutes']} min/day")

        def add():
            raw = name.get().strip()
            target = normalize_domain(raw) if choices is None else raw
            try:
                mins = max(1, int(minutes.get()))
            except (tk.TclError, ValueError):
                mins = 10
            if not target:
                return
            items[:] = [it for it in items if it["target"].lower() != target.lower()]
            items.append({"target": target, "minutes": mins})
            items.sort(key=lambda it: it["target"].lower())
            name.set("")
            redraw()

        def remove():
            for i in reversed(listbox.curselection()):
                del items[i]
            redraw()
        btns = ttk.Frame(box)
        btns.pack(fill="x")
        ttk.Button(add_row, text="Add", command=add).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Remove selected", command=remove).pack(side="left")
        redraw()

    def save_step(self):
        if self.step == 0:
            self.cfg["provider"] = self.provider_var.get()
            key = self.key_var.get().strip()
            if self.cfg["provider"] == "anthropic" and not key and not api_key_saved():
                messagebox.showwarning("FocusGuard", "Paste your Claude API key, or choose the free local AI.")
                return False
            if (self.cfg["provider"] == "gemini" and not self.gemini_key_var.get().strip()
                    and not gemini_key_saved()):
                messagebox.showwarning("FocusGuard", "Paste your free Gemini key "
                                       "(from aistudio.google.com/apikey), or choose another AI.")
                return False
        elif self.step == 2:
            self.sites_text_value = self.sites_text.get("1.0", "end")
        return True

    def back(self):
        if self.step == 2:
            self.sites_text_value = self.sites_text.get("1.0", "end")
        self.step -= 1
        self.render()

    def next(self):
        if not self.save_step():
            return
        if self.step < 3:
            self.step += 1
            self.render()
            return
        apps = [n for n, v in self.app_vars.items() if v.get()]
        apps += [a.strip() for a in self.extra_apps.get().split(",") if a.strip()]
        sites = [s for s, v in self.site_vars.items() if v.get()]
        sites += [normalize_domain(l) for l in self.sites_text_value.splitlines()]
        sites = sorted({s for s in sites if s})
        if not apps:
            messagebox.showwarning("FocusGuard", "Pick at least one app.")
            return
        work_apps = {a.lower() for a in apps}
        lim_apps = [x for x in self.limited_apps if x["target"].lower() not in work_apps]
        lim_sites = [x for x in self.limited_sites if x["target"] not in sites]
        if (sites or lim_sites) and not any(a in BROWSERS for a in apps):
            if not messagebox.askyesno("FocusGuard", "You listed websites but didn't allow a "
                                       "browser, so no websites will work. Continue anyway?"):
                return
        picked = {a.lower() for a in apps} | {x["target"].lower() for x in lim_apps}
        new = {
            "allowed_apps": sorted(set(apps), key=str.lower), "allowed_sites": sites,
            "limited_apps": lim_apps, "limited_sites": lim_sites,
            "distraction_penalty_minutes": int(self.penalty_var.get()),
            "provider": self.cfg["provider"],
            "ai_model_size": next((k for k, v in self.size_names.items()
                                   if v == self.size_var.get()), "auto"),
            "app_ids": {n.lower(): info for n, info in self.app_info.items() if n.lower() in picked},
        }
        if self.key_var.get().strip():
            new["_api_key"] = self.key_var.get().strip()
        if self.gemini_key_var.get().strip():
            new["_gemini_key"] = self.gemini_key_var.get().strip()
        fd, tmp = tempfile.mkstemp(prefix="focusguard-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(new, f)
        ok, msg = run_admin(["set-config", tmp], "FocusGuard: an administrator must approve "
                            "these settings. " + summarize_changes(self.cfg, new))
        if not ok:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            messagebox.showinfo("FocusGuard", "Settings were not saved. An administrator's "
                                f"name and password are needed.\n\n({msg})")
            return
        self.app.show_main()


# ====================================================================== main window
class Main(ttk.Frame):
    def __init__(self, app: "App"):
        super().__init__(app, padding=16)
        self.app = app
        self.cfg = load_config()
        self.busy = False
        self.service_ok = True
        self.version = ""
        # Load the AI model now, so the first chat message doesn't wait for it.
        threading.Thread(target=warm_ai, daemon=True).start()

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.status = ttk.Label(top, text="", font=("Helvetica", 16, "bold"))
        self.status.pack(side="left")
        self.setup_btn = ttk.Button(top, text="Edit setup", command=app.show_onboarding)
        self.setup_btn.pack(side="right")
        self.pause_btn = ttk.Button(top, text="Pause blocking…", command=self.pause)
        self.pause_btn.pack(side="right", padx=6)
        self.resume_btn = ttk.Button(top, text="Resume now", command=self.resume)

        self.perm_bar = ttk.Frame(self)
        self.perm_label = ttk.Label(self.perm_bar, foreground="#c0392b", wraplength=560,
                                    font=("Helvetica", 12, "bold"))
        self.perm_label.pack(side="left")
        ttk.Button(self.perm_bar, text="Open Automation settings",
                   command=lambda: subprocess.run(
                       ["open", "x-apple.systempreferences:com.apple.preference.security"
                                "?Privacy_Automation"])).pack(side="right")
        self.perm_shown = False
        self.mode_detail = ttk.Label(self, text="", foreground="#555", wraplength=760)
        self.mode_detail.pack(anchor="w", pady=(2, 4))
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.clock_btn = ttk.Button(bar, text="Clock in now", command=lambda: self.op("clock_in"))
        self.dinner_btn = ttk.Button(bar, text=f"Dinner break ({DINNER_MINUTES} min)",
                                     command=lambda: self.op("dinner"))
        self.dinner_end_btn = ttk.Button(bar, text="End dinner break",
                                         command=lambda: self.op("dinner_end"))
        self.task_btn = ttk.Button(bar, text="Start a task…", command=self.start_task)
        self.task_end_btn = ttk.Button(bar, text="End task", command=lambda: self.op("task_end"))
        self.mode_buttons = [self.clock_btn, self.dinner_btn, self.dinner_end_btn,
                             self.task_btn, self.task_end_btn]
        ttk.Button(bar, text="Clock-in times…", command=self.edit_times).pack(side="right")
        ttk.Label(self, text="Limited for today (resets at midnight)").pack(anchor="w")
        self.limited_box = tk.Listbox(self, height=4)
        self.limited_box.pack(fill="x", pady=(0, 8))

        lists = ttk.Frame(self)
        lists.pack(fill="x")
        ttk.Label(lists, text="Temporarily unlocked (AI is watching)").grid(row=0, column=0, sticky="w")
        ttk.Label(lists, text="Blocked for distraction / recently blocked").grid(row=0, column=1, sticky="w")
        self.grants_box = tk.Listbox(lists, height=6, width=48)
        self.grants_box.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.blocks_box = tk.Listbox(lists, height=6, width=48)
        self.blocks_box.grid(row=1, column=1, sticky="nsew")
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=1)

        ttk.Label(self, text="Need something that's blocked? Tell the AI why.",
                  font=("Helvetica", 13, "bold")).pack(anchor="w", pady=(14, 4))
        self.log_text = tk.Text(self, height=14, wrap="word", state="disabled")
        self.log_text.tag_configure("you", foreground="#1a5fb4", font=("Helvetica", 12, "bold"))
        self.log_text.tag_configure("ai", foreground="#222")
        self.log_text.tag_configure("sys", foreground="#888", font=("Helvetica", 11, "italic"))
        # The footer and the message box are packed from the bottom before the chat log,
        # so a short window shrinks the log instead of cutting them off.
        foot = ttk.Frame(self)
        foot.pack(side="bottom", fill="x", pady=(8, 0))
        self.version_label = ttk.Label(foot, text="Version: checking…", foreground="#888")
        self.version_label.pack(side="left")
        self.update_btn = ttk.Button(foot, text="Check for updates", command=self.check_updates)
        self.update_btn.pack(side="right")
        row = ttk.Frame(self)
        row.pack(side="bottom", fill="x", pady=(6, 0))
        self.entry = ttk.Entry(row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.send())
        self.send_btn = ttk.Button(row, text="Send", command=self.send)
        self.send_btn.pack(side="left", padx=(6, 0))
        self.log_text.pack(fill="both", expand=True)

        self.say("sys", 'Need a couple of specific sites for homework? "Start a task…" is quicker. '
                        'Or ask here, e.g. "I need youtube.com to watch the biology lecture my '
                        'teacher assigned."')
        self.refresh()

    # -------------------------------------------------- status
    def refresh(self):
        cfg, st = load_config(), load_state()
        now = time.time()
        if paused(st):
            self.status.config(text=f"Paused — {fmt_minutes(st['paused_until'] - now + 59)} left",
                               foreground="#c77c02")
            self.pause_btn.pack_forget()
            if not self.resume_btn.winfo_manager():
                self.resume_btn.pack(side="right", padx=6)
        elif not self.service_ok:
            self.status.config(text="FocusGuard's service isn't running — reinstall it",
                               foreground="#c0392b")
        else:
            self.resume_btn.pack_forget()
            if not self.pause_btn.winfo_manager():
                self.pause_btn.pack(side="right", padx=6, after=self.setup_btn)
        self.show_mode(cfg, st, now)
        denied = sorted(st["no_permission"])
        if denied:
            self.perm_label.config(text=f"FocusGuard can't check websites in {', '.join(denied)}: "
                                        "macOS hasn't allowed it. Turn on python → "
                                        + ", ".join(denied) + " under Automation.")
            if not self.perm_shown:
                self.perm_bar.pack(fill="x", pady=(4, 4), after=self.status.master)
                self.perm_shown = True
        elif self.perm_shown:
            self.perm_bar.pack_forget()
            self.perm_shown = False
        self.limited_box.delete(0, "end")
        for it in limited_items(cfg):
            left = seconds_left(it, st)
            if left > 0:
                line = f"{it['target']} — {fmt_minutes(left)} of {it['minutes']}m left"
            else:
                line = f"⛔ {it['target']} — used up today"
            self.limited_box.insert("end", line)
        if not limited_items(cfg):
            self.limited_box.insert("end", "Nothing yet. Add some in Edit setup.")
        self.grants_box.delete(0, "end")
        for g in st["grants"]:
            self.grants_box.insert("end", f"{g['target']} — {fmt_minutes(g['expires_at'] - now)} left — {g['purpose']}")
        self.blocks_box.delete(0, "end")
        for p in st["penalties"]:
            until = time.strftime("%H:%M", time.localtime(p["until"]))
            self.blocks_box.insert("end", f"⛔ {p['target']} until {until} — {p['reason']}")
        for b in reversed(st["recent_blocks"][-8:]):
            ago = int((now - b["at"]) // 60)
            self.blocks_box.insert("end", f"{b['target']} ({ago}m ago) — {b['reason']}")
        self.version_label.config(text=f"Version {self.version} · updates install automatically"
                                  if self.version else "Version: unknown (service not reachable)")
        self.app.title(f"FocusGuard — version {self.version}" if self.version else "FocusGuard")
        self.after(2000, self.refresh)
        threading.Thread(target=self.check_service, daemon=True).start()

    def show_mode(self, cfg, st, now):
        m = mode(cfg, st, now)
        at = lambda t: time.strftime("%H:%M", time.localtime(t))   # noqa: E731
        show = []
        if m["name"] == "paused":
            detail = "An administrator paused all blocking."
        elif m["name"] == "school":
            if self.service_ok:
                self.status.config(text=f"School time — nothing blocked until {at(m['until'])}",
                                   foreground="#2a7")
            detail = ("Nothing is blocked and the AI is off. Home rules start by themselves at "
                      "the clock-in time, or press Clock in now when you get home.")
            show = [self.clock_btn]
        elif m["name"] == "dinner":
            if self.service_ok:
                self.status.config(text=f"Dinner break — until {at(m['until'])}",
                                   foreground="#2a7")
            detail = "Nothing is blocked. Home rules come back by themselves after the break."
            show = [self.dinner_end_btn]
        else:
            task = m["task"]
            if self.service_ok:
                self.status.config(text=(f"Task until {at(task['until'])}: {task['purpose']}"
                                         if task else "Home — blocking is on"),
                                   foreground="#2a7")
            items = (task["links"] + [a["name"] for a in task.get("apps", [])]) if task else []
            detail = ("Work apps and sites always work. Everything else is blocked."
                      + (f" Never blocked during this task: {', '.join(items)}." if task else ""))
            show = [self.task_end_btn if task else self.task_btn]
            if st.get("dinner_day") != today():
                show.append(self.dinner_btn)
        self.mode_detail.config(text=detail)
        for b in self.mode_buttons:
            if b in show and not b.winfo_manager():
                b.pack(side="left", padx=(0, 6))
            elif b not in show and b.winfo_manager():
                b.pack_forget()

    def op(self, name: str, **payload):
        try:
            request(name, **payload)
            return True
        except ServiceError as e:
            messagebox.showinfo("FocusGuard", str(e), parent=self)
            return False

    def start_task(self):
        win = tk.Toplevel(self)
        win.title("Start a task")
        win.transient(self)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="What are you working on?").pack(anchor="w")
        purpose = ttk.Entry(frm, width=50)
        purpose.pack(anchor="w", fill="x", pady=(0, 8))
        ttk.Label(frm, text=f"Links or apps you need (up to {TASK_MAX_LINKS}). They won't be "
                            "blocked at all during the task.\nType a site (docs.google.com) or "
                            "a link to one page, or pick an app from the list.",
                  wraplength=420).pack(anchor="w")
        apps = installed_apps()
        links = [ttk.Combobox(frm, width=48, values=list(apps)) for _ in range(TASK_MAX_LINKS)]
        for e in links:
            e.pack(anchor="w", fill="x", pady=2)
        row = ttk.Frame(frm)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Label(row, text=f"For how many minutes? (up to {TASK_MAX_MINUTES})").pack(side="left")
        minutes = tk.IntVar(value=60)
        ttk.Spinbox(row, from_=5, to=TASK_MAX_MINUTES, increment=5, width=5,
                    textvariable=minutes).pack(side="left", padx=6)

        verdict = ttk.Label(frm, text="", wraplength=420, foreground="#555")
        verdict.pack(anchor="w", pady=(8, 0))

        def go():
            try:
                mins = int(minutes.get())
            except (tk.TclError, ValueError):
                mins = 0
            start.state(["disabled"])
            verdict.config(text="The AI is checking that the links fit the task…",
                           foreground="#555")
            picked = [e.get().strip() for e in links if e.get().strip()]
            by_key = {app_key(n): info for n, info in apps.items()}
            req = {"purpose": purpose.get(), "minutes": mins,
                   "links": [x for x in picked if app_key(x) not in by_key],
                   "apps": [by_key[app_key(x)]["path"] for x in picked if app_key(x) in by_key]}

            def work():
                try:
                    request("task_start", timeout=75, **req)
                    self.after(0, win.destroy)
                except ServiceError as e:
                    self.after(0, lambda: (verdict.config(text=str(e), foreground="#c0392b"),
                                           start.state(["!disabled"])))
            threading.Thread(target=work, daemon=True).start()
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        start = ttk.Button(btns, text="Start", command=go)
        start.pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        purpose.focus_set()

    def edit_times(self):
        cfg = load_config()
        win = tk.Toplevel(self)
        win.title("Clock-in times")
        win.transient(self)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="From midnight until the clock-in time it's school time: nothing "
                            "is blocked. At the clock-in time home rules start.",
                  wraplength=380).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        entries = {}
        for i, (d, name) in enumerate(zip(DAYS, DAY_NAMES), start=1):
            ttk.Label(frm, text=name).grid(row=i, column=0, sticky="w")
            e = ttk.Entry(frm, width=8)
            e.insert(0, cfg["clock_in"].get(d, "16:00"))
            e.grid(row=i, column=1, sticky="w", pady=1)
            entries[d] = e
        ttk.Label(frm, text=f'Password of the Mac account "{cfg["user"]}":').grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(10, 0))
        pw = ttk.Entry(frm, show="•", width=30)
        pw.grid(row=10, column=0, columnspan=2, sticky="w")

        def save():
            ok = self.op("set_schedule", password=pw.get(),
                         times={d: e.get() for d, e in entries.items()})
            pw.delete(0, "end")
            if ok:
                win.destroy()
        btns = ttk.Frame(frm)
        btns.grid(row=11, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="left", padx=6)
        ttk.Button(btns, text="Save", command=save).pack(side="left")

    def check_service(self):
        try:
            self.version = request("ping", timeout=2).get("version", "")
            self.service_ok = True
        except ServiceError:
            self.service_ok = False

    def check_updates(self):
        self.update_btn.state(["disabled"])
        self.say("sys", "Checking for updates…")

        def work():
            try:
                msg = request("update", timeout=600)["message"]
                if msg.startswith("updated"):
                    msg = (f"FocusGuard {msg}. It restarts in the background; close and reopen "
                           "this window to see the new version.")
                elif msg == "up to date":
                    msg = "You already have the latest version."
            except ServiceError as e:
                msg = str(e)
            self.after(0, lambda: (self.say("sys", msg), self.update_btn.state(["!disabled"])))
        threading.Thread(target=work, daemon=True).start()

    def pause(self):
        minutes = simpledialog.askinteger(
            "Pause blocking", "Turn off all blocking for how many minutes? (5–240)\n\n"
            "An administrator password is needed.", parent=self,
            minvalue=5, maxvalue=240, initialvalue=30)
        if not minutes:
            return
        ok, msg = run_admin(["pause", minutes],
                            f"FocusGuard: an administrator must approve pausing ALL blocking "
                            f"for {minutes} minutes.")
        if not ok:
            self.say("sys", f"Not paused: an administrator's password is needed. ({msg})")
            return
        self.say("sys", f"Blocking is paused for {minutes} minutes. It turns back on by itself.")

    def resume(self):
        try:
            request("resume")
            self.say("sys", "Blocking is back on.")
        except ServiceError as e:
            self.say("sys", str(e))

    # -------------------------------------------------- chat
    def say(self, who: str, text: str):
        prefix = {"you": "You: ", "ai": "FocusGuard AI: ", "sys": ""}[who]
        self.log_text.config(state="normal")
        self.log_text.insert("end", prefix + text + "\n\n", who)
        self.log_text.config(state="disabled")
        self.log_text.see("end")

    def send(self):
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        self.say("you", text)
        self.busy = True
        self.send_btn.state(["disabled"])
        self.say("sys", "Thinking…")
        sent = self.chat_count = getattr(self, "chat_count", 0) + 1
        self.after(20000, lambda: self.busy and self.chat_count == sent and self.say(
            "sys", "Still thinking. The free AI can be slow when it's just starting up; "
                   "it gives up after a minute."))

        def work():
            try:
                result = request("chat", text=text, timeout=75)
                self.after(0, lambda: self.on_reply(result))
            except ServiceError as e:
                self.after(0, lambda: self.on_error(str(e)))
        threading.Thread(target=work, daemon=True).start()

    def on_error(self, msg: str):
        self.busy = False
        self.send_btn.state(["!disabled"])
        self.say("sys", msg)

    def on_reply(self, r: dict):
        self.busy = False
        self.send_btn.state(["!disabled"])
        self.say("ai", r["reply"])
        if r.get("note"):
            self.say("sys", r["note"])


def warm_ai():
    try:
        request("warm")
    except ServiceError:
        pass


# ====================================================================== app shell
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FocusGuard")
        self.geometry("820x720")
        self.minsize(700, 600)
        self.frame = None
        cfg = load_config()
        if cfg["onboarded"]:
            self.show_main()
        else:
            self.swap(Onboarding)

    def swap(self, frame_cls):
        if self.frame is not None:
            self.frame.destroy()
        self.frame = frame_cls(self)
        self.frame.pack(fill="both", expand=True)

    def show_onboarding(self):
        # Anyone may look; saving needs an administrator password (see Onboarding.next).
        self.swap(Onboarding)

    def show_main(self):
        self.swap(Main)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
