"""FocusGuard window: setup (work / limited lists), daily allowances, and the AI unblock chat."""
from __future__ import annotations

import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import ai
from .common import (find_item, get_api_key, limited_items, load_config, load_state, log,
                     new_id, normalize_domain, paused, save_config, seconds_left, set_api_key,
                     update_state)
from .macos import BROWSERS, UNMONITORABLE_BROWSERS, admin_auth, list_installed_apps

COMMON_SITES = [
    "google.com", "docs.google.com", "drive.google.com", "mail.google.com",
    "calendar.google.com", "github.com", "stackoverflow.com", "wikipedia.org",
    "claude.ai", "notion.so", "canva.com", "office.com", "outlook.com",
    "slack.com", "zoom.us", "khanacademy.org", "youtube.com", "reddit.com",
]


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
        self.provider_var = tk.StringVar(value=self.cfg.get("provider", "ollama"))
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
        ttk.Radiobutton(self.body, variable=self.provider_var, value="ollama",
                        text="Free: local AI on this Mac via Ollama (no key, nothing leaves your Mac)"
                        ).pack(anchor="w", pady=2)
        ttk.Label(self.body, text="Needs the Ollama app running (ollama.com) and the model "
                  f"downloaded: ollama pull {self.cfg['ollama_model']}",
                  foreground="#555").pack(anchor="w", padx=24)
        ttk.Radiobutton(self.body, variable=self.provider_var, value="anthropic",
                        text="Claude API (paid, smarter; needs a key from console.anthropic.com)"
                        ).pack(anchor="w", pady=(10, 2))
        ttk.Label(self.body, text="Claude API key (stored in your Keychain):").pack(anchor="w", padx=24)
        ttk.Entry(self.body, textvariable=self.key_var, show="•", width=60).pack(anchor="w", padx=24)
        if get_api_key():
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
        apps = [a for a in list_installed_apps() if a not in UNMONITORABLE_BROWSERS]
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
                     "doing something distracting on one (e.g. gaming videos on YouTube), that site "
                     "is blocked for an hour. Subdomains are included automatically.")
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
        apps = [a for a in list_installed_apps()
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
            if key and not set_api_key(key):
                messagebox.showerror("FocusGuard", "Couldn't save the key to Keychain.")
                return False
            if self.cfg["provider"] == "anthropic" and not key and not get_api_key():
                messagebox.showwarning("FocusGuard", "Paste your Claude API key, or choose the free local AI.")
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
        self.cfg.update(onboarded=True, allowed_apps=sorted(set(apps), key=str.lower),
                        allowed_sites=sites, limited_apps=lim_apps, limited_sites=lim_sites,
                        distraction_penalty_minutes=int(self.penalty_var.get()))
        save_config(self.cfg)
        log("setup saved")
        self.app.show_main()


# ====================================================================== main window
class Main(ttk.Frame):
    def __init__(self, app: "App"):
        super().__init__(app, padding=16)
        self.app = app
        self.cfg = load_config()
        self.chat = []           # Anthropic-format messages for the gatekeeper
        self.busy = False

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
        ttk.Label(self, text="Work apps and sites always work. Everything else is blocked.",
                  foreground="#555").pack(anchor="w", pady=(2, 8))
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
        self.log_text.pack(fill="both", expand=True)
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(6, 0))
        self.entry = ttk.Entry(row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.send())
        self.send_btn = ttk.Button(row, text="Send", command=self.send)
        self.send_btn.pack(side="left", padx=(6, 0))

        self.say("sys", 'Example: "I need youtube.com to watch the biology lecture my teacher assigned."')
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
        else:
            self.status.config(text="Blocking is on", foreground="#2a7")
            self.resume_btn.pack_forget()
            if not self.pause_btn.winfo_manager():
                self.pause_btn.pack(side="right", padx=6, after=self.setup_btn)
        denied = sorted(st["no_permission"])
        if denied:
            self.perm_label.config(text=f"Websites are NOT being blocked in {', '.join(denied)}: "
                                        "macOS hasn't allowed FocusGuard to control it. Turn on "
                                        "python → " + ", ".join(denied) + " under Automation.")
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
        self.after(2000, self.refresh)

    def pause(self):
        minutes = simpledialog.askinteger(
            "Pause blocking", "Turn off all blocking for how many minutes? (5–240)\n\n"
            "An administrator password is needed.", parent=self,
            minvalue=5, maxvalue=240, initialvalue=30)
        if not minutes:
            return
        if not admin_auth(f"FocusGuard wants to pause all blocking for {minutes} minutes."):
            self.say("sys", "Pause cancelled: no administrator password.")
            return
        update_state(lambda s: s.update(paused_until=time.time() + minutes * 60))
        log(f"blocking paused for {minutes}m (admin approved)")
        self.say("sys", f"Blocking is paused for {minutes} minutes. It turns back on by itself.")

    def resume(self):
        update_state(lambda s: s.update(paused_until=0))
        log("pause ended early by user")
        self.say("sys", "Blocking is back on.")

    # -------------------------------------------------- chat
    def say(self, who: str, text: str):
        prefix = {"you": "You: ", "ai": "FocusGuard AI: ", "sys": ""}[who]
        self.log_text.config(state="normal")
        self.log_text.insert("end", prefix + text + "\n\n", who)
        self.log_text.config(state="disabled")
        self.log_text.see("end")

    def context(self) -> str:
        cfg, st = load_config(), load_state()
        now = time.time()
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
        lines.append("Distraction penalties (cannot be lifted): " + (", ".join(
            f"{p['target']} until {time.strftime('%H:%M', time.localtime(p['until']))}"
            for p in st["penalties"]) or "none"))
        lines.append("Recently blocked: " + (", ".join(
            f"{b['kind']} {b['target']} ({b['reason']})" for b in st["recent_blocks"][-8:]) or "nothing"))
        return "\n".join(lines)

    def send(self):
        text = self.entry.get().strip()
        if not text or self.busy:
            return
        self.entry.delete(0, "end")
        self.say("you", text)
        self.chat.append({"role": "user", "content": text})
        self.chat = self.chat[-20:]
        if self.chat[0]["role"] != "user":
            self.chat = self.chat[1:]
        self.busy = True
        self.send_btn.state(["disabled"])
        self.say("sys", "Thinking…")
        chat, ctx = list(self.chat), self.context()

        def work():
            try:
                result = ai.gatekeeper(chat, ctx)
                self.after(0, lambda: self.on_reply(result))
            except ai.AIError as e:
                self.after(0, lambda: self.on_error(str(e)))
            except Exception as e:  # noqa: BLE001 - surface anything unexpected
                self.after(0, lambda: self.on_error(f"Unexpected error: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def on_error(self, msg: str):
        self.busy = False
        self.send_btn.state(["!disabled"])
        self.chat.pop()  # let the user retry the same message
        self.say("sys", msg)

    def on_reply(self, r: dict):
        self.busy = False
        self.send_btn.state(["!disabled"])
        self.chat.append({"role": "assistant", "content": r["reply"]})
        self.say("ai", r["reply"])
        if r["decision"] != "grant" or r["target_kind"] == "none" or not r["target"].strip():
            return
        kind = r["target_kind"]
        target = normalize_domain(r["target"]) if kind == "site" else r["target"].strip()
        minutes = r["duration_minutes"]

        def grant(s):
            s["grants"] = [g for g in s["grants"] if not (g["kind"] == kind and g["target"] == target)]
            s["grants"].append({"id": new_id(), "kind": kind, "target": target,
                                "purpose": r["purpose"], "granted_at": time.time(),
                                "expires_at": time.time() + minutes * 60})
        st = update_state(grant)
        log(f"granted {kind} {target} for {minutes}m: {r['purpose']}")
        if kind == "site" and find_item(st["penalties"], "site", target):
            self.say("sys", f"Note: {target} is still under a distraction block until it expires.")
        else:
            self.say("sys", f"Unlocked {target} for {minutes} min. Activity on it is being monitored.")


# ====================================================================== app shell
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FocusGuard")
        self.geometry("820x720")
        self.minsize(700, 600)
        self.frame = None
        cfg = load_config()
        if cfg["onboarded"] and (cfg.get("provider") == "ollama" or get_api_key()):
            self.show_main()
        else:
            self.swap(Onboarding)

    def swap(self, frame_cls):
        if self.frame is not None:
            self.frame.destroy()
        self.frame = frame_cls(self)
        self.frame.pack(fill="both", expand=True)

    def show_onboarding(self):
        if load_config()["onboarded"]:
            if not admin_auth("FocusGuard wants to change which apps and websites are blocked."):
                messagebox.showinfo("FocusGuard", "Settings weren't opened: an administrator "
                                    "password is needed to change them.")
                return
            log("setup opened (admin approved)")
        self.swap(Onboarding)

    def show_main(self):
        self.swap(Main)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
