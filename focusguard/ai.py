"""The built-in AI: gatekeeper for unblock requests + activity judge.

Two providers:
- "ollama"    (default): free, runs on this Mac via Ollama, no API key, nothing leaves the computer.
- "anthropic": Claude API (paid, needs a key; smarter judgments).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .common import get_api_key, load_config

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIError(Exception):
    pass


def _json_call(system: str, messages: list, schema: dict,
               effort: str = "low", max_tokens: int = 6000) -> dict:
    cfg = load_config()
    if cfg.get("provider", "ollama") == "anthropic":
        return _claude_call(cfg, system, messages, schema, effort, max_tokens)
    return _ollama_call(cfg, system, messages, schema)


# ------------------------------------------------------------- Ollama (free, local)
def _ollama_call(cfg: dict, system: str, messages: list, schema: dict) -> dict:
    model = cfg["ollama_model"]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "format": schema,          # Ollama structured outputs: reply must match the schema
        "stream": False,
        "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(cfg["ollama_url"].rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        # Generous timeout: the first call loads the model into memory.
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code == 404 or "not found" in detail:
            raise AIError(f"The AI model isn't downloaded. In Terminal run: ollama pull {model}") from e
        raise AIError(f"Ollama error {e.code}: {detail[:200]}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise AIError("The local AI isn't running. Open the Ollama app "
                      "(or run: brew services start ollama).") from e
    text = (data.get("message") or {}).get("content", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("The local AI returned malformed output. Try again.") from e


# ------------------------------------------------------------- Claude (optional, paid)
def _claude_call(cfg: dict, system: str, messages: list, schema: dict,
                 effort: str, max_tokens: int) -> dict:
    try:
        import anthropic
    except ImportError as e:
        raise AIError("Claude support isn't installed. Re-run Install FocusGuard.command") from e
    key = get_api_key()
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=cfg["claude_model"],
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=system,
            messages=messages,
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        )
    except anthropic.AuthenticationError as e:
        raise AIError("Your Anthropic API key was rejected. Update it in Setup.") from e
    except anthropic.RateLimitError as e:
        raise AIError("The AI is rate-limited right now. Try again in a minute.") from e
    except anthropic.APIStatusError as e:
        raise AIError(f"AI error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Couldn't reach the AI (no internet?).") from e

    if resp.stop_reason == "refusal":
        raise AIError("The AI declined to evaluate this.")
    if resp.stop_reason == "max_tokens":
        raise AIError("The AI response was cut off.")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise AIError("The AI returned an empty response.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("The AI returned malformed output.") from e


# ------------------------------------------------------------- gatekeeper
GATEKEEPER_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "decision": {"type": "string", "enum": ["grant", "deny", "ask"]},
        "target_kind": {"type": "string", "enum": ["app", "site", "none"]},
        "target": {"type": "string"},
        "duration_minutes": {"type": "integer"},
        "purpose": {"type": "string"},
    },
    "required": ["reply", "decision", "target_kind", "target",
                 "duration_minutes", "purpose"],
    "additionalProperties": False,
}

GATEKEEPER_SYSTEM = """You are the gatekeeper built into FocusGuard, a focus tool the user installed \
on their own Mac to keep themselves on task. During a focus session everything except their \
allowed apps and websites is blocked. The user is now asking you to unlock something.

How to decide:
- grant: the user gives a specific, believable reason the blocked item is needed for their \
work, study or a genuine necessity (e.g. "I need YouTube to watch the lecture my teacher posted").
- ask: the request is vague ("I need YouTube"). Ask one short question about what exactly they \
will do there.
- deny: the reason is entertainment, boredom, "just a quick break", or clearly procrastination. \
Be kind but firm, and suggest getting back to work or waiting until the session ends.
- Distraction penalties (listed below) cannot be lifted by you; tell the user when one ends.

