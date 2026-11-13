"""Tests for scripts/agent_graph.py: the agent, as a LangGraph graph (the only engine since
12 Nov 2026).

Until then these tests ran each scenario through both the old loop and the graph and
required the same result; the graph passed all of them, and the eval suite (52/52) on
langgraph 1.2.20, so the loop was retired. The same scenarios now check the graph's
behaviour directly, through agent_app.ask() (which runs it), plus how it is put together:
state and context kept apart, nodes that never change the state they are given, one
compiled graph per process, each step reported as it happens. No database, no real LLM call.

Uses the real langgraph where it is installed, else tests/mini_langgraph.py (conftest.py).
"""
from __future__ import annotations

import copy
import json
import logging

import pytest

import agent_app as app
import agent_graph as graph
from test_agent_app import FakeMessage, FakeResponse, FakeToolCall, FakeToolResult


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
    """A complete_fn from a list of replies: a str is a plain answer, a list of
    (id, tool, arguments) is a round of tool calls, an Exception is raised. The last reply
    repeats once the list runs out. `.calls["n"]` counts the calls."""
    replies = list(replies)
    calls = {"n": 0, "tool_choice": []}

    def _complete(client, model, messages, tools, tool_choice="auto"):
        r = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        calls["tool_choice"].append(tool_choice)
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


def run(monkeypatch, question, complete_fn, run_tool=None, messages=None, on_step=None,
        **kw):
    """One question through ask() (so through the graph): everything a caller sees."""
    monkeypatch.setattr(app, "complete", complete_fn)
    monkeypatch.setattr(app.at, "run_tool", run_tool or (lambda db, name, args: result()))
    lg, h = capture()
    msgs = copy.deepcopy(messages) if messages is not None else app.new_conversation()
    raw, notes, usage = [], [], {}
    answer, labels = app.ask(None, None, msgs, question, logger=lg, raw_sources=raw,
                             notes=notes, usage=usage, on_step=on_step, **kw)
    return {"answer": answer, "labels": labels, "messages": msgs, "raw": raw, "notes": notes,
            "usage": usage, "events": h.events,
            "kinds": [e["event"] for e in h.events if e["event"] != "graph_step"],
            "steps": [e for e in h.events if e["event"] == "graph_step"],
            "model_calls": getattr(complete_fn, "calls", {}).get("n")}


def nodes(out):
    return [s["node"] for s in out["steps"]]


# --------------------------------------------------------------- behaviour ---

def test_a_plain_answer(monkeypatch):
    out = run(monkeypatch, "hello", model_says("Hello."))
    assert out["answer"] == "Hello." and out["usage"]["model_calls"] == 1
    assert nodes(out) == ["guard_in", "model", "guard_out"]
    assert out["messages"][-1] == {"role": "assistant", "content": "Hello."}


def test_a_tool_round_trip_with_sources_and_notes(monkeypatch):
    tool = lambda db, name, args: result(sources=[{"label": "CMMS machines"}],  # noqa: E731
                                         notes=["read off a scan by a person"])
    out = run(monkeypatch, "how many machines?",
              model_says([("c1", "list_machines", "{}")], "Twelve."), tool)
    assert out["answer"] == "Twelve."
    assert out["labels"] == ["CMMS machines"] and out["notes"] == ["read off a scan by a person"]
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "tool",
                                                    "assistant"]
    assert nodes(out) == ["guard_in", "model", "tools", "model", "guard_out"]


def test_two_rounds_and_several_calls_in_a_round(monkeypatch):
    srcs = {"get_nameplate": "nameplate photo", "get_spares": "spares sheet",
            "find_sop": "SOP-LOTO-01"}
    tool = lambda db, name, args: result(sources=[{"label": srcs[name]}])  # noqa: E731
    out = run(monkeypatch, "what oil does CNC-02 take, and the lockout SOP?",
              model_says([("c1", "get_nameplate", '{"machine": "CNC-02"}'),
                          ("c2", "get_spares", '{"machine": "CNC-02"}')],
                         [("c3", "find_sop", '{"question": "lockout"}')],
                         "ISO VG 68; SOP-LOTO-01 applies."), tool)
    assert out["labels"] == ["nameplate photo", "spares sheet", "SOP-LOTO-01"]
    assert out["usage"]["model_calls"] == 3
    assert [s.get("tools") for s in out["steps"] if s["node"] == "tools"] == [
        ["get_nameplate", "get_spares"], ["find_sop"]]


