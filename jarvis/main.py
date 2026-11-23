"""Jarvis entry point: wake word loop, text mode, reminders, phone API."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import logging.handlers
import socket
import sys
import threading
import time

from . import tools
from .brain import Brain
from .config import Settings
from .tasks import TaskStore
from .voice import strip_wake

log = logging.getLogger("jarvis")

STOP_PHRASES = ("quit jarvis", "exit jarvis", "shut down jarvis", "goodbye jarvis", "stop jarvis", "go to sleep jarvis")
FOLLOW_UP_SECONDS = 10  # keep listening without the wake word after a reply


def setup_logging(settings: Settings, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        settings.data_dir / "jarvis.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr and sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


def acquire_single_instance(port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def greeting(settings: Settings, tasks: TaskStore) -> str:
    hour = dt.datetime.now().hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    text = f"Good {part}, {settings.user_name}. Jarvis online."
    n = len(tasks.list())
    if n:
        text += f" You have {n} open task{'s' if n != 1 else ''}."
    return text


def reminder_loop(tasks: TaskStore, say) -> None:
    while True:
        try:
            for t in tasks.due_reminders():
                say(f"Reminder: {t['title']}")
        except Exception:
            log.exception("reminder loop error")
        time.sleep(15)


def run_text(brain: Brain, settings: Settings, tasks: TaskStore) -> None:
    def confirm(question: str) -> bool:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")

    print(greeting(settings, tasks))
    print("(text mode - type 'quit' to exit)")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            return
        print("jarvis>", brain.handle(line, confirm=confirm))


def run_voice(voice, brain: Brain, settings: Settings, tasks: TaskStore) -> None:
    voice.speak(greeting(settings, tasks))
    follow_up_until = 0.0
    while True:
        try:
            in_conversation = time.time() < follow_up_until
            heard = voice.listen(timeout=6 if in_conversation else 4, phrase_limit=15)
            if not heard:
                continue
            if in_conversation:
                after = strip_wake(heard, settings.wake_words)
                command = after if after else heard
            else:
                command = strip_wake(heard, settings.wake_words)
                if command is None:
                    continue
                if not command:
                    voice.speak("Yes?")
                    command = voice.listen(timeout=6, phrase_limit=15)
                    if not command:
                        continue
            print(f"you> {command}")
            if any(p in command.lower() or p in heard.lower() for p in STOP_PHRASES):
                voice.speak("Goodbye. Signing off.")
                return
            reply = brain.handle(command, confirm=voice.confirm)
            voice.speak(reply)
            follow_up_until = time.time() + FOLLOW_UP_SECONDS
        except KeyboardInterrupt:
            return
        except Exception:
            log.exception("voice loop error")
            time.sleep(1)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis personal assistant")
    parser.add_argument("--text", action="store_true", help="type instead of speaking (no microphone)")
    parser.add_argument("--pair", action="store_true", help="show the address and token for the phone app")
    parser.add_argument("--no-api", action="store_true", help="don't start the phone API")
    parser.add_argument("--test", action="store_true", help="run the automated test suite and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.test:
        from tests.test_jarvis import run_tests
        return 0 if run_tests() else 1

    settings = Settings.load()

    if args.pair:
        print(f"Address: http://{lan_ip()}:{settings.api_port}")
        print(f"Token:   {settings.api_token or '(API disabled in .env)'}")
        return 0

    setup_logging(settings, args.verbose)

    lock = acquire_single_instance(settings.lock_port)
    if lock is None:
        print("Jarvis is already running.")
        return 0

    tasks = TaskStore(settings.data_dir / "tasks.json")
    tools.init(tasks, settings.data_dir)
    brain = Brain(settings)

    voice = None
    if not args.text:
        try:
            from .voice import Voice

            voice = Voice(settings)
        except Exception as e:  # missing mic, missing packages...
            log.warning("voice unavailable: %s", e)
            print(f"Voice mode unavailable ({e}). Falling back to text mode.")

    if settings.api_enabled and not args.no_api:
        try:
            from .api_server import start_in_thread

            start_in_thread(brain, tasks, settings.api_host, settings.api_port, settings.api_token)
        except OSError as e:
            log.warning("phone API not started: %s", e)

    say = voice.speak if voice else (lambda t: print(f"jarvis> {t}"))
    threading.Thread(target=reminder_loop, args=(tasks, say), name="reminders", daemon=True).start()

    if voice:
        run_voice(voice, brain, settings, tasks)
    else:
        run_text(brain, settings, tasks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
