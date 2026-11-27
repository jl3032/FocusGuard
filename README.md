# FocusGuard

A self-control tool for macOS that applies to **one Mac user account only**. Blocking is always on; there are no sessions to start.

- **Work apps and sites:** you pick the apps and websites you use for work. They always work.
- **Limited apps and sites:** you pick a few you're allowed for a set time each day, e.g. chess.com for 10 minutes. Time only counts while it's on screen; you get a warning at 5 and 1 minutes left, then it's blocked until midnight. The AI also judges limited sites by what they're for: playing chess on chess.com is fine, but on a general site like YouTube only learning content is (a gaming video gets that page blocked; a second one within 30 minutes blocks the site for an hour).
- **Everything else is blocked**, e.g. ea.com when only chess.com is on your limited list.
- **Built-in AI gatekeeper:** open FocusGuard and tell the AI why you need something that's blocked. If your reason is specific and real, it unlocks the app or site for a limited time and writes down what you said you'd do there.
- **Monitoring after an unlock:** about every 60 seconds the AI compares your recent pages or window titles with that stated purpose. If you're off task, access is taken away and the item is blocked again.
- **AI watches every page on your work sites.** It reads each page you open (even while you scroll Shorts or Reels) and judges whether it is work. Home pages and search results are skipped. If a page is distracting, e.g. a gaming video on YouTube, a second AI check confirms it, then that page is blocked; a second distracting page on the same site within 30 minutes blocks the whole site for an hour.
- **Admin password for changes:** editing setup, or pausing all blocking for 5–240 minutes, needs a Mac administrator's name and password (macOS's own dialog; FocusGuard never sees the password). For this to mean anything, the blocked user should be a *standard* account and the password should belong to someone else, e.g. a parent.

## How it's protected

FocusGuard has three parts:

| Part | Runs as | Job |
|---|---|---|
| Root service (`/Library/LaunchDaemons/com.focusguard.root.plist`) | root | Makes every decision, holds all settings and state, runs the AI chat. The blocked user can't stop it, edit its files or uninstall it. |
| Helper (`/Library/LaunchAgents/com.focusguard.agent.plist`) | the blocked user | Tells the root service which app and tab are in front, and quits/redirects what it's told to. If it's killed it is restarted, and meanwhile the root service closes blocked apps itself (allowed browsers stay open). Reports from any other process are ignored. |
| Window (`/Applications/FocusGuard.app`) | the blocked user | Shows status and the AI chat. Saving settings or pausing needs an administrator's password. |

Files live in `/Library/Application Support/FocusGuard`, owned by root. It only enforces for the user who ran the installer; other users on the Mac aren't affected.

Workarounds it closes:
- **Killing or unloading it** (Activity Monitor, Terminal, VS Code): the root service restarts it within seconds and keeps closing blocked apps meanwhile. Allowed browsers are never closed just because their tabs can't be read, so websites go unchecked for those few seconds.
- **Editing its settings** (TextEdit, scripts): the files are root-owned.
- **Uninstalling, pausing, changing lists**: need an administrator password; the password window says exactly what is being newly allowed.
- **Renaming a game** to an allowed app's name (`Notes.app`, `Finder.app`): apps are matched by location and bundle ID.
- **Games run through Python**: "Python" is only allowed when it's FocusGuard's own window.
- **Turning off the browser permission**: websites in that browser can't be checked; FocusGuard keeps showing a warning (and logs it) but doesn't close the browser.
- **Talking the AI into it** ("my parent said it's fine"): the AI is told to ignore such claims, and the chat history lives in the root service, so it can't be faked.

For any of this to hold, **the blocked account must be a Standard user** (System Settings → Users & Groups) and the administrator password must belong to someone else.

## How blocking works

