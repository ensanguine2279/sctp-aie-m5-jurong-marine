"""Tests for scripts/agent_graph.py: the LangGraph engine gives the same turn as the loop.

Every scenario below runs through agent_app.ask() twice, once with AGENT_ENGINE=loop and
once with AGENT_ENGINE=graph, with the same scripted model and tools, and the two must
agree on everything a caller sees: the answer, the source labels, the conversation left in
`messages`, the raw sources, the notes, the usage tally and the log events (apart from the
graph's own `graph_step` lines, and timings). No database, no real LLM call.

Uses the real langgraph where it is installed (`uv add langgraph` / `pip install langgraph`),
else tests/mini_langgraph.py, a stand-in for the few calls agent_graph.py makes.
"""
from __future__ import annotations

import copy
import json
import logging

import pytest

import mini_langgraph

STAND_IN = mini_langgraph.install()

import agent_app as app  # noqa: E402
import agent_graph as graph  # noqa: E402
from test_agent_app import (FakeMessage, FakeResponse, FakeToolCall,  # noqa: E402
                            FakeToolResult)


# ------------------------------------------------------------------- helpers ---

class Events(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def emit(self, record):
        self.events.append(json.loads(record.getMessage()))


def capture():
    h = Events()
    lg = logging.getLogger(f"test_agent_graph.{id(h)}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(h)
    return lg, h


def model_says(*replies):
    """A complete_fn from a list of replies made fresh for each run: a str is a plain
    answer, a list of (id, tool, arguments) is a round of tool calls, an Exception is
    raised. The last reply repeats once the list runs out."""
    replies = list(replies)
    calls = {"n": 0}

    def _complete(client, model, messages, tools):
        r = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, str):
            return FakeResponse(FakeMessage(content=r))
        return FakeResponse(FakeMessage(content=None,
                                        tool_calls=[FakeToolCall(*c) for c in r]))
    _complete.calls = calls
    return _complete


def result(ok=True, sources=(), notes=(), error=None, content=None):
    r = FakeToolResult(sources=list(sources), content=content or json.dumps({"ok": ok}))
    r.ok, r.error, r.notes = ok, error, list(notes)
    return r


def _plain(events):
    """The events a caller compares: no graph_step lines, no timestamps or timings."""
    return [{k: v for k, v in e.items() if k not in ("ts", "ms")}
            for e in events if e.get("event") != "graph_step"]


def run(monkeypatch, engine, question, complete_fn, run_tool=None, messages=None,
        on_step=None):
    monkeypatch.setenv("AGENT_ENGINE", engine)
    monkeypatch.setattr(app, "complete", complete_fn)
    monkeypatch.setattr(app.at, "run_tool", run_tool or (lambda db, name, args: result()))
    lg, h = capture()
    msgs = copy.deepcopy(messages) if messages is not None else app.new_conversation()
    raw, notes, usage = [], [], {}
    answer, labels = app.ask(None, None, msgs, question, logger=lg, raw_sources=raw,
                             notes=notes, usage=usage, on_step=on_step)
    return {"answer": answer, "labels": labels, "messages": msgs, "raw": raw, "notes": notes,
            "usage": usage, "events": _plain(h.events), "all_events": h.events,
            "model_calls": getattr(complete_fn, "calls", {}).get("n")}


def both(monkeypatch, question, make_complete, run_tool=None, messages=None):
    """(loop's run, graph's run), each with a fresh scripted model."""
    loop = run(monkeypatch, "loop", question, make_complete(), run_tool, messages)
    g = run(monkeypatch, "graph", question, make_complete(), run_tool, messages)
    return loop, g


def same(loop, g):
    for key in ("answer", "labels", "messages", "raw", "notes", "usage", "events",
                "model_calls"):
        assert g[key] == loop[key], f"{key} differs between the loop and the graph"


# ------------------------------------------------------- same turn, both ways ---

def test_a_plain_answer_is_the_same_on_both_engines(monkeypatch):
    loop, g = both(monkeypatch, "hello", lambda: model_says("Hello."))
    same(loop, g)
    assert g["answer"] == "Hello." and g["usage"]["model_calls"] == 1


def test_a_tool_round_trip_with_sources_and_notes_is_the_same(monkeypatch):
    tool = lambda db, name, args: result(sources=[{"label": "CMMS machines"}],  # noqa: E731
                                         notes=["read off a scan by a person"])
    loop, g = both(monkeypatch, "how many machines?",
                   lambda: model_says([("c1", "list_machines", "{}")], "Twelve."), tool)
    same(loop, g)
    assert g["labels"] == ["CMMS machines"] and g["notes"] == ["read off a scan by a person"]
    assert [m["role"] for m in g["messages"]] == ["system", "user", "assistant", "tool",
                                                  "assistant"]


def test_two_rounds_and_several_calls_in_a_round_are_the_same(monkeypatch):
    srcs = {"get_nameplate": "nameplate photo", "get_spares": "spares sheet",
            "find_sop": "SOP-LOTO-01"}
    tool = lambda db, name, args: result(sources=[{"label": srcs[name]}])  # noqa: E731
    loop, g = both(monkeypatch, "what oil does CNC-02 take, and the lockout SOP?",
                   lambda: model_says([("c1", "get_nameplate", '{"machine": "CNC-02"}'),
                                       ("c2", "get_spares", '{"machine": "CNC-02"}')],
                                      [("c3", "find_sop", '{"question": "lockout"}')],
                                      "ISO VG 68; SOP-LOTO-01 applies."), tool)
    same(loop, g)
    assert g["labels"] == ["nameplate photo", "spares sheet", "SOP-LOTO-01"]
    assert g["usage"]["model_calls"] == 3


def test_a_clarification_goes_to_the_user_with_no_second_model_call_on_both(monkeypatch):
    ask_back = {"code": "needs_clarification", "message": "Which scale: 5 = worst, or 1?"}
    tool = lambda db, name, args: result(ok=False, error=ask_back)  # noqa: E731
    loop, g = both(monkeypatch, "severity 4 or worse in 2025?",
                   lambda: model_says([("c1", "count_incidents", '{"at_least_as_severe_as": 4}')],
                                      AssertionError("model called after a clarification")),
                   tool)
    same(loop, g)
    assert g["answer"] == ask_back["message"] and g["model_calls"] == 1


def test_a_failed_model_call_degrades_the_same_way(monkeypatch):
    loop, g = both(monkeypatch, "is CNC-02 off?", lambda: model_says(app.ChatError("budget")))
    same(loop, g)
    assert g["answer"] == app.CHAT_FAILED
    assert any(e["event"] == "chat_error" for e in g["events"])


def test_too_many_rounds_stops_at_the_same_point(monkeypatch):
    loop, g = both(monkeypatch, "loop forever",
                   lambda: model_says([("c", "list_machines", "{}")]))
    same(loop, g)
    assert g["answer"] == app.ROUNDS_EXHAUSTED
    assert g["usage"]["model_calls"] == app.MAX_TOOL_ROUNDS     # the graph's recursion limit
                                                                # never cut in first


def test_a_refused_question_never_reaches_the_model_on_either(monkeypatch):
    loop, g = both(monkeypatch, "What is the name of employee EMP015?",
                   lambda: model_says(AssertionError("the model was called")))
    same(loop, g)
    assert g["model_calls"] == 0
    assert "EMP015" not in json.dumps(g["messages"])
    assert any(e["event"] == "input_refused" for e in g["events"])


def test_an_answer_with_personal_data_is_replaced_on_both(monkeypatch):
    loop, g = both(monkeypatch, "who do I call about CNC-02?",
                   lambda: model_says("Write to a.tan.jme@example.com about it."))
    same(loop, g)
    assert "@" not in g["answer"] and "@" not in json.dumps(g["messages"])
    assert any(e["event"] == "output_blocked" for e in g["events"])


def test_a_short_year_is_asked_about_with_no_model_call_on_both(monkeypatch):
    loop, g = both(monkeypatch, "how many work orders were completed in nov 202",
                   lambda: model_says(AssertionError("the model was called")))
    same(loop, g)
    assert g["model_calls"] == 0 and "isn't a full year" in g["answer"]


def test_completed_read_as_closed_and_the_scale_guard_act_the_same(monkeypatch):
    seen = []

    def tool(db, name, args):
        seen.append((name, args))
        return result(sources=[{"label": name}])

    loop, g = both(monkeypatch, "how many work orders were completed in 2025, severity 4 or worse?",
                   lambda: model_says([("c1", "list_work_orders", '{"status": "CLOSED"}'),
                                       ("c2", "count_incidents",
                                        '{"at_least_as_severe_as": 4, "severity_scale": "5_is_worst",'
                                        ' "scale_quote": "5 is worst"}')], "Done."), tool)
    same(loop, g)
    kinds = [e["event"] for e in g["events"]]
    assert "completed_read_as_closed" in kinds and "scale_guess_blocked" in kinds
    assert "severity_scale" not in seen[-1][1]                  # the guess removed, both times


def test_a_broken_saved_conversation_is_mended_the_same_way(monkeypatch):
    broken = app.new_conversation() + [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "x", "type": "function",
                         "function": {"name": "list_machines", "arguments": "{}"}}]}]
    loop, g = both(monkeypatch, "again", lambda: model_says("Fine."), messages=broken)
    same(loop, g)
    assert g["events"][0] == {"event": "history_repaired", "dropped": 1}


