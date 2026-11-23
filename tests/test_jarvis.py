import datetime as dt
import inspect
import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import tools  # noqa: E402
from jarvis.api_server import create_server  # noqa: E402
from jarvis.brain import Brain  # noqa: E402
from jarvis.tasks import TaskStore  # noqa: E402
from jarvis.voice import clean_for_speech, strip_wake  # noqa: E402


def make_settings(**kw):
    base = dict(provider="groq", base_url="http://x", model="m", api_key="k", user_name="Boss",
                wake_words=["jarvis"], voice_rate=180, needs_key=True)
    base.update(kw)
    return SimpleNamespace(**base)


class FakeResp:
    def __init__(self, message, status=200):
        self.status_code, self._m, self.headers, self.text = status, message, {}, "err"

    def json(self):
        return {"choices": [{"message": self._m}]}


def tool_call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def scripted_brain(responses, **kw):
    b = Brain(make_settings(**kw))
    queue = list(responses)
    b._post = lambda *a, **k: queue.pop(0)
    b._sleep = lambda s: None
    return b


# ---- wake word / speech ----------------------------------------------------
def test_wake_word():
    assert strip_wake("Jarvis open chrome", ["jarvis"]) == "open chrome"
    assert strip_wake("hey jarvis, what time is it?", ["jarvis"]) == "what time is it"
    assert strip_wake("jarvis", ["jarvis"]) == ""
    assert strip_wake("open chrome", ["jarvis"]) is None
    assert strip_wake("jarvisnotawake", ["jarvis"]) is None


def test_clean_for_speech():
    assert clean_for_speech("see **this** https://a.com/x?y=1 ok") == "see this the link ok"


# ---- tasks -----------------------------------------------------------------
def test_tasks_lifecycle(tmp_path):
    s = TaskStore(tmp_path / "t.json")
    a = s.add("Buy milk", "2026-11-21", "high")
    b = s.add("Call mom", "2026-11-20 18:00", remind=True)
    s.add("Someday", "")
    assert [t["title"] for t in s.list()] == ["Call mom", "Buy milk", "Someday"]
    assert s.complete("milk")["done"] is True
    assert [t["title"] for t in s.list()] == ["Call mom", "Someday"]
    assert len(s.list(include_done=True)) == 3
    assert TaskStore(tmp_path / "t.json").list()[0]["id"] == b["id"]  # persisted
    s.delete(str(b["id"]))
    assert all(t["title"] != "Call mom" for t in s.list())
    assert a["priority"] == "high"


def test_tasks_validation_and_ambiguity(tmp_path):
    s = TaskStore(tmp_path / "t.json")
    for bad in ("", "   "):
        try:
            s.add(bad)
            assert False
        except ValueError:
            pass
    try:
        s.add("x", "next tuesday")
        assert False
    except ValueError:
        pass
    s.add("Email Bob")
    s.add("Email Alice")
    try:
        s.complete("email")
        assert False
    except LookupError as e:
        assert "several" in str(e)


def test_reminders_fire_once(tmp_path):
    s = TaskStore(tmp_path / "t.json")
    s.add("Stretch", "2026-11-20 10:00", remind=True)
    s.add("Later", "2026-11-20 23:00", remind=True)
    assert s.due_reminders(dt.datetime(2026, 11, 20, 9, 0)) == []
    fired = s.due_reminders(dt.datetime(2026, 11, 20, 10, 1))
    assert [t["title"] for t in fired] == ["Stretch"]
    assert s.due_reminders(dt.datetime(2026, 11, 20, 10, 2)) == []


def test_summary(tmp_path):
    s = TaskStore(tmp_path / "t.json")
    assert s.summary() == "No open tasks."
    s.add("Old", "2026-11-18")
    s.add("Today", "2026-11-20", "high")
    out = s.summary(dt.datetime(2026, 11, 20, 8, 0))
    assert "Overdue: Old" in out and "Due today: Today" in out and "High priority: Today" in out


# ---- tools -----------------------------------------------------------------
def test_tool_schemas():
    specs = {s["function"]["name"]: s["function"] for s in tools.tool_specs()}
    assert {"open_app", "web_search", "add_task", "run_shell", "set_reminder", "system_health"} <= set(specs)
    p = specs["add_task"]["parameters"]
    assert p["required"] == ["title"] and p["properties"]["remind"]["type"] == "boolean"
    assert specs["set_reminder"]["parameters"]["properties"]["in_minutes"]["type"] == "integer"


def test_tool_errors_are_contained():
    assert "Unknown tool" in tools.call_tool("nope", {})
    assert "Bad arguments" in tools.call_tool("open_website", {})
    assert "unsafe" in tools.open_app("chrome & calc").lower()
    try:
        tools._normalise_url("file:///etc/passwd")
        assert False
    except ValueError:
        pass
    assert tools._normalise_url("youtube.com") == "https://youtube.com"


def test_shell_confirmation_rules():
    t = tools.REGISTRY["run_shell"]
    assert not t.needs_confirm({"command": "Get-Date"})
    assert t.needs_confirm({"command": "Remove-Item C:\\x -Recurse"})
    assert t.needs_confirm({"command": "echo hi > a.txt"})
    assert tools.REGISTRY["power"].needs_confirm({"action": "shutdown"})
    assert not tools.REGISTRY["power"].needs_confirm({"action": "lock"})


def test_ddg_parse_and_html_text():
    html = ('<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.com%2Fa&rut=1">Hi</a>'
            '<a class="result__snippet">Snippet</a></div>')
    r = tools._parse_ddg(html, 5)
    assert r == [{"title": "Hi", "url": "https://ex.com/a", "snippet": "Snippet"}]
    page = "<html><script>x=1</script><p class=price>Rs 5</p><p>other</p></html>"
    assert tools._html_to_text(page, ".price") == "Rs 5"
    assert "x=1" not in tools._html_to_text(page)


