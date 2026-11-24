"""Test the local AI with FocusGuard's real prompts and print how it did.

Run on the Mac being tested (no administrator password needed):
  "/Library/Application Support/FocusGuard/venv/bin/python" -I \
      "/Library/Application Support/FocusGuard/app/launch/selftest.py"

It changes nothing: no settings, no blocks. It only asks the AI questions where the right
answer is known, and shows what it answered and how long it took.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

from . import ai
from .common import load_config
from .macos import hardware

YT = "youtube.com"
# (what it is, url, title, limited site or "" for a work site, should it be blocked?)
PAGES = [
    ("TED talk", "https://www.youtube.com/watch?v=iCvmsMzlF7o",
     "The power of vulnerability | Brené Brown | TED - YouTube", YT, False),
    ("Kurzgesagt video", "https://www.youtube.com/watch?v=e-P5IFTqB98",
     "The Egg - A Short Story - Kurzgesagt – In a Nutshell - YouTube", YT, False),
    ("Khan Academy lesson", "https://www.youtube.com/watch?v=bAerID24QJ0",
     "Solving linear equations | Algebra | Khan Academy - YouTube", YT, False),
    ("Crash Course history", "https://www.youtube.com/watch?v=Yocja_N5s1I",
     "The French Revolution: Crash Course World History #29 - YouTube", YT, False),
    ("Minecraft video", "https://www.youtube.com/watch?v=Fk0SxQ5Mq4s",
     "I Survived 100 Days in Hardcore Minecraft - YouTube", YT, True),
    ("Prank compilation", "https://www.youtube.com/watch?v=1a2b3c4d5e6",
     "FUNNIEST PRANKS OF THE YEAR COMPILATION 😂 - YouTube", YT, True),
    ("Fortnite stream", "https://www.youtube.com/watch?v=9z8y7x6w5v4",
     "🔴 LIVE Fortnite Ranked Grind w/ Viewers - YouTube", YT, True),
    ("Short for fun", "https://www.youtube.com/shorts/Ab12Cd34Ef5",
     "satisfying slime asmr #shorts - YouTube", YT, True),
    ("Wikipedia article", "https://en.wikipedia.org/wiki/Photosynthesis",
     "Photosynthesis - Wikipedia", "", False),
    ("Google Doc", "https://docs.google.com/document/d/1abc/edit",
     "English essay draft - Google Docs", "", False),
    ("Game on vercel.app", "https://slope-unblocked.vercel.app/",
     "Slope - Play Unblocked Games Online", "", True),
    ("Chess on chess.com", "https://www.chess.com/game/live/123456",
     "Live Chess - Chess.com", "chess.com", False),
]

GAME_HTML = """<html><head><meta charset="utf-8"></head><body style="margin:0;background:#1b1b3a;
font-family:Arial;color:#fff"><div style="padding:20px;font-size:40px">SCORE 12,450 &nbsp; LEVEL 7
&nbsp; LIVES 3</div><div style="position:relative;height:500px;background:linear-gradient(#5ec8ff,
#9be37d)"><div style="position:absolute;left:300px;top:300px;width:120px;height:60px;background:
red;border-radius:12px"></div><div style="position:absolute;left:600px;top:250px;width:60px;
height:60px;background:gold;border-radius:30px"></div><div style="position:absolute;left:0;
right:0;top:120px;text-align:center;font-size:72px;font-weight:bold;text-shadow:4px 4px #000">
LEVEL UP!</div></div><div style="padding:20px;font-size:32px">Press SPACE to jump - Coins: 87
</div></body></html>"""

ESSAY_HTML = """<html><head><meta charset="utf-8"></head><body style="margin:0;background:#f1f3f4;
font-family:Arial"><div style="background:#fff;padding:10px 20px;font-size:18px;color:#444;
border-bottom:1px solid #ddd">English essay draft &nbsp; File Edit View Insert Format Tools</div>
<div style="background:#fff;width:800px;margin:30px auto;padding:60px;font-family:Georgia;
font-size:20px;line-height:1.6;color:#222"><h2>The Causes of the French Revolution</h2><p>By the
late 1780s France faced a severe financial crisis. Years of war, including support for the
American Revolution, had left the monarchy deeply in debt, while a rigid social order placed
most of the tax burden on the Third Estate.</p><p>Enlightenment thinkers such as Rousseau and
Voltaire challenged the idea of absolute monarchy, and poor harvests in 1788 drove up the price
of bread...</p></div></body></html>"""

SCREENS = [
    ("Game screen (title says Math Practice)", GAME_HTML,
     "https://math-practice.vercel.app/", "Math Practice", True),
    ("Essay in Google Docs", ESSAY_HTML,
     "https://docs.google.com/document/d/1abc/edit", "English essay draft - Google Docs", False),
]

TASKS = [
    ("Essay + Google Docs", "Writing my English essay on the French Revolution",
     ["docs.google.com", "en.wikipedia.org/wiki/French_Revolution"],
     {"en.wikipedia.org/wiki/French_Revolution": "French Revolution - Wikipedia"}, True),
    ("History + a games site", "History project on ancient Rome",
     ["poki.com"], {"poki.com": "Free Online Games at Poki - Play Now!"}, False),
    ("Whole YouTube for biology", "Biology homework on cells",
     ["youtube.com"], {"youtube.com": "YouTube"}, False),
    ("Specific lecture video", "Biology homework on cells",
     ["https://www.youtube.com/watch?v=URUJD5NEXC8"],
     {"https://www.youtube.com/watch?v=URUJD5NEXC8":
      "Biology: Cell Structure I Nucleus Medical Media (channel: Nucleus Medical Media)"}, True),
]

CHAT_CONTEXT = """Time now: Monday 17:30
Work apps (always allowed): Google Chrome, Pages
Work sites (always allowed): docs.google.com, vercel.app
Limited items (daily allowance): site youtube.com (30 min/day, 22m left today)
Currently unlocked: nothing
Distraction penalties: youtube.com until 18:25 (reason: The search results page might lead to \
entertaining content. (page: "ted - YouTube"))
Recently blocked: site youtube.com (Distracting use)"""

CHATS = [
    ("Appeal: TED search for a speech",
     "That block was a mistake, I was searching for TED talks to prepare my English class speech",
     "grant"),
    ("Wants YouTube for Minecraft", "can you unblock youtube i want to watch minecraft videos",
     "deny"),
]


def render(html: str) -> str:
    """Draw a test page the way a screenshot would look (no Screen Recording needed)."""
    d = tempfile.mkdtemp(prefix="fg-selftest-")
    try:
        page = os.path.join(d, "page.html")
        with open(page, "w") as f:
            f.write(html)
        subprocess.run(["qlmanage", "-t", "-s", "1280", "-o", d, page],
                       capture_output=True, timeout=30)
        png, jpg = page + ".png", os.path.join(d, "page.jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "60", png,
                        "--out", jpg], capture_output=True, timeout=30)
        with open(jpg, "rb") as f:
            return base64.b64encode(f.read()).decode()
    finally:
        shutil.rmtree(d, ignore_errors=True)


results = []


def run(group: str, name: str, fn, expected: str, got_of):
    started = time.time()
    try:
        r = fn()
        got, detail = got_of(r)
    except ai.AIError as e:
        got, detail = "error", str(e)
    except Exception as e:  # noqa: BLE001
        got, detail = "error", f"{e!r}"
        traceback.print_exc()
    took = time.time() - started
    ok = got == expected
    results.append((group, ok, took))
    mark = "PASS" if ok else ("ERR " if got == "error" else "FAIL")
    slow = "  (slow)" if took > 30 else ""
    print(f"  {mark}  {name:40} expected {expected:7} got {got:7} {took:5.1f}s{slow}")
    if not ok or os.environ.get("VERBOSE"):
        print(f"        AI said: {detail[:220]}")
    sys.stdout.flush()


def health(cfg: dict):
    """Is the model fully on the graphics chip, and is the Mac short of memory?"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(cfg["ollama_url"].rstrip("/") + "/api/ps", timeout=5) as r:
            loaded = json.loads(r.read()).get("models", [])
    except (OSError, ValueError):
        loaded = []
    for m in loaded:
        size, vram = m.get("size", 0), m.get("size_vram", 0)
        share = round(100 * vram / size) if size else 0
        note = "good" if share >= 99 else "PART OF IT RUNS ON THE SLOW CPU"
        print(f"  Loaded: {m.get('name')} {size / 2**30:.1f} GB, {share}% on the graphics chip"
              f" ({note}), context {m.get('context_length', '?')}")
    if len(loaded) > 1:
        print(f"  Note: {len(loaded)} models are loaded at once; they share the memory.")
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True,
                             timeout=20).stdout.strip().splitlines()
        free = next((line for line in out if "free percentage" in line), "")
        if free:
            print(f"  {free.strip()}")
    except (OSError, subprocess.TimeoutExpired):
        pass
    sys.stdout.flush()