| What | How |
|---|---|
| Apps not on your lists (or out of daily time) | Asked to quit as soon as they come to the front (your unsaved work still gets a save prompt) |
| Websites in Safari, Chrome, Arc, Brave, Edge, Vivaldi | The current tab's URL is read through AppleScript; a blocked tab is sent to a "blocked" page |
| Firefox, Opera, Zen, Orion | Their URLs can't be read, so these apps are always blocked |
| macOS's own windows (password prompts, permission dialogs) | Never touched |

## Install (on this Mac or any other)

You need an **administrator's name and password** once during install.

1. **Download it.** On the GitHub page, click the green **Code** button, then **Download ZIP**. Double-click the zip in Downloads to unpack it.
2. **Log in as the user who should be blocked.**
3. **Open Terminal** and run:
   ```
   zsh ~/Downloads/FocusGuard-main/install.sh
   ```
   (Double-clicking `Install FocusGuard.command` also works, but macOS often refuses to open downloaded `.command` files; the Terminal command avoids that.)
4. **When the password window appears**, an administrator types their name and password.
5. **Wait for it to finish.** It sets up the free AI too, picking a model size that fits the Mac (see below), then opens FocusGuard. Pick the apps and sites, then an administrator approves the settings with their password.
6. When macOS asks to let python control Safari/Chrome or System Events, click **Allow**.

**Updates install themselves.** About 2 minutes after the Mac starts, and then every 6 hours, FocusGuard checks this GitHub repo for a new version. It downloads it, makes sure it loads, swaps it in and restarts in the background; settings are kept. If a new version can't start, it switches back to the previous one. The **Check for updates** button in the app does it right away. (Only changes to the installer itself need a reinstall.)

If the Mac has never used Python, a popup asks to install Apple's Command Line Tools. Click **Install**, wait, then run the installer again.

To remove it, double-click **`Uninstall FocusGuard.command`** (or run `zsh ~/Downloads/FocusGuard-main/uninstall.sh`). An administrator's password is needed. Ollama stays installed; drag it to the Trash if you don't want it.

### Which AI?