# ---- brain -----------------------------------------------------------------
def test_brain_plain_reply():
    b = scripted_brain([FakeResp({"content": "Hello Boss."})])
    assert b.handle("hi") == "Hello Boss."


def test_brain_tool_loop(tmp_path):
    tools.init(TaskStore(tmp_path / "t.json"), tmp_path)
    b = scripted_brain([
        FakeResp({"content": None, "tool_calls": [tool_call("add_task", {"title": "Buy milk", "priority": "high"})]}),
        FakeResp({"content": "Added it."}),
    ])
    assert b.handle("add buy milk") == "Added it."
    assert tools._tasks().list()[0]["title"] == "Buy milk"
    assert [m["role"] for m in b.history] == ["user", "assistant", "tool", "assistant"]


def test_brain_confirmation_declined_and_approved(tmp_path):
    tools.init(TaskStore(tmp_path / "t.json"), tmp_path)
    ran = []
    original = tools.REGISTRY["run_shell"].fn
    tools.REGISTRY["run_shell"].fn = lambda command: ran.append(command) or "ok"
    try:
        call = tool_call("run_shell", {"command": "Remove-Item foo"})
        b = scripted_brain([FakeResp({"content": "", "tool_calls": [call]}), FakeResp({"content": "Cancelled."})])
        b.handle("delete foo", confirm=lambda q: False)
        assert ran == [] and "NOT performed" in b.history[2]["content"]
        b = scripted_brain([FakeResp({"content": "", "tool_calls": [call]}), FakeResp({"content": "Done."})])
        b.handle("delete foo", confirm=lambda q: True)
        assert ran == ["Remove-Item foo"]
    finally:
        tools.REGISTRY["run_shell"].fn = original


def test_brain_retry_and_failure():
    b = scripted_brain([FakeResp({}, 429), FakeResp({"content": "Back."})])
    assert b.handle("hi") == "Back."
    b = scripted_brain([FakeResp({}, 400)])
    out = b.handle("hi")
    assert "can't reach my brain" in out and b.history == []  # history rolled back


def test_brain_missing_key_and_bad_args():
    assert "API key" in Brain(make_settings(api_key="")).handle("hi")
    b = scripted_brain([
        FakeResp({"content": "", "tool_calls": [{"id": "1", "function": {"name": "current_time", "arguments": "{oops"}}]}),
        FakeResp({"content": "Sorry."}),
    ])
    assert b.handle("time") == "Sorry." and "Could not parse" in b.history[2]["content"]


def test_brain_step_limit():
    looping = FakeResp({"content": "", "tool_calls": [tool_call("current_time", {})]})
    b = scripted_brain([looping] * 8)
    assert "circles" in b.handle("loop")


# ---- phone API -------------------------------------------------------------
def test_api(tmp_path):
    tasks = TaskStore(tmp_path / "t.json")
    tools.init(tasks, tmp_path)
    brain = scripted_brain([FakeResp({"content": "At your service."})])
    server = create_server(brain, tasks, "127.0.0.1", 0, "secret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, method="GET", body=None, token="secret"):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        health_code, health_data = call("/health", token=None)
        assert health_code == 200
        assert health_data["ok"] is True
        assert health_data["name"] == "jarvis"
        assert "version" in health_data
        assert health_data["status"] == "healthy"
        assert call("/tasks", token=None)[0] == 401
        assert call("/tasks", token="wrong")[0] == 401
        code, data = call("/tasks", "POST", {"title": "From phone"})
        assert code == 201 and data["task"]["title"] == "From phone"
        assert call("/tasks")[1]["tasks"][0]["title"] == "From phone"
        assert call(f"/tasks/{data['task']['id']}/complete", "POST", {})[1]["task"]["done"] is True
        assert call("/tasks", "POST", {"title": ""})[0] == 400
        assert call("/command", "POST", {"text": "hello"})[1]["reply"] == "At your service."
        assert call("/command", "POST", {})[0] == 400
        assert call("/nothing")[0] == 404
    finally:
        server.shutdown()


def test_health_and_version():
    from jarvis import __version__
    from jarvis.api_server import get_health_status
    health = get_health_status()
    assert health["ok"] is True
    assert health["version"] == __version__
    assert health["status"] == "healthy"
    assert "healthy" in tools.call_tool("system_health", {})


def test_main_test_flag():
    from jarvis.main import main
    assert main(["--test"]) == 0


_RUNNING_TESTS = False


def run_tests() -> bool:
    """Run all test functions and return True if all pass, False otherwise."""
    global _RUNNING_TESTS
    if _RUNNING_TESTS:
        return True
    _RUNNING_TESTS = True
    try:
        test_funcs = [
            (name, func)
            for name, func in sorted(globals().items())
            if name.startswith("test_") and callable(func)
        ]
        print(f"Running {len(test_funcs)} test functions...", flush=True)
        failed = []
        for name, func in test_funcs:
            sig = inspect.signature(func)
            try:
                if "tmp_path" in sig.parameters:
                    with tempfile.TemporaryDirectory() as td:
                        func(Path(td))
                else:
                    func()
                print(f"  [PASS] {name}", flush=True)
            except Exception as e:
                failed.append((name, e))
                print(f"  [FAIL] {name}: {e}", flush=True)

        if failed:
            print(f"\n{len(failed)} test(s) failed out of {len(test_funcs)}.", flush=True)
            return False
        print(f"\nAll {len(test_funcs)} tests passed successfully!", flush=True)
        return True
    finally:
        _RUNNING_TESTS = False


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
