"""The LLM 'brain': talks to any OpenAI-compatible chat API and runs the tool loop."""
from __future__ import annotations

import datetime as dt
import json
import logging
import platform
import threading
import time
from typing import Callable

import requests

from . import tools
from .config import Settings

log = logging.getLogger("jarvis.brain")

MAX_STEPS = 8
KEEP_MESSAGES = 30

SYSTEM_PROMPT = """You are JARVIS, {name}'s personal assistant, executive assistant and close friend. \
You run on their Windows laptop and can really control it through your tools.

Style: your replies are spoken aloud, so answer in one to three short plain sentences. \
No markdown, no lists, no emojis, and never read out long URLs. Be warm, quick and a little witty, \
like Tony Stark's JARVIS but friendlier. Address the user as {name}.

Rules:
- When asked to do something, do it with tools instead of explaining how. Chain tools when needed.
- Never claim you did something unless a tool result confirmed it. If a tool fails, say so plainly and suggest the next step.
- You cannot place or pay for orders yourself yet. For food or shopping, open the search for the user and tell them to finish checkout. Never invent order confirmations.
- Use the current time below and pass absolute dates and times (YYYY-MM-DD HH:MM) to tools.
- Ask a brief question only if the request is truly ambiguous; otherwise choose a sensible default.
- Be a proactive PA: when the user mentions something they need to do, add it as a task; use reminders for anything time-bound.

Current time: {now}. Operating system: {system}."""

Confirm = Callable[[str], bool]


def _describe(name: str, args: dict) -> str:
    if name == "run_shell":
        return f"run the command: {args.get('command', '')}"
    if name == "power":
        return f"{args.get('action', 'do a power action on')} the computer"
    return f"{name.replace('_', ' ')} with {json.dumps(args)}"


class Brain:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.history: list[dict] = []
        self._lock = threading.RLock()
        self._post = requests.post  # replaceable in tests
        self._sleep = time.sleep

    # ---- prompt ------------------------------------------------------
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            name=self.settings.user_name,
            now=dt.datetime.now().strftime("%A, %Y-%m-%d %H:%M"),
            system=f"{platform.system()} {platform.release()}",
        )

    # ---- LLM call ----------------------------------------------------
    def _chat(self, messages: list) -> dict:
        s = self.settings
        headers = {"Content-Type": "application/json"}
        if s.api_key:
            headers["Authorization"] = f"Bearer {s.api_key}"
        payload = {
            "model": s.model,
            "messages": messages,
            "tools": tools.tool_specs(),
            "tool_choice": "auto",
            "temperature": 0.4,
        }
        for attempt in range(4):
            r = self._post(f"{s.base_url}/chat/completions", headers=headers, json=payload, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float(r.headers.get("retry-after", 2 ** attempt))
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                log.warning("LLM %s, retrying in %.0fs", r.status_code, min(wait, 20))
                self._sleep(min(wait, 20))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"LLM error {r.status_code}: {r.text[:300]}")
            return r.json()["choices"][0]["message"]
        raise RuntimeError("The AI service is rate limiting me. Give it a minute.")

    # ---- tool execution ----------------------------------------------
    def _run_tool(self, call: dict, confirm: Confirm | None) -> str:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError as e:
            return f"Could not parse arguments for {name}: {e}"
        t = tools.REGISTRY.get(name)
        if t and t.needs_confirm(args):
            approved = bool(confirm and confirm(f"I'm about to {_describe(name, args)}. Shall I proceed?"))
            if not approved:
                log.info("declined: %s %s", name, args)
                return "The user did not approve this action, so it was NOT performed."
        log.info("tool: %s %s", name, args)
        result = tools.call_tool(name, args)
        log.info("  -> %s", result[:200].replace("\n", " "))
        return result

    # ---- public API --------------------------------------------------
    def handle(self, text: str, confirm: Confirm | None = None) -> str:
        if self.settings.needs_key and not self.settings.api_key:
            return (f"I don't have an API key for {self.settings.provider} yet. "
                    "Add it to the dot env file and restart me.")
        with self._lock:
            start = len(self.history)
            self.history.append({"role": "user", "content": text})
            try:
                for _ in range(MAX_STEPS):
                    msg = self._chat([{"role": "system", "content": self.system_prompt()}] + self.history)
                    calls = msg.get("tool_calls")
                    entry = {"role": "assistant", "content": msg.get("content") or ""}
                    if calls:
                        entry["tool_calls"] = calls
                    self.history.append(entry)
                    if not calls:
                        self._trim()
                        return entry["content"].strip() or "Done."
                    for call in calls:
                        self.history.append({
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": self._run_tool(call, confirm),
                        })
                self._trim()
                return "I went around in circles on that one. Could you rephrase it?"
            except Exception as e:
                log.exception("brain failure")
                del self.history[start:]
                return f"I can't reach my brain right now. {e}"

    def reset(self) -> None:
        with self._lock:
            self.history.clear()

    def _trim(self) -> None:
        if len(self.history) <= KEEP_MESSAGES + 10:
            return
        for i in range(len(self.history) - KEEP_MESSAGES, len(self.history)):
            if self.history[i]["role"] == "user":
                del self.history[:i]
                return
