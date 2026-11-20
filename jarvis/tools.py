"""Everything Jarvis can *do*. Each @tool function is exposed to the LLM automatically.

Windows is the primary target; most tools degrade gracefully elsewhere so the
logic can be tested on any OS.
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import typing
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .tasks import PRIORITIES, TaskStore

IS_WINDOWS = os.name == "nt"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STATE: dict = {"tasks": None, "data_dir": Path("data")}


def init(tasks: TaskStore, data_dir: Path | None = None) -> None:
    STATE["tasks"] = tasks
    if data_dir:
        STATE["data_dir"] = Path(data_dir)


def _tasks() -> TaskStore:
    if STATE["tasks"] is None:
        raise RuntimeError("Task store not initialised.")
    return STATE["tasks"]


# --------------------------------------------------------------------------
# Tool registry
# --------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    schema: dict
    confirm: bool | Callable[[dict], bool] = False

    def needs_confirm(self, args: dict) -> bool:
        return bool(self.confirm(args)) if callable(self.confirm) else bool(self.confirm)


REGISTRY: dict[str, Tool] = {}
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _schema_for(fn: Callable, param_docs: dict) -> dict:
    hints = typing.get_type_hints(fn)
    props, required = {}, []
    for name, param in inspect.signature(fn).parameters.items():
        prop = {"type": _JSON_TYPES.get(hints.get(name, str), "string")}
        if name in param_docs:
            prop["description"] = param_docs[name]
        props[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def tool(description: str, confirm: bool | Callable[[dict], bool] = False, **param_docs):
    def decorate(fn: Callable) -> Callable:
        REGISTRY[fn.__name__] = Tool(fn.__name__, description, fn, _schema_for(fn, param_docs), confirm)
        return fn

    return decorate


def tool_specs() -> list:
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
        for t in REGISTRY.values()
    ]


def call_tool(name: str, args: dict) -> str:
    t = REGISTRY.get(name)
    if not t:
        return f"Unknown tool: {name}"
    try:
        out = t.fn(**args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:  # tools must never crash the assistant
        return f"{name} failed: {type(e).__name__}: {e}"
    return out if isinstance(out, str) else json.dumps(out, default=str)


def _clip(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------
@tool("Get the current local date and time.")
def current_time() -> str:
    return dt.datetime.now().strftime("%A, %d %B %Y, %H:%M")


# --------------------------------------------------------------------------
# Opening things
# --------------------------------------------------------------------------
APP_ALIASES = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "task manager": "taskmgr", "settings": "ms-settings:", "command prompt": "cmd", "cmd": "cmd",
    "powershell": "powershell", "terminal": "wt", "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "firefox": "firefox", "vscode": "code", "vs code": "code",
    "visual studio code": "code", "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "outlook": "outlook", "spotify": "spotify:", "whatsapp": "whatsapp:",
    "snipping tool": "snippingtool", "camera": "microsoft.windows.camera:",
    "calendar": "outlookcal:", "mail": "outlookmail:", "store": "ms-windows-store:",
}
_UNSAFE = re.compile(r'[&|<>^"%\r\n]')


@tool("Open an application on the PC by name (chrome, spotify, notepad, vs code, settings...).",
      name="App name such as 'chrome' or 'calculator'")
def open_app(name: str) -> str:
    key = name.strip().lower()
    target = APP_ALIASES.get(key, key)
    if not target or _UNSAFE.search(target):
        return "Refusing: that app name is empty or has unsafe characters."
    if IS_WINDOWS:
        subprocess.Popen(f'start "" "{target}"', shell=True)
        return f"Opened {name}."
    exe = shutil.which(target)
    if exe:
        subprocess.Popen([exe])
        return f"Opened {name}."
    return f"Couldn't find an app called {name} on this system."


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http and https links can be opened.")
    return url


@tool("Open a website in the default browser.", url="Address, e.g. 'youtube.com' or a full https URL")
def open_website(url: str) -> str:
    url = _normalise_url(url)
    webbrowser.open(url)
    return f"Opened {url}."


@tool("Open a file or folder on the PC with its default program.", path="File or folder path; ~ is allowed")
def open_path(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"{p} does not exist."
    if IS_WINDOWS:
        os.startfile(str(p))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
    return f"Opened {p}."


@tool("Find files by name under a folder (wildcards ok, e.g. '*.pdf' or 'resume').",
      pattern="File name or glob pattern", folder="Folder to search, default home", limit="Max results")
def find_files(pattern: str, folder: str = "~", limit: int = 15) -> str:
    root = Path(folder).expanduser()
    if not any(c in pattern for c in "*?["):
        pattern = f"*{pattern}*"
    end, hits = time.time() + 10, []
    try:
        for p in root.rglob(pattern):
            hits.append(str(p))
            if len(hits) >= limit or time.time() > end:
                break
    except (PermissionError, OSError):
        pass
    return "\n".join(hits) if hits else f"No files matching '{pattern}' under {root}."


# --------------------------------------------------------------------------
# Web: search and scraping
# --------------------------------------------------------------------------
def _http_get(url: str, params: dict | None = None, timeout: int = 15):
    import requests

    r = requests.get(url, params=params, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=timeout)
    r.raise_for_status()
    return r


def _clean_ddg_url(href: str) -> str:
    if "uddg=" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    return "https:" + href if href.startswith("//") else href


def _parse_ddg(html: str, limit: int) -> list:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results = []
    for box in soup.select("div.result"):
        link = box.select_one("a.result__a")
        if not link or not link.get("href"):
            continue
        snippet = box.select_one(".result__snippet")
        results.append({
            "title": link.get_text(" ", strip=True),
            "url": _clean_ddg_url(link["href"]),
            "snippet": snippet.get_text(" ", strip=True) if snippet else "",
        })
        if len(results) >= limit:
            break
    return results


@tool("Search the web and return the top results (title, url, snippet). Use for facts, news, prices.",
      query="Search query", max_results="How many results, default 5")
def web_search(query: str, max_results: int = 5) -> str:
    html = _http_get("https://html.duckduckgo.com/html/", params={"q": query}).text
    results = _parse_ddg(html, max(1, min(int(max_results), 10)))
    if not results:
        return "No results found."
    return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)


def _html_to_text(html: str, selector: str = "") -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    else:
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        if selector:
            nodes = soup.select(selector)
            if not nodes:
                return f"(no elements matched '{selector}')"
            text = "\n".join(n.get_text(" ", strip=True) for n in nodes)
        else:
            text = soup.get_text("\n", strip=True)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


@tool("Download a web page and return its readable text. Optionally limit to a CSS selector (scraping).",
      url="Page URL", selector="Optional CSS selector, e.g. 'h2' or '.price'", max_chars="Max characters to return")
def fetch_page(url: str, selector: str = "", max_chars: int = 4000) -> str:
    html = _http_get(_normalise_url(url), timeout=20).text
    return _clip(_html_to_text(html, selector), max(500, min(int(max_chars), 12000)))


_SITES = {
    "food": {
        "swiggy": "https://www.swiggy.com/search?query={q}",
        "zomato": "https://www.google.com/search?q=zomato+{q}",
    },
    "shop": {
        "amazon": "https://www.amazon.in/s?k={q}",
        "flipkart": "https://www.flipkart.com/search?q={q}",
    },
    "video": {"youtube": "https://www.youtube.com/results?search_query={q}"},
}


def _open_search(kind: str, site: str, query: str) -> str:
    template = _SITES[kind].get(site.strip().lower())
    if not template:
        return f"Unknown site '{site}'. Options: {', '.join(_SITES[kind])}."
    url = template.format(q=urllib.parse.quote_plus(query))
    webbrowser.open(url)
    return url


@tool("Open a food search on Swiggy or Zomato in the browser. Cannot place or pay for the order itself.",
      query="Dish or restaurant", platform="swiggy or zomato")
def find_food(query: str, platform: str = "swiggy") -> str:
    out = _open_search("food", platform, query)
    return f"Opened {platform} search for '{query}' ({out}). I have NOT placed an order; the user must pick and check out."


@tool("Open a product search on Amazon or Flipkart in the browser. Cannot buy or pay by itself.",
      query="Product to look for", store="amazon or flipkart")
def shop_search(query: str, store: str = "amazon") -> str:
    out = _open_search("shop", store, query)
    return f"Opened {store} search for '{query}' ({out}). I have NOT bought anything; the user must check out."


@tool("Open YouTube search results for a song, video or topic.", query="What to search for")
def search_youtube(query: str) -> str:
    return f"Opened YouTube results ({_open_search('video', 'youtube', query)})."


# --------------------------------------------------------------------------
# Shell and keyboard
# --------------------------------------------------------------------------
_RISKY = re.compile(
    r"(?i)\b(rm|del|erase|rd|rmdir|remove-item|format|diskpart|shutdown|restart-computer|stop-computer|"
    r"reg|taskkill|stop-process|set-executionpolicy|net|sc|mkfs|dd|chmod|chown|mv|move|ren|rename|"
    r"clear-content|set-content|out-file|invoke-expression|iex|invoke-webrequest|iwr|curl|wget|"
    r"start-process|msiexec|winget|choco|pip|npm)\b|>"
)


def _shell_risky(args: dict) -> bool:
    return bool(_RISKY.search(str(args.get("command", ""))))


@tool("Run a PowerShell command (bash on non-Windows) and return its output. Risky commands ask the user first.",
      confirm=_shell_risky, command="The command line to run")
def run_shell(command: str) -> str:
    cmd = (["powershell", "-NoProfile", "-NonInteractive", "-Command", command] if IS_WINDOWS
           else ["bash", "-lc", command])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "The command timed out after 30 seconds."
    out = (p.stdout + p.stderr).strip()
    return _clip(out or f"(no output, exit code {p.returncode})")


def _pyautogui():
    try:
        import pyautogui
    except ImportError as e:
        raise RuntimeError("pyautogui is not installed (pip install pyautogui).") from e
    pyautogui.FAILSAFE = True
    return pyautogui


@tool("Type text into whatever window currently has focus.", text="Text to type")
def type_text(text: str) -> str:
    gui = _pyautogui()
    if text.isascii():
        gui.write(text, interval=0.015)
    else:  # non-ASCII: paste via clipboard
        import pyperclip

        pyperclip.copy(text)
        gui.hotkey("ctrl", "v")
    return f"Typed {len(text)} characters."


@tool("Press a key or shortcut, e.g. 'enter', 'ctrl+c', 'alt+tab', 'win+d'.", keys="Keys joined by +")
def press_keys(keys: str) -> str:
    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    if not parts:
        return "No keys given."
    _pyautogui().hotkey(*parts)
    return f"Pressed {'+'.join(parts)}."


@tool("Take a screenshot of the screen and save it as a PNG.")
def take_screenshot() -> str:
    folder = Path(STATE["data_dir"]) / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"shot_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
    _pyautogui().screenshot(str(path))
    return f"Saved screenshot to {path}."


@tool("Change the system volume.", action="up, down or mute", steps="How many steps (each is about 2%)")
def volume(action: str, steps: int = 5) -> str:
    keys = {"up": "volumeup", "down": "volumedown", "mute": "volumemute"}
    key = keys.get(action.strip().lower())
    if not key:
        return "Action must be up, down or mute."
    _pyautogui().press(key, presses=1 if key == "volumemute" else max(1, min(int(steps), 50)))
    return f"Volume {action}."


@tool("Control media playback.", action="playpause, next, previous or stop")
def media(action: str) -> str:
    keys = {"playpause": "playpause", "next": "nexttrack", "previous": "prevtrack", "stop": "stop"}
    key = keys.get(action.strip().lower())
    if not key:
        return "Action must be playpause, next, previous or stop."
    _pyautogui().press(key)
    return f"Media: {action}."


@tool("Read the text currently on the clipboard.")
def clipboard_get() -> str:
    import pyperclip

    return _clip(pyperclip.paste() or "(clipboard is empty)")


@tool("Put text on the clipboard.", text="Text to copy")
def clipboard_set(text: str) -> str:
    import pyperclip

    pyperclip.copy(text)
    return "Copied to clipboard."


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------
@tool("Report CPU, memory, disk and battery status of the PC.")
def system_status() -> str:
    import psutil

    anchor = Path.home().anchor or "/"
    lines = [
        f"CPU {psutil.cpu_percent(interval=0.5):.0f}%",
        f"Memory {psutil.virtual_memory().percent:.0f}% used",
        f"Disk {psutil.disk_usage(anchor).percent:.0f}% used",
    ]
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery:
        lines.append(f"Battery {battery.percent:.0f}% ({'charging' if battery.power_plugged else 'on battery'})")
    return ", ".join(lines)


@tool("Lock, sleep, restart or shut down the PC. Restart and shutdown ask the user first.",
      confirm=lambda a: str(a.get("action", "")).lower() in ("restart", "shutdown"),
      action="lock, sleep, restart, shutdown or cancel (cancels a pending shutdown)")
def power(action: str) -> str:
    if not IS_WINDOWS:
        return "Power actions are only implemented for Windows."
    cmds = {
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "restart": "shutdown /r /t 10",
        "shutdown": "shutdown /s /t 10",
        "cancel": "shutdown /a",
    }
    cmd = cmds.get(action.strip().lower())
    if not cmd:
        return "Action must be lock, sleep, restart, shutdown or cancel."
    subprocess.Popen(cmd, shell=True)
    return f"Done: {action}."


# --------------------------------------------------------------------------
# Tasks, reminders, briefing
# --------------------------------------------------------------------------
def _fmt_task(t: dict) -> str:
    bits = [t["priority"]] if t["priority"] != "normal" else []
    if t["due"]:
        bits.insert(0, f"due {t['due']}")
    if t["remind"]:
        bits.append("reminder")
    extra = f" ({', '.join(bits)})" if bits else ""
    return f"[{t['id']}] {'DONE ' if t['done'] else ''}{t['title']}{extra}"


@tool("Add a task to the to-do list.", title="What needs doing",
      due="Optional deadline as YYYY-MM-DD or YYYY-MM-DD HH:MM",
      priority=f"One of {', '.join(PRIORITIES)}", remind="Announce it by voice when due")
def add_task(title: str, due: str = "", priority: str = "normal", remind: bool = False) -> str:
    return "Added " + _fmt_task(_tasks().add(title, due, priority, remind))


@tool("List the to-do list.", include_done="Also show finished tasks")
def list_tasks(include_done: bool = False) -> str:
    tasks = _tasks().list(include_done)
    return "\n".join(_fmt_task(t) for t in tasks) if tasks else "The to-do list is empty."


@tool("Mark a task as done.", task="Task id number or part of its title")
def complete_task(task: str) -> str:
    return "Completed " + _fmt_task(_tasks().complete(task))


@tool("Delete a task from the list.", task="Task id number or part of its title")
def delete_task(task: str) -> str:
    return "Deleted " + _fmt_task(_tasks().delete(task))


@tool("Set a spoken reminder, either in N minutes or at an exact time.",
      text="What to remind about", in_minutes="Minutes from now (use this OR at)",
      at="Exact time as YYYY-MM-DD HH:MM")
def set_reminder(text: str, in_minutes: int = 0, at: str = "") -> str:
    if in_minutes and int(in_minutes) > 0:
        due = (dt.datetime.now() + dt.timedelta(minutes=int(in_minutes))).strftime("%Y-%m-%d %H:%M")
    elif at:
        due = at
    else:
        return "Tell me when: give in_minutes or an exact time."
    return "Reminder set. " + _fmt_task(_tasks().add(text, due, "normal", remind=True))


@tool("Summarise the user's day: current time, open tasks, overdue and due-today items.")
def daily_briefing() -> str:
    return f"It is {current_time()}. {_tasks().summary()}"
