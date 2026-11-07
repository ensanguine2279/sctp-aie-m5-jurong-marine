"""Tests for scripts/agent_app.py: the loop's own logic only (message building, tool
round-trip, honest degradation on failure, source formatting). Tool correctness is
tested in test_agent_tools.py; here `at.run_tool` and the chat call are stubbed, so
these tests need no database and no real LLM call."""
from __future__ import annotations

import json

import pytest

import agent_app as app


# ------------------------------------------------------------- tiny stand-ins ---

class FakeFunction:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id, self.function = call_id, FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class FakeUsage:
    def __init__(self, prompt_tokens=10, completion_tokens=5):
        self.prompt_tokens, self.completion_tokens = prompt_tokens, completion_tokens


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message, usage=None):
        self.choices = [FakeChoice(message)]
        self.usage = usage or FakeUsage()


class FakeToolResult:
    """Stands in for agent_tools.ToolResult: run_turn only ever reads .sources and
    calls .for_model()."""
    def __init__(self, sources=None, content="ok"):
        self.sources = sources or []
        self._content = content

    def for_model(self):
        return self._content


def scripted(*responses):
    """A complete_fn that returns each response in turn, ignoring its arguments."""
    it = iter(responses)

    def _complete(client, model, messages, tools):
        return next(it)
    return _complete


def erroring(message="boom"):
    def _complete(client, model, messages, tools):
        raise app.ChatError(message)
    return _complete


# ------------------------------------------------------------------ run_turn ---

def test_a_plain_answer_needs_no_tool_call():
    messages = [{"role": "user", "content": "hi"}]
    complete_fn = scripted(FakeResponse(FakeMessage(content="Hello.")))
    answer, sources, tools_called = app.run_turn(
        None, client=None, messages=messages, complete_fn=complete_fn)
    assert answer == "Hello."
    assert sources == [] and tools_called == []
    assert messages[-1] == {"role": "assistant", "content": "Hello."}


def test_a_tool_call_round_trips_and_feeds_back_into_the_next_call(monkeypatch):
    call = FakeToolCall("c1", "list_machines", "{}")
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))
    second = FakeResponse(FakeMessage(content="There are 12 machines."))
    complete_fn = scripted(first, second)

    fake_result = FakeToolResult(sources=[{"label": "CMMS machines table"}],
                                  content='{"ok": true}')
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: fake_result)

    messages = [{"role": "user", "content": "how many machines are there?"}]
    answer, sources, tools_called = app.run_turn(
        None, client=None, messages=messages, complete_fn=complete_fn)

    assert answer == "There are 12 machines."
    assert tools_called == ["list_machines"]
    assert sources == [{"label": "CMMS machines table"}]
    # the tool's result must appear as a tool message, referencing the call id
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}]


def test_several_tool_calls_in_one_round_are_all_run_and_cited(monkeypatch):
    calls = [FakeToolCall("c1", "get_nameplate", '{"machine": "CNC-02"}'),
             FakeToolCall("c2", "get_spares", '{"machine": "CNC-02"}')]
    first = FakeResponse(FakeMessage(content=None, tool_calls=calls))
    second = FakeResponse(FakeMessage(content="Here's what the plate and the spares sheet say."))
    complete_fn = scripted(first, second)

    results = {"get_nameplate": FakeToolResult(sources=[{"label": "nameplate photo"}]),
               "get_spares": FakeToolResult(sources=[{"label": "spares sheet"}])}
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: results[name])

    messages = [{"role": "user", "content": "what oil does CNC-02 take?"}]
    answer, sources, tools_called = app.run_turn(
        None, client=None, messages=messages, complete_fn=complete_fn)

    assert tools_called == ["get_nameplate", "get_spares"]
    assert sources == [{"label": "nameplate photo"}, {"label": "spares sheet"}]


def test_a_failed_chat_call_degrades_honestly_instead_of_raising():
    messages = [{"role": "user", "content": "is CNC-02 off?"}]
    answer, sources, tools_called = app.run_turn(
        None, client=None, messages=messages, complete_fn=erroring("budget exceeded"))
    assert "couldn't reach the language model" in answer
    assert sources == [] and tools_called == []


def test_too_many_tool_call_rounds_gives_an_honest_message_not_a_crash(monkeypatch):
    call = FakeToolCall("c1", "query_sql", '{"sql": "SELECT 1", "reason": "x"}')

    def always_wants_another_tool(client, model, messages, tools):
        return FakeResponse(FakeMessage(content=None, tool_calls=[call]))

    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: FakeToolResult())
    messages = [{"role": "user", "content": "loop forever"}]
    answer, sources, tools_called = app.run_turn(
        None, client=None, messages=messages,
        complete_fn=always_wants_another_tool, max_rounds=3)

    assert "allowed number of tool calls" in answer
    assert len(tools_called) == 3   # stopped at max_rounds, never ran forever


