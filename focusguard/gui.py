"""FocusGuard window: onboarding, focus sessions, and the AI unblock chat."""
from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import ai
from .common import (find_item, get_api_key, load_config, load_state, log, new_id,
                     normalize_domain, save_config, session_active, set_api_key,
                     update_state)
from .macos import BROWSERS, UNMONITORABLE_BROWSERS, list_installed_apps

COMMON_SITES = [
    "google.com", "docs.google.com", "drive.google.com", "mail.google.com",
    "calendar.google.com", "github.com", "stackoverflow.com", "wikipedia.org",
    "claude.ai", "notion.so", "canva.com", "office.com", "outlook.com",
    "slack.com", "zoom.us", "khanacademy.org", "youtube.com", "reddit.com",
]
END_EARLY_PHRASE = "I am choosing to stop focusing"


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
        self.minutes_var = tk.IntVar(value=self.cfg["default_session_minutes"])
        self.penalty_var = tk.IntVar(value=self.cfg["distraction_penalty_minutes"])
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self)
        nav.pack(fill="x", pady=(12, 0))
        self.back_btn = ttk.Button(nav, text="Back", command=self.back)
        self.back_btn.pack(side="left")
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
        [self.render_key, self.render_apps, self.render_sites, self.render_session][self.step]()

    # step 0
    def render_key(self):
        self.heading("Welcome to FocusGuard",
                     "During a focus session, FocusGuard blocks every app and website except the ones "
                     "you pick next. If you need something that's blocked, ask the built-in AI. "
                     "It only applies to this Mac user account.\n\nChoose which AI to use:")
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
                     "Everything you leave unticked is blocked during focus sessions. Tick your "
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
        self.heading("Which websites do you use (or might use)?",
                     "All other sites are blocked. Allowed sites are still watched: if you start "
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
    def render_session(self):
        self.heading("Time limits",
                     "Blocking runs for a set time each session. You can start a session any time "
                     "from the main window.")
        row = ttk.Frame(self.body)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="Default focus session length (minutes):").pack(side="left")
        ttk.Spinbox(row, from_=10, to=720, increment=10, textvariable=self.minutes_var,
                    width=6).pack(side="left", padx=6)
        row2 = ttk.Frame(self.body)
        row2.pack(anchor="w", pady=4)
        ttk.Label(row2, text="Block distracting sites for (minutes):").pack(side="left")
        ttk.Spinbox(row2, from_=10, to=240, increment=10, textvariable=self.penalty_var,
                    width=6).pack(side="left", padx=6)

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
        if sites and not any(a in BROWSERS for a in apps):
            if not messagebox.askyesno("FocusGuard", "You listed websites but didn't allow a "
                                       "browser, so no websites will work. Continue anyway?"):
                return
        self.cfg.update(onboarded=True, allowed_apps=sorted(set(apps), key=str.lower),
                        allowed_sites=sites,
                        default_session_minutes=int(self.minutes_var.get()),
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

        ctl = ttk.Frame(self)
        ctl.pack(fill="x", pady=8)
        self.minutes = tk.IntVar(value=self.cfg["default_session_minutes"])
        ttk.Label(ctl, text="Session length (min):").pack(side="left")
        self.spin = ttk.Spinbox(ctl, from_=10, to=720, increment=10, textvariable=self.minutes, width=6)
        self.spin.pack(side="left", padx=6)
        self.start_btn = ttk.Button(ctl, text="Start focus session", command=self.start)
        self.start_btn.pack(side="left")
        self.end_btn = ttk.Button(ctl, text="End early…", command=self.end_early)
        self.end_btn.pack(side="left", padx=6)

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
        st = load_state()
        now = time.time()
        active = session_active(st)
        if active:
            self.status.config(text=f"Focusing — {fmt_minutes(st['session']['ends_at'] - now)} left",
                               foreground="#2a7")
            self.start_btn.state(["disabled"])
            self.end_btn.state(["!disabled"])
            self.setup_btn.state(["disabled"])
        else:
            self.status.config(text="Not in a focus session", foreground="#888")
            self.start_btn.state(["!disabled"])
            self.end_btn.state(["disabled"])
            self.setup_btn.state(["!disabled"])
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

    def start(self):
        minutes = max(1, int(self.minutes.get()))

        def go(s):
            s["session"] = {"started_at": time.time(), "ends_at": time.time() + minutes * 60}
            s["grants"] = []
        update_state(go)
        log(f"session started for {minutes}m")
        self.say("sys", f"Focus session started for {minutes} minutes.")

    def end_early(self):
        typed = simpledialog.askstring(
            "End focus session early",
            f'To end early, type exactly:\n\n{END_EARLY_PHRASE}', parent=self)
        if typed is None:
            return
        if typed.strip() != END_EARLY_PHRASE:
            messagebox.showinfo("FocusGuard", "That didn't match. Your session continues.")
            return
        update_state(lambda s: s.update(session=None, grants=[]))
        log("session ended early by user")
        self.say("sys", "Session ended early.")

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
        if session_active(st):
            lines.append(f"Focus session active, {fmt_minutes(st['session']['ends_at'] - now)} left.")
        else:
            lines.append("No focus session is running, so nothing is blocked right now.")
        lines.append("Allowed apps: " + ", ".join(cfg["allowed_apps"]))
        lines.append("Allowed sites: " + (", ".join(cfg["allowed_sites"]) or "none"))
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
        if session_active(load_state()):
            messagebox.showinfo("FocusGuard", "You can't change your setup during a focus session.")
            return
        self.swap(Onboarding)

    def show_main(self):
        self.swap(Main)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