def test_a_clarification_goes_to_the_user_with_no_second_model_call(monkeypatch):
    ask_back = {"code": "needs_clarification", "message": "Which scale: 5 = worst, or 1?"}
    tool = lambda db, name, args: result(ok=False, error=ask_back)  # noqa: E731
    out = run(monkeypatch, "severity 4 or worse in 2025?",
              model_says([("c1", "count_incidents", '{"at_least_as_severe_as": 4}')],
                         AssertionError("model called after a clarification")), tool)
    assert out["answer"] == ask_back["message"] and out["model_calls"] == 1
    assert out["steps"][2]["asked_user"] is True
    assert "clarification_returned" in out["kinds"]


def test_a_failed_model_call_degrades_honestly(monkeypatch):
    out = run(monkeypatch, "is CNC-02 off?", model_says(app.ChatError("budget")))
    assert out["answer"] == app.CHAT_FAILED and "chat_error" in out["kinds"]
    assert out["steps"][1] == {**out["steps"][1], "node": "model", "failed": True}


def test_too_many_rounds_stops_before_the_recursion_limit(monkeypatch):
    out = run(monkeypatch, "how many machines?", model_says([("c", "list_machines", "{}")]))
    assert out["answer"] == app.ROUNDS_EXHAUSTED
    assert out["usage"]["model_calls"] == app.MAX_TOOL_ROUNDS    # LangGraph's own limit never
    assert out["steps"][-2]["stopped"] == "max_rounds"           # cut in first


def test_the_round_limit_comes_from_the_context(monkeypatch):
    out = run(monkeypatch, "how many machines?", model_says([("c", "list_machines", "{}")]),
              max_rounds=2)
    assert out["usage"]["model_calls"] == 2 and out["answer"] == app.ROUNDS_EXHAUSTED


def test_a_refused_question_never_reaches_the_model(monkeypatch):
    out = run(monkeypatch, "What is the name of employee EMP015?",
              model_says(AssertionError("the model was called")))
    assert out["model_calls"] == 0 and "EMP015" not in json.dumps(out["messages"])
    assert out["kinds"] == ["input_refused", "turn"]
    assert [s for s in out["steps"]] == [{**out["steps"][0], "node": "guard_in", "passed": False,
                                          "answered_without_model": True}]


def test_an_answer_with_personal_data_is_replaced_in_the_history_too(monkeypatch):
    out = run(monkeypatch, "who do I call about CNC-02?",
              model_says("Write to a.tan.jme@example.com about it."))
    assert "@" not in out["answer"] and "@" not in json.dumps(out["messages"])
    assert "output_blocked" in out["kinds"] and out["steps"][-1]["answer_replaced"] is True


def test_a_short_year_is_asked_about_with_no_model_call(monkeypatch):
    out = run(monkeypatch, "how many work orders were completed in nov 202",
              model_says(AssertionError("the model was called")))
    assert out["model_calls"] == 0 and "isn't a full year" in out["answer"]
    assert nodes(out) == ["guard_in"]


def test_completed_read_as_closed_and_the_scale_guard_in_one_turn(monkeypatch):
    seen = []

    def tool(db, name, args):
        seen.append((name, args))
        return result(sources=[{"label": name}])

    out = run(monkeypatch, "how many work orders were completed in 2025, severity 4 or worse?",
              model_says([("c1", "list_work_orders", '{"status": "CLOSED"}'),
                          ("c2", "count_incidents",
                           '{"at_least_as_severe_as": 4, "severity_scale": "5_is_worst",'
                           ' "scale_quote": "5 is worst"}')], "Done."), tool)
    assert "completed_read_as_closed" in out["kinds"] and "scale_guess_blocked" in out["kinds"]
    assert "severity_scale" not in seen[-1][1]                  # the guess removed


def test_a_broken_saved_conversation_is_mended(monkeypatch):
    broken = app.new_conversation() + [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "x", "type": "function",
                         "function": {"name": "list_machines", "arguments": "{}"}}]}]
    out = run(monkeypatch, "again", model_says("Fine."), messages=broken)
    assert out["answer"] == "Fine."
    assert out["events"][0]["event"] == "history_repaired" and out["events"][0]["dropped"] == 1
    assert not any(m.get("tool_calls") for m in out["messages"])


# ------------------------------- every fact from a tool call made this turn (#1) ---
# 13 Nov 2026, eval run (mt-last-month-work-orders, turn 3): "Which of those are still open?"
# was answered from the previous turn's tool result in the conversation, with no tool call
# and no sources.

