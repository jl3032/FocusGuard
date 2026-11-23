# FocusGuard

A self-control tool for macOS that applies to **one Mac user account only**. Blocking is always on; there are no sessions to start.

- **Work apps and sites:** you pick the apps and websites you use for work. They always work.
- **Limited apps and sites:** you pick a few you're allowed for a set time each day, e.g. chess.com for 10 minutes. Time only counts while it's on screen; you get a warning at 5 and 1 minutes left, then it's blocked until midnight. The AI also judges limited sites by what they're for: playing chess on chess.com is fine, but on a general site like YouTube only learning content is (gaming videos or Shorts scrolling get it blocked for an hour).
- **Everything else is blocked**, e.g. ea.com when only chess.com is on your limited list.
- **Built-in AI gatekeeper:** open FocusGuard and tell the AI why you need something that's blocked. If your reason is specific and real, it unlocks the app or site for a limited time and writes down what you said you'd do there.
- **Monitoring after an unlock:** about every 60 seconds the AI compares your recent pages or window titles with that stated purpose. If you're off task, access is taken away and the item is blocked again.
- **AI watches every page on your work sites.** It reads each page you open (even while you scroll Shorts or Reels) and judges whether it is work. If it is distracting, e.g. gaming videos on YouTube, that whole site is blocked for an hour.
- **Admin password for changes:** editing setup, or pausing all blocking for 5–240 minutes, needs a Mac administrator's name and password (macOS's own dialog; FocusGuard never sees the password). For this to mean anything, the blocked user should be a *standard* account and the password should belong to someone else, e.g. a parent.

## How it's protected

FocusGuard has three parts:

| Part | Runs as | Job |
|---|---|---|
| Root service (`/Library/LaunchDaemons/com.focusguard.root.plist`) | root | Makes every decision, holds all settings and state, runs the AI chat. The blocked user can't stop it, edit its files or uninstall it. |
| Helper (`/Library/LaunchAgents/com.focusguard.agent.plist`) | the blocked user | Tells the root service which app and tab are in front, and quits/redirects what it's told to. If it's killed it is restarted, and meanwhile the root service closes blocked apps and browsers itself. Reports from any other process are ignored. |
| Window (`/Applications/FocusGuard.app`) | the blocked user | Shows status and the AI chat. Saving settings or pausing needs an administrator's password. |

Files live in `/Library/Application Support/FocusGuard`, owned by root. It only enforces for the user who ran the installer; other users on the Mac aren't affected.

Workarounds it closes:
- **Killing or unloading it** (Activity Monitor, Terminal, VS Code): the root service restarts the helper and closes browsers until it's back.
- **Editing its settings** (TextEdit, scripts): the files are root-owned.
- **Uninstalling, pausing, changing lists**: need an administrator password; the password window says exactly what is being newly allowed.
- **Renaming a game** to an allowed app's name (`Notes.app`, `Finder.app`): apps are matched by location and bundle ID.
- **Games run through Python**: "Python" is only allowed when it's FocusGuard's own window.
- **Turning off the browser permission**: the browser is closed instead of websites going unchecked.
- **Game sites on hosting platforms** (`*.vercel.app`, `*.github.io`, `sites.google.com`, …): allowing `google.com` or `vercel.app` doesn't cover them; list the exact site.
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
5. **Wait for it to finish.** It sets up the free AI too (about a 4.7 GB download the first time), then opens FocusGuard. Pick the apps and sites, then an administrator approves the settings with their password.
6. When macOS asks to let python control Safari/Chrome or System Events, click **Allow**.

Updating works the same way: download the new ZIP and run the installer again. Your settings are kept.

If the Mac has never used Python, a popup asks to install Apple's Command Line Tools. Click **Install**, wait, then run the installer again.

To remove it, double-click **`Uninstall FocusGuard.command`** (or run `zsh ~/Downloads/FocusGuard-main/uninstall.sh`). An administrator's password is needed. Ollama stays installed; drag it to the Trash if you don't want it.

### Which AI?

| | Free (default) | Claude (optional) |
|---|---|---|
| Cost | $0 | Paid per use |
| Key needed | No | Yes, from console.anthropic.com (stored where only root can read it) |
| Privacy | Everything stays on your Mac | Page titles/URLs are sent to Anthropic |
| Needs | Apple Silicon Mac (M1+), ~8 GB free memory, Ollama | Internet |
| Judgment | Good | Better |

If Ollama isn't running, FocusGuard starts it. While the AI can't be reached, unlock requests fail and page checks are skipped.

Permissions macOS will ask for:
- **Automation:** allow python to control Safari/Chrome and System Events. Without this, the browser is closed because its websites can't be checked.
- **Accessibility (optional):** add `/Library/Application Support/FocusGuard/venv/bin/python` so FocusGuard can read window titles of unlocked *apps* for the on-task check.

## Settings

Change them in the app (administrator password needed). They're stored in `/Library/Application Support/FocusGuard/config.json`:
`allowed_apps` / `allowed_sites` (work), `limited_apps` / `limited_sites` (`[{"target": "chess.com", "minutes": 10}]`), `max_grant_minutes` (90), `distraction_penalty_minutes` (60), `on_task_check_seconds` (60), `provider` (`ollama` or `anthropic`), `ollama_model` (`qwen2.5:7b`), `claude_model` (`claude-opus-5-5`). The log is `activity.log` in the same folder.

## Files

- `focusguard/rootd.py`: root service (socket server, AI chat, watchdog)
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
