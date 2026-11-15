"""Tests for scripts/agent_api.py. The service (AgentService) is plain Python, so most of
these need neither FastAPI nor a server: they call it with the real agent graph, a scripted
model, stubbed tools, and real conversations (conversations.py) on SQLite with LangGraph's
in-memory checkpointer in place of Postgres. The last few drive the FastAPI routes and run
only where FastAPI (and httpx, for its TestClient) is installed:
`uv add fastapi uvicorn httpx` / `pip install fastapi uvicorn httpx`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

import agent_api as api
import agent_app as app
import conversations as conv
from test_agent_graph import model_says, result

KEY = "k" * 32


def conversations(down: bool = False):
    """Real Conversations: the threads table on SQLite, the in-memory checkpointer."""
    from langgraph.checkpoint.memory import InMemorySaver
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.execute(conv.SQLITE_DDL)
    run = conv.sqlite_run(db)
    if down:
        def run(sql, params=None):                      # noqa: F811
            raise ConnectionError("can't reach Postgres at db.internal:5432")
    c = conv.Conversations(conv.ThreadStore(run), InMemorySaver())
    c.sqlite = db
    return c


@pytest.fixture
def scripted(monkeypatch):
    """Make the model and tools answer as told: scripted("Hello.") or
    scripted([("c1", "list_machines", "{}")], "Twelve.", sources=[...])."""
    def _set(*replies, sources=()):
        monkeypatch.setattr(app, "complete", model_says(*replies))
        monkeypatch.setattr(app.at, "run_tool",
                            lambda db, name, args: result(sources=[{"label": s} for s in sources]))
    return _set


def service(convs=None, **kw):
    return api.AgentService(None, None, convs, None if convs else "not set up", api_key=KEY,
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

def test_health_says_whether_it_can_answer_and_where_conversations_go():
    r = service(conversations()).health()
    assert r.status == 200 and r.body["guard_ready"] is True
    assert r.body["agent"] == "langgraph" and r.body["sessions"] == "on"
    assert r.body["store"] == "postgres"
    off = service().health().body
    assert off["sessions"].startswith("off") and off["store"] is None


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
    svc = service(conversations(), ask_fn=lambda *a, **k: called.append(1))
    for sid in ("abc", "g" * 32, "../etc/passwd", "A" * 32):
        r = svc.ask("hello", sid)
        assert (r.status, r.body["error"]["code"]) == (422, "bad_session_id")
    assert called == []


def test_a_question_starts_a_saved_conversation(scripted):
    scripted([("c1", "list_machines", "{}")], "Twelve machines.", sources=["CMMS machines"])
    convs = conversations()
    r = service(convs).ask("how many machines?")
    assert r.status == 200
    b = r.body
    assert b["answer"] == "Twelve machines." and b["sources"] == ["CMMS machines"]
    assert b["saved"] is True and conv.valid_id(b["session_id"]) and b["turns"] == 1
    assert b["refused"] is None and b["answer_replaced"] is None
    assert b["usage"]["model_calls"] == 2
    assert [row["step"] for row in b["trace"]] == ["model, round 1", "tool, round 1",
                                                   "model, round 2"]
    t = convs.threads.get(b["session_id"])
    assert t.found and t.turns == 1 and t.title == "how many machines?"
    assert [m["role"] for m in t.display] == ["user", "assistant"]
    saved = convs.messages(b["session_id"])                        # the checkpointer's copy
    assert [m["role"] for m in saved] == ["system", "user", "assistant", "tool", "assistant"]


def test_a_follow_up_continues_the_conversation_from_the_checkpointer(scripted, monkeypatch):
    convs = conversations()
    svc = service(convs)
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
    assert len(convs.threads.get(sid).display) == 4 and convs.threads.get(sid).turns == 2


def test_a_restart_keeps_the_conversation(scripted, monkeypatch):
    """A new service (a new process) over the same database and checkpointer carries on."""
    convs = conversations()
    scripted("Three are open.")
    sid = service(convs).ask("open work orders on CNC-02?").body["session_id"]
    again = conv.Conversations(conv.ThreadStore(conv.sqlite_run(convs.sqlite)),
                               convs.checkpointer)                 # compiled afresh
    seen = []

    def complete(client, model, messages, tools):
        seen.append([m.get("content") for m in messages])
        return model_says("And one on that one.")(client, model, messages, tools)
    monkeypatch.setattr(app, "complete", complete)
    assert service(again).ask("and CNC-03?", sid).body["turns"] == 2
    assert seen[0][1:] == ["open work orders on CNC-02?", "Three are open.", "and CNC-03?"]
    assert seen[0][0].startswith(app.SYSTEM_PROMPT)                # from the code, not saved


def test_a_refused_question_is_stored_only_as_its_placeholder(scripted):
    scripted(AssertionError("the model was called"))
    convs = conversations()
    r = service(convs).ask("What is the name of employee EMP015?")
    assert r.status == 200 and r.body["refused"] == "pdpa"
    assert r.body["usage"]["model_calls"] == 0
    sid = r.body["session_id"]
    assert "EMP015" not in json.dumps(convs.messages(sid))         # the checkpoint
    t = convs.threads.get(sid)
    assert "EMP015" not in json.dumps(t.display) and "EMP015" not in t.title   # the row


def test_an_answer_with_personal_data_never_reaches_the_checkpoint(scripted):
    scripted("Mail a.tan.jme@example.com.")
    convs = conversations()
    r = service(convs).ask("who handles the lathe?")
    assert r.body["answer_replaced"] == "pii" and "@" not in r.body["answer"]
    sid = r.body["session_id"]
    state = convs.graph.get_state(convs.config(sid)).values
    assert "@" not in json.dumps(state, default=str)              # no key of the state holds it


def test_unknown_and_unreadable_conversations(scripted):
    scripted("x")
    sid = conv.new_id()
    r = service(conversations()).ask("hello", sid)
    assert (r.status, r.body["error"]["code"]) == (404, "unknown_session")
    r = service(conversations(down=True)).ask("hello", sid)
    assert (r.status, r.body["error"]["code"]) == (503, "store_down")
    assert "db.internal" not in json.dumps(r.body)                 # the error's type only
    r = service(None).ask("hello", sid)
    assert (r.status, r.body["error"]["code"]) == (503, "sessions_off")


def test_without_saved_conversations_a_question_is_still_answered(scripted):
    scripted("Hello.")
    r = service(None).ask("hello")
    assert r.status == 200 and r.body["answer"] == "Hello."
    assert r.body["saved"] is False and r.body["session_id"] is None


def test_a_store_that_cant_save_is_reported_not_hidden(scripted):
    scripted("Hello.")
    r = service(conversations(down=True)).ask("hello")
    assert r.status == 200 and r.body["answer"] == "Hello."
    assert r.body["saved"] is False and r.body["session_id"] is None
    assert r.body["save_error"].startswith("couldn't save the conversation")


def test_a_new_conversation_has_its_row_before_its_first_answer(scripted, monkeypatch):
    """So its checkpoints are always covered by retention, even if the turn fails."""
    convs = conversations()
    rows = []

    def boom(sid, question, context, on_step=None):
        rows.extend(convs.threads.recent(10) or [])
        rows.append(convs.threads.get(sid))
        raise RuntimeError("the model client broke")
    r = service(convs, ask_fn=boom).ask("hello")
    assert r.status == 500 and rows[-1].found and rows[-1].turns == 0
    assert rows[:-1] == []                         # a row with no question isn't listed


def test_a_bug_is_a_500_with_no_trace():
    def boom(*a, **k):
        raise KeyError("secret internals")
    r = service(conversations(), ask_fn=boom).ask("hello")
    assert (r.status, r.body["error"]["code"]) == (500, "internal")
    assert "secret" not in json.dumps(r.body)


def test_two_questions_on_one_conversation_at_once_are_taken_in_turn(scripted, monkeypatch):
    convs = conversations()
    svc = service(convs)
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
    users = [m["text"] for m in convs.threads.get(sid).display if m["role"] == "user"]
    assert users[0] == "first" and sorted(users[1:]) == ["second", "third"]   # none lost
    said = [m["content"] for m in convs.messages(sid) if m["role"] == "user"]
    assert said[0] == "first" and sorted(said[1:]) == ["second", "third"]


# -------------------------------------------------------------- conversations ---

def test_reading_one_conversation_back(scripted):
    convs = conversations()
    svc = service(convs)
    scripted("Twelve.")
    sid = svc.ask("how many machines?").body["session_id"]
    r = svc.session(sid)
    assert r.status == 200 and r.body["turns"] == 1
    assert [m["role"] for m in r.body["messages"]] == ["user", "assistant"]
    assert r.body["spend"]["line"].startswith("This conversation:")
    assert svc.session("nope").status == 422
    assert svc.session(conv.new_id()).status == 404


def test_listing_recent_conversations(scripted):
    convs = conversations()
    svc = service(convs)
    scripted("ok")
    for q in ("one", "two", "three"):
        svc.ask(q)
    r = svc.recent(2)
    assert r.status == 200 and [s["title"] for s in r.body["sessions"]] == ["three", "two"]
    assert svc.recent(0).status == 200                          # clamped up to 1
    assert svc.recent("lots").status == 422
    assert service(None).recent().status == 503
    assert service(conversations(down=True)).recent().status == 503


# ------------------------------------------------------------------ streaming ---

def test_streaming_sends_each_step_then_the_answer(scripted):
    scripted([("c1", "list_machines", "{}")], "Twelve machines.", sources=["CMMS machines"])
    convs = conversations()
    events = list(service(convs).ask_stream("how many machines?"))
    kinds = [e["event"] for e in events]
    assert kinds == ["step"] * 5 + ["answer"]
    assert [e["data"]["node"] for e in events[:-1]] == ["guard_in", "model", "tools", "model",
                                                       "guard_out"]
    final = events[-1]["data"]
    assert final["answer"] == "Twelve machines." and final["saved"] is True
    assert convs.threads.get(final["session_id"]).found
    assert "machines?" not in json.dumps([e["data"] for e in events[:-1]])   # no text in steps


def test_streaming_a_refused_question_sends_one_step_then_the_answer(scripted):
    scripted(AssertionError("the model was called"))
    events = list(service(conversations()).ask_stream("What is the name of employee EMP015?"))
    assert [e["event"] for e in events] == ["step", "answer"]
    assert events[0]["data"]["passed"] is False and events[1]["data"]["refused"] == "pdpa"


def test_streaming_a_bad_request_sends_one_error():
    events = list(service(conversations()).ask_stream("", None))
    assert events == [{"event": "error", "data": {"status": 422, "error": {
        "code": "bad_question", "message": "question must be non-empty text."}}}]


def test_streaming_gives_up_waiting_but_the_question_still_finishes(scripted, monkeypatch):
    convs = conversations()
    done = threading.Event()

    def slow(client, model, messages, tools):
        time.sleep(0.3)
        done.set()
        return model_says("Late.")(client, model, messages, tools)
    scripted("unused")
    monkeypatch.setattr(app, "complete", slow)
    events = list(service(convs).ask_stream("hello", timeout=0.05))
    assert events[-1]["event"] == "error" and events[-1]["data"]["status"] == 504
    assert done.wait(2)
    for _ in range(200):                                   # saved once it finishes
        rows = convs.threads.recent(10) or []
        if rows:
            break
        time.sleep(0.01)
    assert [r.turns for r in rows] == [1]


def test_an_event_is_written_as_server_sent_events_expect():
    out = api.sse({"event": "step", "data": {"node": "model", "round": 1}})
    assert out == 'event: step\ndata: {"node": "model", "round": 1}\n\n'


# --------------------------------------------------------------- HTTP routes ---

@pytest.fixture
def client(scripted):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    return TestClient(api.create_app(service(conversations())))


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