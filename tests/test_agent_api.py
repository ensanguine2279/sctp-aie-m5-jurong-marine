"""Tests for scripts/agent_api.py. The service (AgentService) is plain Python, so most of
these need neither FastAPI nor a server: they call it with the real agent_app.ask(), a
scripted model, stubbed tools and an in-memory stand-in for the session store. The last few
drive the FastAPI routes and run only where FastAPI (and httpx, for its TestClient) is
installed: `uv add fastapi uvicorn httpx` / `pip install fastapi uvicorn httpx`.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

import agent_api as api
import agent_app as app
import sessions
from test_agent_graph import model_says, result

KEY = "k" * 32


class MemStore:
    """Stands in for sessions.SessionStore: load / save / recent, never raising, keeping
    the messages without the system prompt (restored() puts a fresh one back)."""

    def __init__(self, down: bool = False):
        self.rows: dict[str, dict] = {}
        self.down = down
        self.saves = 0

    def load(self, sid):
        if self.down:
            return SimpleNamespace(ok=False, found=False, messages=[], display=[],
                                   error="store is down")
        r = self.rows.get(sid)
        return SimpleNamespace(ok=True, found=r is not None,
                               messages=list(r["messages"]) if r else [],
                               display=list(r["display"]) if r else [], error=None)

    def save(self, sid, messages, display):
        if self.down:
            return False, "store is down"
        self.saves += 1
        self.rows[sid] = {"messages": [m for m in messages if m.get("role") != "system"],
                          "display": display, "at": time.time()}
        return True, None

    def recent(self, n):
        if self.down:
            return None
        rows = sorted(self.rows.items(), key=lambda kv: -kv[1]["at"])[:n]
        return [SimpleNamespace(sid=sid, title=r["display"][0]["text"],
                                turns=sum(1 for m in r["display"] if m["role"] == "user"),
                                updated_at=None) for sid, r in rows]


@pytest.fixture
def scripted(monkeypatch):
    """Make the model and tools answer as told: scripted("Hello.") or
    scripted([("c1", "list_machines", "{}")], "Twelve.", sources=[...])."""
    def _set(*replies, sources=()):
        monkeypatch.setattr(app, "complete", model_says(*replies))
        monkeypatch.setattr(app.at, "run_tool",
                            lambda db, name, args: result(sources=[{"label": s} for s in sources]))
    return _set


def service(store=None, **kw):
    return api.AgentService(None, None, store, None if store else "not set up", api_key=KEY,
                            log_to_file=False, **kw)


# ------------------------------------------------------------------- access ---

def test_the_key_is_required_and_compared_exactly():
    svc = service()
    assert svc.authorised(KEY)
    for wrong in (None, "", KEY[:-1], KEY + "x", KEY.upper()):
        assert not svc.authorised(wrong)


def test_no_key_no_service():
    with pytest.raises(ValueError):
        api.AgentService(None, None, api_key="")


# ------------------------------------------------------------------- health ---

def test_health_says_whether_it_can_answer():
    r = service(MemStore()).health()
    assert r.status == 200 and r.body["guard_ready"] is True
    assert r.body["agent"] == "langgraph" and r.body["sessions"] == "on"
    assert service().health().body["sessions"].startswith("off")


# ---------------------------------------------------------------------- ask ---

@pytest.mark.parametrize("question,code", [("", "bad_question"), ("   ", "bad_question"),
                                           (None, "bad_question"), (42, "bad_question"),
                                           ("x" * (api.MAX_QUESTION + 1), "question_too_long")])
def test_a_bad_question_is_refused_unasked(question, code):
    called = []
    svc = service(ask_fn=lambda *a, **k: called.append(1))
    r = svc.ask(question)
    assert (r.status, r.body["error"]["code"]) == (422, code) and called == []


def test_a_bad_session_id_is_refused_unasked():
    called = []
    svc = service(MemStore(), ask_fn=lambda *a, **k: called.append(1))
    for sid in ("abc", "g" * 32, "../etc/passwd"):
        r = svc.ask("hello", sid)
        assert (r.status, r.body["error"]["code"]) == (422, "bad_session_id")
    assert called == []


def test_a_question_starts_a_saved_conversation(scripted):
    scripted([("c1", "list_machines", "{}")], "Twelve machines.", sources=["CMMS machines"])
    store = MemStore()
    r = service(store).ask("how many machines?")
    assert r.status == 200
    b = r.body
    assert b["answer"] == "Twelve machines." and b["sources"] == ["CMMS machines"]
    assert b["saved"] is True and sessions.valid_id(b["session_id"]) and b["turns"] == 1
    assert b["refused"] is None and b["answer_replaced"] is None
    assert b["usage"]["model_calls"] == 2
    assert [row["step"] for row in b["trace"]] == ["model, round 1", "tool, round 1",
                                                   "model, round 2"]
    saved = store.rows[b["session_id"]]
    assert [m["role"] for m in saved["display"]] == ["user", "assistant"]
    assert saved["messages"][0] == {"role": "user", "content": "how many machines?"}


def test_a_follow_up_continues_the_conversation(scripted, monkeypatch):
    store = MemStore()
    svc = service(store)
    scripted("Three are open.")           # no digits: facts with no tool call are re-asked
    sid = svc.ask("open work orders on CNC-02?").body["session_id"]
    seen = []

    def complete(client, model, messages, tools):
        seen.append([m.get("content") for m in messages if m.get("role") == "user"])
        return model_says("And one on that machine.")(client, model, messages, tools)
    monkeypatch.setattr(app, "complete", complete)
    r = svc.ask("and CNC-03?", sid)
    assert r.status == 200 and r.body["session_id"] == sid and r.body["turns"] == 2
    assert seen[0] == ["open work orders on CNC-02?", "and CNC-03?"]
    assert len(store.rows[sid]["display"]) == 4


def test_a_refused_question_is_stored_only_as_its_placeholder(scripted):
    scripted(AssertionError("the model was called"))
    store = MemStore()
    r = service(store).ask("What is the name of employee EMP015?")
    assert r.status == 200 and r.body["refused"] == "pdpa"
    assert r.body["usage"]["model_calls"] == 0
    assert "EMP015" not in json.dumps(store.rows[r.body["session_id"]])


def test_an_answer_with_personal_data_is_reported_as_replaced(scripted):
    scripted("Mail a.tan.jme@example.com.")
    r = service(MemStore()).ask("who handles CNC-02?")
    assert r.body["answer_replaced"] == "pii" and "@" not in r.body["answer"]   # the guard's output rule


def test_unknown_and_unreadable_conversations(scripted):
    scripted("x")
    sid = sessions.new_session_id()
    r = service(MemStore()).ask("hello", sid)
    assert (r.status, r.body["error"]["code"]) == (404, "unknown_session")
    r = service(MemStore(down=True)).ask("hello", sid)
    assert (r.status, r.body["error"]["code"]) == (503, "store_down")
    r = service(None).ask("hello", sid)
    assert (r.status, r.body["error"]["code"]) == (503, "sessions_off")


def test_without_a_store_a_question_is_still_answered_but_not_saved(scripted):
    scripted("Hello.")
    r = service(None).ask("hello")
    assert r.status == 200 and r.body["answer"] == "Hello."
    assert r.body["saved"] is False and r.body["session_id"] is None


def test_a_save_that_fails_is_reported_not_hidden(scripted):
    scripted("Hello.")
    store = MemStore()
    store.save = lambda *a: (False, "disk full")
    r = service(store).ask("hello")
    assert r.status == 200 and r.body["saved"] is False and r.body["session_id"] is None
    assert r.body["save_error"] == "disk full"


def test_a_bug_is_a_500_with_no_trace():
    def boom(*a, **k):
        raise KeyError("secret internals")
    r = service(MemStore(), ask_fn=boom).ask("hello")
    assert (r.status, r.body["error"]["code"]) == (500, "internal")
    assert "secret" not in json.dumps(r.body)


def test_two_questions_on_one_conversation_at_once_are_taken_in_turn(scripted, monkeypatch):
    store = MemStore()
    svc = service(store)
    scripted("First.")
    sid = svc.ask("first").body["session_id"]

    def slow(client, model, messages, tools):
        time.sleep(0.05)
        return model_says("ok")(client, model, messages, tools)
    monkeypatch.setattr(app, "complete", slow)
    threads = [threading.Thread(target=svc.ask, args=(q, sid)) for q in ("second", "third")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    users = [m["text"] for m in store.rows[sid]["display"] if m["role"] == "user"]
    assert users[0] == "first" and sorted(users[1:]) == ["second", "third"]   # none lost


# -------------------------------------------------------------- conversations ---

def test_reading_one_conversation_back(scripted):
    store = MemStore()
    svc = service(store)
    scripted("Twelve.")
    sid = svc.ask("how many machines?").body["session_id"]
    r = svc.session(sid)
    assert r.status == 200 and r.body["turns"] == 1
    assert [m["role"] for m in r.body["messages"]] == ["user", "assistant"]
    assert r.body["spend"]["line"].startswith("This conversation:")
    assert svc.session("nope").status == 422
    assert svc.session(sessions.new_session_id()).status == 404


def test_listing_recent_conversations(scripted):
    store = MemStore()
    svc = service(store)
    scripted("ok")
    for q in ("one", "two", "three"):
        svc.ask(q)
        time.sleep(0.01)
    r = svc.recent(2)
    assert r.status == 200 and [s["title"] for s in r.body["sessions"]] == ["three", "two"]
    assert svc.recent(0).status == 200                          # clamped up to 1
    assert svc.recent("lots").status == 422
    assert service(None).recent().status == 503
    assert service(MemStore(down=True)).recent().status == 503


# ------------------------------------------------------------------ streaming ---

def test_streaming_sends_each_step_then_the_answer(scripted):
    scripted([("c1", "list_machines", "{}")], "Twelve machines.", sources=["CMMS machines"])
    store = MemStore()
    events = list(service(store).ask_stream("how many machines?"))
    kinds = [e["event"] for e in events]
    assert kinds == ["step"] * 5 + ["answer"]
    assert [e["data"]["node"] for e in events[:-1]] == ["guard_in", "model", "tools", "model",
                                                       "guard_out"]
    final = events[-1]["data"]
    assert final["answer"] == "Twelve machines." and final["saved"] is True
    assert final["session_id"] in store.rows
    assert "machines?" not in json.dumps([e["data"] for e in events[:-1]])   # no text in steps


def test_streaming_a_refused_question_sends_one_step_then_the_answer(scripted):
    scripted(AssertionError("the model was called"))
    events = list(service(MemStore()).ask_stream("What is the name of employee EMP015?"))
    assert [e["event"] for e in events] == ["step", "answer"]
    assert events[0]["data"]["passed"] is False and events[1]["data"]["refused"] == "pdpa"


def test_streaming_a_bad_request_sends_one_error():
    events = list(service(MemStore()).ask_stream("", None))
    assert events == [{"event": "error", "data": {"status": 422, "error": {
        "code": "bad_question", "message": "question must be non-empty text."}}}]


def test_streaming_gives_up_waiting_but_the_question_still_finishes(scripted, monkeypatch):
    store = MemStore()
    done = threading.Event()

    def slow(client, model, messages, tools):
        time.sleep(0.3)
        done.set()
        return model_says("Late.")(client, model, messages, tools)
    scripted("unused")
    monkeypatch.setattr(app, "complete", slow)
    events = list(service(store).ask_stream("hello", timeout=0.05))
    assert events[-1]["event"] == "error" and events[-1]["data"]["status"] == 504
    assert done.wait(2)
    for _ in range(100):                                   # saved once it finishes
        if store.saves:
            break
        time.sleep(0.01)
    assert store.saves == 1


def test_an_event_is_written_as_server_sent_events_expect():
    out = api.sse({"event": "step", "data": {"node": "model", "round": 1}})
    assert out == 'event: step\ndata: {"node": "model", "round": 1}\n\n'


# --------------------------------------------------------------- HTTP routes ---

@pytest.fixture
def client(scripted):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(api.create_app(service(MemStore())))


def test_routes_need_the_key_except_health(client):
    assert client.get("/health").status_code == 200
    for method, path, body in (("post", "/ask", {"question": "hi"}),
                               ("post", "/ask/stream", {"question": "hi"}),
                               ("get", "/sessions", None),
                               ("get", f"/sessions/{'a' * 32}", None)):
        r = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorised"


def test_ask_over_http(client, scripted):
    scripted("Hello.")
    r = client.post("/ask", json={"question": "hello"}, headers={api.KEY_HEADER: KEY})
    assert r.status_code == 200 and r.json()["answer"] == "Hello."
    sid = r.json()["session_id"]
    r = client.get(f"/sessions/{sid}", headers={api.KEY_HEADER: KEY})
    assert r.status_code == 200 and r.json()["turns"] == 1


def test_ask_stream_over_http(client, scripted):
    scripted([("c1", "list_machines", "{}")], "Twelve.")
    r = client.post("/ask/stream", json={"question": "how many?"},
                    headers={api.KEY_HEADER: KEY})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    kinds = [line.split(": ", 1)[1] for line in r.text.splitlines() if line.startswith("event:")]
    assert kinds == ["step"] * 5 + ["answer"]