# FocusGuard

A self-control tool for macOS that applies to **one Mac user account only**. Blocking is always on; there are no sessions to start.

- **Work apps and sites:** you pick the apps and websites you use for work. They always work.
- **Limited apps and sites:** you pick a few you're allowed for a set time each day, e.g. chess.com for 10 minutes. Time only counts while it's on screen; you get a warning at 5 and 1 minutes left, then it's blocked until midnight.
- **Everything else is blocked**, e.g. ea.com when only chess.com is on your limited list.
- **Built-in AI gatekeeper:** open FocusGuard and tell the AI why you need something that's blocked. If your reason is specific and real, it unlocks the app or site for a limited time and writes down what you said you'd do there.
- **Monitoring after an unlock:** about every 60 seconds the AI compares your recent pages or window titles with that stated purpose. If you're off task, access is taken away and the item is blocked again.
- **AI watches every page on your work sites.** It reads each page you open (even while you scroll Shorts or Reels) and judges whether it is work. If it is distracting, e.g. gaming videos on YouTube, that whole site is blocked for an hour.
- **Changing your lists** needs you to type a confirmation sentence, so it isn't a one-click escape.

## Why it only affects one user

The blocker runs as a **LaunchAgent** in `~/Library/LaunchAgents`, which macOS starts only inside your own login session. Its settings live in `~/Library/Application Support/FocusGuard`. It doesn't touch `/etc/hosts`, the firewall, or anything else system-wide, so a second Mac user is never affected.

## How blocking works

| What | How |
|---|---|
| Apps not on your lists (or out of daily time) | Asked to quit as soon as they come to the front (your unsaved work still gets a save prompt) |
| Websites in Safari, Chrome, Arc, Brave, Edge, Vivaldi | The current tab's URL is read through AppleScript; a blocked tab is sent to a "Stay on task" page |
| Firefox, Opera, Zen, Orion | Their URLs can't be read, so these apps are always blocked |

## Install (on this Mac or any other)

1. **Download it.** On the GitHub page, click the green **Code** button, then **Download ZIP**. Double-click the zip in Downloads to unpack it. (You can also copy the folder from another Mac by AirDrop or USB stick.)
2. **Log in as the user who should be blocked**, open the folder, and double-click **`Install FocusGuard.command`**.
   - If macOS says it "can't be opened", right-click the file, choose **Open**, then click **Open** again.
3. **Wait for it to finish.** It sets everything up, including the free AI (about a 4.7 GB download the first time), then opens FocusGuard so you can pick your apps and sites.
4. When macOS asks to let Python control Safari/Chrome or System Events, click **Allow**.

That's it, and you can delete the folder you installed from. Installing applies only to the user who was logged in; to cover another user on the same Mac, log in as them and repeat step 2.

If double-clicking says you don't have permission, open Terminal and run `zsh ~/Downloads/FocusGuard-main/install.sh` instead.

If the Mac has never used Python, a popup asks to install Apple's Command Line Tools. Click **Install**, wait, then double-click `Install FocusGuard.command` again.

To remove it, double-click **`Uninstall FocusGuard.command`**. That deletes FocusGuard and its settings for that user. Ollama stays installed; drag it from `~/Applications` to the Trash if you don't want it.

### Which AI?

| | Free (default) | Claude (optional) |
|---|---|---|
| Cost | $0 | Paid per use |
| Key needed | No | Yes, from console.anthropic.com (stored in Keychain) |
| Privacy | Everything stays on your Mac | Page titles/URLs are sent to Anthropic |
| Needs | Apple Silicon Mac (M1+), ~8 GB free memory, Ollama running | Internet |
| Judgment | Good | Better |

You pick one on the first setup screen. If Ollama isn't running, FocusGuard tells you; open the Ollama app or run `brew services start ollama`.

Permissions macOS will ask for:
- **Automation:** allow Python to control Safari/Chrome and System Events. Without this, websites can't be checked.
- **Accessibility (optional):** add `~/Library/Application Support/FocusGuard/venv/bin/python` so FocusGuard can read window titles of unlocked *apps* for the on-task check.

## Settings

`~/Library/Application Support/FocusGuard/config.json`:
`allowed_apps` / `allowed_sites` (work), `limited_apps` / `limited_sites` (`[{"target": "chess.com", "minutes": 10}]`), `max_grant_minutes` (90), `distraction_penalty_minutes` (60), `on_task_check_seconds` (60), `provider` (`ollama` or `anthropic`), `ollama_model` (`qwen2.5:7b`; on a Mac with 16 GB+ memory you can try a bigger model, e.g. `qwen2.5:14b`, after `ollama pull` for it), `claude_model` (`claude-opus-5-5`; `claude-haiku-5-5` is much cheaper). The log is in `activity.log` in the same folder.

## Files

- `focusguard/daemon.py`: background enforcer (blocks things, counts daily time on limited items, watches unlocks, applies penalties)
- `focusguard/gui.py`: setup (work + limited lists), time left today, AI chat
- `focusguard/ai.py`: Claude calls (gatekeeper, on-task check, distraction check)
- `focusguard/macos.py`: `lsappinfo` and `osascript` helpers
- `focusguard/common.py`: config/state files, Keychain, domain matching

## Limits

This is a self-control tool, not a lock you can't get out of. An admin user can still unload the LaunchAgent with `launchctl` or delete the files. If the AI can't be reached, unlock requests fail and the activity checks are skipped, so blocked things stay blocked.