| | Gemini (default) | Local / Ollama | Claude (optional) |
|---|---|---|---|
| Cost | $0 (Google's free tier) | $0 | Paid per use |
| Key needed | Yes, free from aistudio.google.com/apikey | No | Yes, from console.anthropic.com |
| Privacy | Page titles/URLs and screenshots are sent to Google | Everything stays on your Mac | Page titles/URLs are sent to Anthropic |
| Needs | Internet | Apple Silicon Mac (M1+), ~8 GB free memory, Ollama | Internet |
| Upkeep | None | Ollama has to be running | None |

Keys are stored where only root can read them. With Gemini, FocusGuard uses the newest Flash-Lite model, which has the biggest free daily allowance. If Gemini can't answer (no internet, or the free allowance for the day is used up), the local AI answers instead when Ollama is installed. If Ollama isn't running, FocusGuard starts it. While no AI can be reached, unlock requests fail and page checks are skipped.

Permissions macOS will ask for:
- **Automation:** allow python to control Safari/Chrome and System Events. Without this, websites in that browser can't be checked (FocusGuard warns about it).
- **Accessibility (optional):** add `/Library/Application Support/FocusGuard/venv/bin/python` so FocusGuard can read window titles of unlocked *apps* for the on-task check.

## Settings

Change them in the app (administrator password needed). They're stored in `/Library/Application Support/FocusGuard/config.json`:
`allowed_apps` / `allowed_sites` (work), `limited_apps` / `limited_sites` (`[{"target": "chess.com", "minutes": 10}]`), `max_grant_minutes` (90), `distraction_penalty_minutes` (60), `on_task_check_seconds` (60), `provider` (`gemini`, `ollama` or `anthropic`), `gemini_model` (`auto`), `ai_model_size` (`auto`, `small`, `medium` or `large`), `claude_model` (`claude-opus-5-5`). The log is `activity.log` in the same folder.

## Files

- `focusguard/rootd.py`: root service (socket server, AI chat, watchdog)
- `focusguard/updater.py`: automatic updates from GitHub, with rollback
- `focusguard/daemon.py`: decision engine (what to block, daily time, unlocks, penalties)
- `focusguard/agent.py`: user-session helper (reads front app/tab, carries out actions)
- `focusguard/admin.py`: root-only tool behind the administrator password (settings, pause)
- `focusguard/gui.py`: setup, time left today, AI chat
- `focusguard/ai.py`: AI calls (gatekeeper, on-task check, distraction check)
- `focusguard/macos.py`: `lsappinfo` and `osascript` helpers
- `focusguard/common.py`: config/state files, site matching, client for the root service
- `install.sh` / `install_root.sh`, `uninstall.sh` / `uninstall_root.sh`

## Limits

Anyone with the administrator password can turn it off. Without it, a determined user could still boot the Mac from another disk or recovery mode, or use a different device.

## Daily schedule

| When | What applies |
|---|---|
| Midnight until the day's clock-in time | **School time:** nothing is blocked and the AI is off. |
| From the clock-in time (or earlier with **Clock in now**) until midnight | **Home rules:** the normal blocking described above. |
| **Dinner break** (once a day, at home) | 30 minutes with nothing blocked, then home rules come back by themselves. |
| **Task** (at home, up to 2 hours) | Type what you're working on and up to 2 links or apps. Those are never blocked or judged during the task; everything else follows home rules. |

Each day of the week has its own clock-in time (**Clock-in times…** in the main window). Changing them needs the password of the Mac account FocusGuard is set up for, not an administrator password. Home rules start by themselves at the clock-in time.

A task link can be a whole site (`docs.google.com`) or one page (`youtube.com/watch?v=…` allows just that video). Before a task starts, the AI checks that the links and apps make sense for it (FocusGuard fetches each link's page title, and a YouTube video's title and channel, to show the AI). It turns down games, entertainment, links that don't fit the task, and whole sites like youtube.com where one page is enough, and says what to change. If the AI can't answer, the task doesn't start.

The AI chat gives up after a minute if the AI doesn't answer.

## The AI reads the screen

On work and limited sites the AI judges each page by its title, and also looks at a screenshot: when a page opens, and every 90 seconds while it stays open (every 3 minutes on Intel Macs, where it's slower). So a game on a site called "Math Practice" is still caught. Screenshots are judged by the free AI on the Mac and never saved or sent anywhere.

This needs **Screen Recording** permission: System Settings → Privacy & Security → Screen Recording → turn on **python**. Until it's on, FocusGuard shows a reminder and judges by titles only. The screen models need a recent Ollama (0.7 or newer); the Ollama app updates itself.

Strikes and hour-long blocks apply to the exact site, e.g. `math-practice.vercel.app`, not everything under `vercel.app`.

## Which free AI model it uses

Bigger models judge more accurately but need more memory and are slower. With **Automatic** (the default), FocusGuard picks one from the Mac's chip and memory:

| Mac | Text model (page titles, chat, tasks) | Screen model | Download |
|---|---|---|---|
| Apple Silicon (M1–M4), 24 GB memory or more | Large: `qwen2.5:14b` | `qwen2.5vl:7b` | about 15 GB |
| Apple Silicon, 16 GB | Medium: `qwen2.5:7b` | `qwen2.5vl:7b` | about 11 GB |
| Apple Silicon, 8 GB | Small: `qwen2.5:3b` | `qwen2.5vl:3b` | about 5 GB |
| Intel Mac (any memory) | Small: `qwen2.5:3b` | `qwen2.5vl:3b` | about 5 GB |

The screen models are only used for screenshots: in Ollama they read text about 8 times slower than the text models, which made every check take minutes.

You can pick a different size in Setup (needs the administrator password). A new model downloads in the background, and a model already on the Mac is used until it's ready. On Intel Macs the free AI is slow and makes more mistakes.

Apps are recognised by what they really are (where they're installed and the developer's app ID), not just their name, so an app whose file is called `BambuStudio` but shows as "Bambu Studio" still counts, and a game renamed to look like an allowed app doesn't.