def test_facts_with_no_tool_call_are_asked_again_with_a_tool_required(monkeypatch):
    seen = []

    def tool(db, name, args):
        seen.append(name)
        return result(sources=[{"label": "CMMS work orders"}])
    model = model_says("Open: 11591 and 11572.",                       # from the history
                       [("c1", "list_work_orders", '{"machine": "CNC-02", "status": "open"}')],
                       "Open: 11591 and 11572.")
    out = run(monkeypatch, "Which of those are still open?", model, tool)
    assert model.calls["tool_choice"] == ["auto", "required", "auto"]
    assert seen == ["list_work_orders"] and out["labels"] == ["CMMS work orders"]
    assert "ungrounded_answer_retried" in out["kinds"] and "ungrounded_answer" not in out["kinds"]
    assert out["usage"]["model_calls"] == 3                             # the dropped call is paid for
    said = [m["content"] for m in out["messages"] if m["role"] == "assistant" and m.get("content")]
    assert said == ["Open: 11591 and 11572."]                           # the first answer dropped
    assert nodes(out) == ["guard_in", "model", "model", "tools", "model", "guard_out"]
    assert out["steps"][1]["retry"] and out["steps"][2]["forced"] is True


def test_an_answer_without_facts_is_not_asked_again(monkeypatch):
    model = model_says("Hello, ask me about a machine, an incident or an SOP.")
    out = run(monkeypatch, "hello", model)
    assert model.calls["tool_choice"] == ["auto"] and "ungrounded_answer_retried" not in out["kinds"]


def test_facts_after_a_tool_call_this_turn_are_not_asked_again(monkeypatch):
    model = model_says([("c1", "list_machines", "{}")], "There are 12 machines.")
    out = run(monkeypatch, "how many machines?", model)
    assert model.calls["tool_choice"] == ["auto", "auto"]
    assert out["answer"] == "There are 12 machines."


def test_asked_once_only_then_kept_and_flagged(monkeypatch):
    """If the model still calls no tool (a provider that ignores tool_choice), the answer is
    kept, flagged `ungrounded_answer`, and the eval suite's checks catch it."""
    model = model_says("CNC-02 has 3 open.")
    out = run(monkeypatch, "how many are open on CNC-02?", model)
    assert model.calls["tool_choice"] == ["auto", "required"]
    assert out["answer"] == "CNC-02 has 3 open." and out["labels"] == []
    assert out["kinds"].count("ungrounded_answer_retried") == 1 and "ungrounded_answer" in out["kinds"]


def test_the_real_chat_call_sends_the_tool_choice():
    sent = {}

    class Completions:
        @staticmethod
        def create(**kw):
            sent.update(kw)
            return FakeResponse(FakeMessage(content="x"))
    client = type("C", (), {"chat": type("Ch", (), {"completions": Completions})()})()
    app.complete(client, "m", [], [])
    assert sent["tool_choice"] == "auto"
    app.complete(client, "m", [], [], tool_choice="required")
    assert sent["tool_choice"] == "required"


def test_what_counts_as_stating_facts():
    for answer in ("There are 12 machines.", "CNC-02 is fine.", "WO 11591 is open.",
                   "Raised on 19 June 2026."):
        assert app.states_facts(answer), answer
    for answer in ("Hello.", "I couldn't check that.", "", None):
        assert not app.states_facts(answer), answer


# ------------------------------------------------- how it's put together ---

def test_the_graph_is_compiled_once_and_reused():
    assert graph.graph() is graph.graph()


def test_the_state_holds_the_conversation_and_the_context_holds_the_rest(monkeypatch):
    """What a checkpointer would save (AgentState) has the conversation and this turn's
    results, and never the database, the client or the logger (AgentContext)."""
    monkeypatch.setattr(app, "complete", model_says([("c1", "list_machines", "{}")], "Twelve."))
    monkeypatch.setattr(app.at, "run_tool",
                        lambda db, name, args: result(sources=[{"label": "CMMS machines"}]))
    db, client = object(), object()
    lg, _ = capture()
    ctx = graph.AgentContext(db=db, client=client, logger=lg)
    final = graph.run(app.new_conversation(), "how many machines?", ctx)
    assert final["answer"] == "Twelve." and final["route"] == "end"
    assert final["messages"][-1] == {"role": "assistant", "content": "Twelve."}
    assert final["sources"] == [{"label": "CMMS machines"}] and final["usage"]["model_calls"] == 2
    assert set(final) <= set(graph.AgentState.__annotations__)
    held = json.dumps(final, default=lambda o: f"<{type(o).__name__}>")
    assert "<object>" not in held and "Logger" not in held      # nothing from the context


def test_the_db_and_client_reach_the_nodes_through_the_context(monkeypatch):
    db, client, got = object(), object(), {}

    def complete(c, model, messages, tools):
        got["client"], got["model"] = c, model
        return model_says([("c1", "list_machines", "{}")], "ok")(c, model, messages, tools)
    first = model_says([("c1", "list_machines", "{}")], "ok")
    monkeypatch.setattr(app, "complete", lambda *a: (complete(*a), first(*a))[1])

    def run_tool(d, name, args):
        got["db"] = d
        return result()
    monkeypatch.setattr(app.at, "run_tool", run_tool)
    lg, _ = capture()
    graph.run(app.new_conversation(), "how many machines?",
              graph.AgentContext(db=db, client=client, model="m-test", logger=lg))
    assert got == {"client": client, "model": "m-test", "db": db}