# --------------------------------------------------------- what the graph adds ---

def test_each_step_is_reported_as_it_happens_without_the_question_or_answer(monkeypatch):
    steps = []
    out = run(monkeypatch, "graph", "secret question text",
              model_says([("c1", "list_machines", "{}")], "secret answer text"),
              on_step=steps.append)
    assert [s["node"] for s in steps] == ["guard_in", "model", "tools", "model", "guard_out"]
    assert steps[1]["tool_calls"] == ["list_machines"] and steps[2]["tools"] == ["list_machines"]
    assert steps[-1]["answer_replaced"] is False and steps[-1]["model_calls"] == 2
    assert "secret" not in json.dumps(steps)
    logged = [e for e in out["all_events"] if e["event"] == "graph_step"]
    assert [e["node"] for e in logged] == [s["node"] for s in steps]


def test_a_refusal_is_one_step(monkeypatch):
    steps = []
    run(monkeypatch, "graph", "What is the name of employee EMP015?",
        model_says(AssertionError("called")), on_step=steps.append)
    assert steps == [{"node": "guard_in", "passed": False, "answered_without_model": True}]


def test_a_failing_on_step_never_breaks_the_turn(monkeypatch):
    def bad(step):
        raise RuntimeError("client went away")
    out = run(monkeypatch, "graph", "hello", model_says("Hello."), on_step=bad)
    assert out["answer"] == "Hello."
    assert any(e["event"] == "on_step_error" for e in out["all_events"])


