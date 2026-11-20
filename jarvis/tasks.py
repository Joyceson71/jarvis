"""Persistent task + reminder store (JSON file, thread-safe)."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

DATE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)
PRIORITIES = ("low", "normal", "high")
_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


def parse_due(value: str | None) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date/time '{value}'. Use YYYY-MM-DD or YYYY-MM-DD HH:MM.")


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                data.setdefault("next_id", len(data["tasks"]) + 1)
                return data
        except (OSError, ValueError):
            pass
        return {"next_id": 1, "tasks": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- operations --------------------------------------------------
    def add(self, title: str, due: str = "", priority: str = "normal", remind: bool = False) -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("A task needs a title.")
        if priority not in PRIORITIES:
            priority = "normal"
        parsed = parse_due(due)
        due_text = ""
        if parsed:
            due_text = parsed.strftime("%Y-%m-%d %H:%M") if len(due.strip()) > 10 else parsed.strftime("%Y-%m-%d")
        with self._lock:
            task = {
                "id": self._data["next_id"],
                "title": title,
                "due": due_text,
                "priority": priority,
                "remind": bool(remind and parsed),
                "notified": False,
                "done": False,
                "created": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._data["next_id"] += 1
            self._data["tasks"].append(task)
            self._save()
            return dict(task)

    def list(self, include_done: bool = False) -> list:
        with self._lock:
            tasks = [dict(t) for t in self._data["tasks"] if include_done or not t["done"]]

        def key(t):
            return (t["done"], t["due"] == "", t["due"], _PRIORITY_RANK.get(t["priority"], 1), t["id"])

        return sorted(tasks, key=key)

    def _resolve(self, ref) -> dict:
        ref = str(ref).strip()
        open_tasks = [t for t in self._data["tasks"] if not t["done"]]
        if ref.isdigit():
            for t in self._data["tasks"]:
                if t["id"] == int(ref):
                    return t
            raise LookupError(f"No task with id {ref}.")
        matches = [t for t in open_tasks if ref.lower() in t["title"].lower()]
        if not matches:
            raise LookupError(f"No open task matches '{ref}'.")
        if len(matches) > 1:
            names = "; ".join(f"[{t['id']}] {t['title']}" for t in matches[:5])
            raise LookupError(f"'{ref}' matches several tasks: {names}. Use the id.")
        return matches[0]

    def complete(self, ref) -> dict:
        with self._lock:
            task = self._resolve(ref)
            task["done"] = True
            self._save()
            return dict(task)

    def delete(self, ref) -> dict:
        with self._lock:
            task = self._resolve(ref)
            self._data["tasks"].remove(task)
            self._save()
            return dict(task)

    def due_reminders(self, now: dt.datetime | None = None) -> list:
        """Return reminders that are due and not yet announced (and mark them announced)."""
        now = now or dt.datetime.now()
        fired = []
        with self._lock:
            for t in self._data["tasks"]:
                if t["done"] or not t["remind"] or t["notified"] or not t["due"]:
                    continue
                when = parse_due(t["due"])
                if len(t["due"]) <= 10:  # date only -> 9am that day
                    when = when.replace(hour=9, minute=0)
                if when <= now:
                    t["notified"] = True
                    fired.append(dict(t))
            if fired:
                self._save()
        return fired

    def summary(self, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now()
        open_tasks = self.list()
        if not open_tasks:
            return "No open tasks."
        overdue, today = [], []
        for t in open_tasks:
            if not t["due"]:
                continue
            when = parse_due(t["due"])
            timed = len(t["due"]) > 10
            if when.date() < now.date() or (timed and when < now):
                overdue.append(t)
            elif when.date() == now.date():
                today.append(t)
        parts = [f"{len(open_tasks)} open task{'s' if len(open_tasks) != 1 else ''}."]
        if overdue:
            parts.append("Overdue: " + "; ".join(t["title"] for t in overdue) + ".")
        if today:
            parts.append("Due today: " + "; ".join(t["title"] for t in today) + ".")
        high = [t["title"] for t in open_tasks if t["priority"] == "high"]
        if high:
            parts.append("High priority: " + "; ".join(high) + ".")
        return " ".join(parts)