def test_a_complete_fn_in_the_context_is_used_instead_of_agent_app_complete(monkeypatch):
    monkeypatch.setattr(app, "complete", model_says(AssertionError("module complete used")))
    lg, _ = capture()
    final = graph.run(app.new_conversation(), "hello",
                      graph.AgentContext(logger=lg, complete_fn=model_says("From the context.")))
    assert final["answer"] == "From the context."


def test_nodes_never_change_the_state_they_are_given(monkeypatch):
    monkeypatch.setattr(app, "complete", model_says([("c1", "list_machines", "{}")], "Twelve."))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: result())
    lg, _ = capture()
    ctx = graph.AgentContext(logger=lg)
    rt = type("Rt", (), {"context": ctx})()
    state = {"messages": app.new_conversation(), "question": "how many machines?"}
    for node in (graph.guard_in, graph.model, graph.tools, graph.model, graph.guard_out):
        before = copy.deepcopy(state)
        change = node(state, rt)
        assert state == before, node.__name__          # what it was given is untouched
        state = {**state, **change}
    assert state["answer"] == "Twelve."


def test_each_turn_starts_with_empty_results(monkeypatch):
    """With a checkpointer the state persists between questions: this turn's sources,
    notes, usage and round must not carry over from the last one."""
    monkeypatch.setattr(app, "complete", model_says("Second."))
    lg, _ = capture()
    left_over = {"messages": app.new_conversation(), "question": "hello",
                 "sources": [{"label": "old"}], "notes": ["old"], "round": 5,
                 "usage": {**app._new_usage(), "model_calls": 9}, "tool_names": ["old"]}
    rt = type("Rt", (), {"context": graph.AgentContext(logger=lg)})()
    change = graph.guard_in(left_over, rt)
    assert (change["sources"], change["notes"], change["round"], change["tool_names"]) == \
        ([], [], 0, [])
    assert change["usage"]["model_calls"] == 0


# --------------------------------------------------------- what it reports ---

def test_each_step_is_reported_as_it_happens_without_the_question_or_answer(monkeypatch):
    steps = []
    out = run(monkeypatch, "secret question text",
              model_says([("c1", "list_machines", "{}")], "secret answer text"),
              on_step=steps.append)
    assert [s["node"] for s in steps] == ["guard_in", "model", "tools", "model", "guard_out"]
    assert steps[1]["tool_calls"] == ["list_machines"] and steps[2]["tools"] == ["list_machines"]
    assert steps[-1]["answer_replaced"] is False and steps[-1]["model_calls"] == 2
    assert "secret" not in json.dumps(steps)
    assert [s["node"] for s in out["steps"]] == [s["node"] for s in steps]   # and logged


def test_a_refusal_is_one_step(monkeypatch):
    steps = []
    run(monkeypatch, "What is the name of employee EMP015?",
        model_says(AssertionError("called")), on_step=steps.append)
    assert steps == [{"node": "guard_in", "passed": False, "answered_without_model": True}]


def test_a_failing_on_step_never_breaks_the_turn(monkeypatch):
    def bad(step):
        raise RuntimeError("client went away")
    out = run(monkeypatch, "hello", model_says("Hello."), on_step=bad)
    assert out["answer"] == "Hello." and "on_step_error" in out["kinds"]


def test_a_turn_cut_off_inside_the_graph_leaves_the_conversation_as_it_was(monkeypatch):
    before = app.new_conversation() + [{"role": "user", "content": "earlier"},
                                       {"role": "assistant", "content": "Earlier answer."}]
    msgs = copy.deepcopy(before)
    monkeypatch.setattr(app, "complete",
                        model_says([("c1", "list_machines", "{}")], KeyboardInterrupt()))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: result())
    with pytest.raises(KeyboardInterrupt):
        app.ask(None, None, msgs, "and now?")
    assert msgs == before


def test_the_loop_and_the_engine_switch_are_gone():
    for name in ("run_turn", "_ask", "engine", "graph_ready", "model_round", "tool_round",
                 "before_model", "after_model"):
        assert not hasattr(app, name), name


def test_the_diagram_has_the_four_nodes_and_their_routes():
    m = graph.diagram()
    for node in graph.NODES:
        assert node in m
    assert "__start__" in m and "__end__" in m


def test_the_recursion_limit_leaves_room_for_every_round():
    for rounds in (1, 6, 10):
        assert graph.recursion_limit(rounds) >= 2 * rounds + 3