def test_the_loop_does_not_call_on_step(monkeypatch):
    steps = []
    run(monkeypatch, "loop", "hello", model_says("Hello."), on_step=steps.append)
    assert steps == []


def test_a_turn_cut_off_in_the_graph_leaves_the_conversation_as_it_was(monkeypatch):
    before = app.new_conversation() + [{"role": "user", "content": "earlier"},
                                       {"role": "assistant", "content": "Earlier answer."}]
    msgs = copy.deepcopy(before)
    monkeypatch.setenv("AGENT_ENGINE", "graph")
    monkeypatch.setattr(app, "complete",
                        model_says([("c1", "list_machines", "{}")], KeyboardInterrupt()))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: result())
    with pytest.raises(KeyboardInterrupt):
        app.ask(None, None, msgs, "and now?")
    assert msgs == before


def test_the_engine_switch(monkeypatch):
    for value, want in (("graph", "graph"), (" Graph ", "graph"), ("loop", "loop"),
                        ("", "loop"), ("langchain", "loop")):
        monkeypatch.setenv("AGENT_ENGINE", value)
        assert app.engine() == want
    monkeypatch.delenv("AGENT_ENGINE")
    assert app.engine() == "loop"


def test_graph_asked_for_but_not_installed_runs_the_loop_and_says_so(monkeypatch):
    monkeypatch.setattr(app, "graph_ready", lambda: False)
    steps = []
    out = run(monkeypatch, "graph", "hello", model_says("Hello."), on_step=steps.append)
    assert out["answer"] == "Hello." and steps == []
    assert out["events"][0] == {"event": "engine_unavailable", "engine": "graph",
                                "using": "loop"}


def test_the_diagram_has_the_four_nodes_and_their_routes():
    m = graph.diagram()
    for node in graph.NODES:
        assert node in m
    assert "__start__" in m and "__end__" in m


def test_the_recursion_limit_leaves_room_for_every_round():
    for rounds in (1, 6, 10):
        assert graph.recursion_limit(rounds) >= 2 * rounds + 3