When granting:
- target: for a website, the bare domain only (e.g. "youtube.com"); for an app, its exact name \
as macOS shows it (use the name from "recently blocked" when it matches).
- duration_minutes: the shortest reasonable time, at most {max_minutes}.
- purpose: one precise sentence describing what activity is acceptable. FocusGuard will monitor \
the user's activity against this sentence and re-block the item if they drift off task, so make \
it specific (e.g. "Watching the MIT 18.06 linear algebra lecture videos").
- Tell the user in your reply that you'll be watching their activity on it.

When not granting, set target_kind "none", target "", duration_minutes 0, purpose "".
Keep "reply" to 1-3 friendly sentences, written directly to the user.

Current situation:
{context}"""


def gatekeeper(chat: list, context: str) -> dict:
    cfg = load_config()
    system = GATEKEEPER_SYSTEM.format(max_minutes=cfg["max_grant_minutes"], context=context)
    result = _json_call(system, chat, GATEKEEPER_SCHEMA, effort="medium")
    for k, v in (("reply", "Sorry, I didn't catch that. Can you say it again?"),
                 ("decision", "ask"), ("target_kind", "none"), ("target", ""),
                 ("duration_minutes", 0), ("purpose", "")):
        result.setdefault(k, v)
    result["duration_minutes"] = max(1, min(int(result.get("duration_minutes") or 0),
                                            cfg["max_grant_minutes"]))
    return result


# ------------------------------------------------------------- on-task check
ON_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "on_task": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["on_task", "reason"],
    "additionalProperties": False,
}

ON_TASK_SYSTEM = """You monitor activity for FocusGuard, a focus tool the user installed on \
their own Mac. The user was temporarily given access to something for a stated purpose. You get \
the purpose and the pages/windows they've had open on it recently (most recent last).

Return on_task=false only when the recent activity is clearly unrelated to the purpose (e.g. \
purpose is a chemistry lecture, but they're now watching gaming videos). Related pages, search \
results, navigation, logins, loading screens and ambiguous titles count as on task. \
"reason" is one short sentence addressed to the user."""


def check_on_task(grant: dict, samples: list) -> dict:
    activity = "\n".join(f"- {s}" for s in samples) or "- (no details available)"
    msg = (f"Unlocked: {grant['kind']} '{grant['target']}'\n"
           f"Purpose: {grant['purpose']}\n"
           f"Recent activity:\n{activity}")
    r = _json_call(ON_TASK_SYSTEM, [{"role": "user", "content": msg}],
                   ON_TASK_SCHEMA, max_tokens=3000)
    return {"on_task": bool(r.get("on_task", True)), "reason": r.get("reason", "")}


# ------------------------------------------------------------- distraction check
DISTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "distracting": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["distracting", "reason"],
    "additionalProperties": False,
}

DISTRACTION_SYSTEM = """You help FocusGuard, a focus tool the user installed on their own Mac. \
The user is in a focus session on a website they marked as allowed for work. Some allowed sites \
can also be used for distraction (e.g. YouTube for lectures vs. gaming videos, Reddit for a \
programming question vs. memes, news sites for doom-scrolling).

Judge only the page shown. distracting=true only when the page is clearly entertainment, \
social scrolling, gaming, celebrity/sports gossip, shopping for fun or similar. Work, study, \
reference, documentation, email, tools, search pages and anything ambiguous are not distracting. \
"reason" is one short sentence addressed to the user."""


def check_distraction(url: str, title: str, allowed_sites: list) -> dict:
    msg = (f"Time: {time.strftime('%A %H:%M')}\n"
           f"User's allowed sites: {', '.join(allowed_sites)}\n"
           f"Current page URL: {url}\nCurrent page title: {title}")
    r = _json_call(DISTRACTION_SYSTEM, [{"role": "user", "content": msg}],
                   DISTRACTION_SCHEMA, max_tokens=3000)
    return {"distracting": bool(r.get("distracting", False)), "reason": r.get("reason", "")}