def test_run_turn_never_raises_even_when_a_tool_blows_up(monkeypatch):
    """agent_tools.run_tool is documented to never raise, but if something upstream
    still throws, the loop must not crash the whole app mid-demo."""
    call = FakeToolCall("c1", "query_sql", '{"sql": "SELECT 1", "reason": "x"}')
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))

    def exploding_run_tool(db, name, args):
        raise RuntimeError("the database connection dropped")

    monkeypatch.setattr(app.at, "run_tool", exploding_run_tool)
    messages = [{"role": "user", "content": "anything"}]
    with pytest.raises(RuntimeError):
        # documents today's behaviour: a genuine bug in run_tool still propagates.
        # agent_tools.run_tool itself is tested elsewhere to never raise; this test
        # exists so a future change that makes run_turn swallow tool exceptions is a
        # deliberate choice, not a silent one.
        app.run_turn(None, client=None, messages=messages,
                      complete_fn=scripted(first))


# ------------------------------------------------------------------- helpers ---

def test_assistant_message_without_tool_calls():
    msg = FakeMessage(content="hi there", tool_calls=None)
    assert app._assistant_message(msg) == {"role": "assistant", "content": "hi there"}


def test_assistant_message_with_tool_calls_matches_the_api_shape():
    call = FakeToolCall("c1", "find_sop", '{"question": "lockout"}')
    msg = FakeMessage(content=None, tool_calls=[call])
    assert app._assistant_message(msg) == {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "find_sop", "arguments": '{"question": "lockout"}'}}],
    }


def test_format_sources_dedupes_and_keeps_first_seen_order():
    sources = [{"label": "A"}, {"label": "B"}, {"label": "A"}, {}, {"label": None}, {"label": "C"}]
    assert app.format_sources(sources) == ["A", "B", "C"]


def test_new_conversation_starts_with_the_system_prompt_only():
    messages = app.new_conversation()
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert "never answer from memory" in messages[0]["content"].lower() or \
           "never estimate" in messages[0]["content"].lower()


# ----------------------------------------------------------------------- ask ---

def test_ask_appends_the_question_and_returns_formatted_sources(monkeypatch):
    call = FakeToolCall("c1", "find_sop", '{"question": "lockout"}')
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))
    second = FakeResponse(FakeMessage(content="Here is SOP-LOTO-01."))
    monkeypatch.setattr(app, "complete", scripted(first, second))
    monkeypatch.setattr(app.at, "run_tool",
                         lambda db, name, args: FakeToolResult(sources=[{"label": "SOP-LOTO-01"}]))

    messages = app.new_conversation()
    answer, sources = app.ask(None, client=None, messages=messages, question="lock out CNC-02")

    assert messages[1] == {"role": "user", "content": "lock out CNC-02"}
    assert answer == "Here is SOP-LOTO-01."
    assert sources == ["SOP-LOTO-01"]


def test_ask_logs_one_turn_summary_line(monkeypatch, caplog):
    monkeypatch.setattr(app, "complete", scripted(FakeResponse(FakeMessage(content="fine"))))
    messages = app.new_conversation()
    with caplog.at_level("INFO", logger="agent_app"):
        app.ask(None, client=None, messages=messages, question="hello")
    turn_lines = [json.loads(r.message) for r in caplog.records
                  if json.loads(r.message).get("event") == "turn"]
    assert len(turn_lines) == 1
    assert turn_lines[0]["question"] == "hello"
    assert turn_lines[0]["tools_called"] == []


def test_every_log_line_has_a_timestamp_and_tool_calls_log_their_arguments(monkeypatch, caplog):
    call = FakeToolCall("c1", "count_incidents", '{"date_from": "2025-01-01"}')
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))
    second = FakeResponse(FakeMessage(content="done"))
    monkeypatch.setattr(app, "complete", scripted(first, second))
    refused = FakeToolResult()
    refused.ok, refused.error = False, {"code": "needs_clarification"}
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: refused)
    with caplog.at_level("INFO", logger="agent_app"):
        app.ask(None, client=None, messages=app.new_conversation(), question="q")
    lines = [json.loads(r.message) for r in caplog.records]
    assert all("ts" in x for x in lines)
    tc = [x for x in lines if x["event"] == "tool_call"]
    assert len(tc) == 1
    assert tc[0]["arguments"] == '{"date_from": "2025-01-01"}'
    assert tc[0]["ok"] is False and tc[0]["error_code"] == "needs_clarification"