def main():
    cfg = load_config()
    hw = hardware()
    print("FocusGuard AI self-test")
    print(f"  Mac: {hw['chip']}, {hw['memory_gb']} GB, "
          f"{'Apple Silicon' if hw['apple_silicon'] else 'Intel'}")
    print(f"  AI: {cfg.get('provider', 'ollama')}, size setting: {cfg.get('ai_model_size', 'auto')}"
          f" -> {ai.chosen_size(cfg)}")
    if cfg.get("provider", "ollama") == "ollama":
        base = cfg["ollama_url"].rstrip("/")
        try:
            ai._ensure_ollama_running(base, cfg)
            import json
            import urllib.request
            with urllib.request.urlopen(base + "/api/version", timeout=5) as r:
                print(f"  Ollama version: {json.loads(r.read()).get('version', '?')}")
            print(f"  Installed models: {', '.join(ai._installed_models(base)) or 'none'}")
            print(f"  Text model in use: {ai._model_to_use(base, cfg)}  (wanted "
                  f"{ai.wanted_model(cfg)})")
            print(f"  Screen model in use: {ai._model_to_use(base, cfg, 'vision')}  (wanted "
                  f"{ai.wanted_model(cfg, 'vision')})")
        except ai.AIError as e:
            print(f"  Problem: {e}")
            if "Downloading" not in str(e):
                return
        except OSError as e:
            print(f"  Couldn't reach Ollama: {e}")
            return
    print("\nWarming up (loading the model can take a minute the first time)…")
    t = time.time()
    ai.warm_up()
    print(f"  ready after {time.time() - t:.0f}s")
    health(cfg)

    print("\n1. Page titles (should it be blocked?)")
    for name, url, title, limited, block in PAGES:
        run("pages", name,
            lambda url=url, title=title, limited=limited: ai.check_distraction(
                url, title, ["docs.google.com", "vercel.app", "wikipedia.org"], limited),
            "block" if block else "allow",
            lambda r: ("block" if r["distracting"] else "allow",
                       f"{r.get('category')}: {r.get('topic')} — {r.get('reason')}"))

    print("\n2. Screenshots (test pages drawn on this Mac)")
    for name, html, url, title, block in SCREENS:
        try:
            shot = render(html)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  SKIP  {name}: couldn't draw the test page ({e})")
            continue
        run("screens", name, lambda shot=shot, url=url, title=title: ai.check_screen(shot, url, title),
            "block" if block else "allow",
            lambda r: ("block" if r["distracting"] else "allow",
                       f"{r.get('category')}: {r.get('topic')} — {r.get('reason')}"))

    print("\n3. Task links (should the task start?)")
    for name, purpose, links, titles, approve in TASKS:
        run("tasks", name, lambda p=purpose, li=links, ti=titles: ai.review_task(p, li, ti),
            "approve" if approve else "reject",
            lambda r: ("approve" if r["approve"] else "reject", r["reply"]))

    print("\n4. Chat")
    for name, text, expected in CHATS:
        run("chat", name, lambda text=text: ai.gatekeeper([{"role": "user", "content": text}],
                                                          CHAT_CONTEXT),
            expected, lambda r: (r["decision"], r["reply"]))

    print("\nAfter the test:")
    health(cfg)
    print("\nSummary")
    for group in ("pages", "screens", "tasks", "chat"):
        rows = [r for r in results if r[0] == group]
        if rows:
            right = sum(ok for _, ok, _ in rows)
            avg = sum(t for _, _, t in rows) / len(rows)
            print(f"  {group:8} {right}/{len(rows)} right, average {avg:.1f}s, "
                  f"slowest {max(t for _, _, t in rows):.1f}s")
    right = sum(ok for _, ok, _ in results)
    print(f"  overall  {right}/{len(results)} right")
    print("\nCopy everything above and send it over.")