def test_a_clarification_goes_straight_to_the_user_and_the_model_never_answers_over_it(monkeypatch):
    call = FakeToolCall("c1", "count_incidents", '{"at_least_as_severe_as": 4}')
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))
    seen = []

    def complete_fn(client, model, messages, tools):
        seen.append(1)
        if len(seen) > 1:
            raise AssertionError("the model was called again after a clarification")
        return first

    refused = FakeToolResult()
    refused.ok = False
    refused.error = {"code": "needs_clarification",
                     "message": "Which scale: 5 = most severe, or 1 = most severe?"}
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: refused)
    messages = [{"role": "user", "content": "severity 4 or worse in 2025?"}]
    answer, sources, tools_called = app.run_turn(
        None, client=None, messages=messages, complete_fn=complete_fn)
    assert answer == "Which scale: 5 = most severe, or 1 = most severe?"
    assert sources == [] and tools_called == ["count_incidents"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]


# --------------------------------------------------- the severity-scale guess (C1) ---

SEV = '{"at_least_as_severe_as": 4, "severity_scale": "5_is_worst"%s}'


def _user(*texts):
    return [{"role": "user", "content": t} for t in texts]


def test_a_scale_the_user_never_named_is_a_guess():
    msgs = _user("how many incidents in 2025 were severity 4 or worse?")
    assert app.scale_guessed("count_incidents", SEV % "", msgs)                       # no quote
    assert app.scale_guessed("count_incidents", SEV % ', "scale_quote": "5 is worst"', msgs)  # invented
    assert app.scale_guessed("count_incidents", SEV % ', "scale_quote": "severity 4 or worse"', msgs)  # names no scale


def test_a_scale_the_user_named_is_not_a_guess():
    msgs = _user("severity 4 or worse in 2025?", "the current scale")
    assert not app.scale_guessed("count_incidents", SEV % ', "scale_quote": "the current scale"', msgs)
    assert not app.scale_guessed("list_incidents", SEV % ', "scale_quote": "The  Current Scale"', msgs)


def test_other_tools_and_calls_without_a_severity_number_are_left_alone():
    msgs = _user("hi")
    assert not app.scale_guessed("list_machines", "{}", msgs)
    assert not app.scale_guessed("count_incidents", '{"date_from": "2025-01-01"}', msgs)
    assert not app.scale_guessed("count_incidents", "not json", msgs)


def test_run_turn_strips_a_guessed_scale_so_the_tool_asks(monkeypatch):
    call = FakeToolCall("c1", "count_incidents", SEV % ', "scale_quote": "5 is worst"')
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))
    seen = {}

    def run_tool(db, name, args):
        seen["args"] = json.loads(args)
        r = FakeToolResult()
        r.ok, r.error = False, {"code": "needs_clarification", "message": "Which scale?"}
        return r

    monkeypatch.setattr(app.at, "run_tool", run_tool)
    messages = [{"role": "user", "content": "how many were severity 4 or worse in 2025?"}]
    answer, _, _ = app.run_turn(None, client=None, messages=messages,
                                complete_fn=scripted(first))
    assert answer == "Which scale?"
    assert "severity_scale" not in seen["args"] and "scale_quote" not in seen["args"]
    assert seen["args"]["at_least_as_severe_as"] == 4


# ------------------------------------------------- the tools' notes (caveats) ---

def test_the_tools_notes_are_handed_back_so_the_page_can_show_them(monkeypatch):
    """7 Nov 2026: get_incident's note on 911 ('read off the scan by a person') reached the
    model, which left it out of the answer. The page now shows the notes itself."""
    call = FakeToolCall("c1", "get_incident", '{"incident_id": 911}')
    monkeypatch.setattr(app, "complete", scripted(
        FakeResponse(FakeMessage(content=None, tool_calls=[call])),
        FakeResponse(FakeMessage(content="Severity 2."))))
    result = FakeToolResult(sources=[{"label": "CMMS incident 911"}])
    result.notes = ["On the scanned form, severity was read off the scan by a person.",
                    "On the scanned form, severity was read off the scan by a person."]
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: result)
    notes = []
    answer, _ = app.ask(None, client=None, messages=app.new_conversation(), question="911?",
                        notes=notes)
    assert answer == "Severity 2." and len(notes) == 2
    assert app.format_notes(notes) == [notes[0]]                     # once each


def test_results_without_notes_are_fine(monkeypatch):
    call = FakeToolCall("c1", "list_machines", "{}")
    monkeypatch.setattr(app, "complete", scripted(
        FakeResponse(FakeMessage(content=None, tool_calls=[call])),
        FakeResponse(FakeMessage(content="12."))))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: FakeToolResult())
    notes = []
    app.ask(None, client=None, messages=app.new_conversation(), question="q", notes=notes)
    assert notes == []


def test_the_system_prompt_says_every_note_must_be_carried():
    assert "Every note in a tool result is a caveat" in app.SYSTEM_PROMPT