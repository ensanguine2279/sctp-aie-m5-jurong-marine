"""Words the data can't take as written (agent_app.py), found 10 Nov 2026.

Asked "how many work orders were completed in nov 202", the app read "202" as 2026 and
"completed" as closed, and said neither. A year written short is now asked about in code,
with no model call; "completed" is read as status CLOSED (the CMMS has no completion date),
with a note on the tool result that makes the answer say so. No database, no real model."""
from __future__ import annotations

import json
import logging
from datetime import date

import agent_app as app
from test_agent_app import FakeMessage, FakeResponse, FakeToolCall, FakeToolResult, scripted

DATES = {"incidents": date(2026, 5, 28), "work orders": date(2026, 6, 30),
         "sensor readings": date(2026, 6, 30)}


class Events(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        self.events.append(json.loads(record.getMessage()))


def _logger():
    h = Events()
    lg = logging.getLogger(f"test_clarify.{id(h)}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(h)
    return lg, h


# ------------------------------------------------------------------ the year ---

def test_a_year_written_short_is_found():
    for q, written, month in [
            ("how many work orders were completed in nov 202", "nov 202", 11),
            ("incidents in November 202?", "November 202", 11),
            ("work orders in Nov. 202", "Nov. 202", 11),
            ("anything in mar202", "mar202", 3),
            ("what happened in year 202", "year 202", None)]:
        assert app.incomplete_year(q) == (written, month), q


def test_full_years_days_and_ids_are_not_short_years():
    for q in ["how many work orders were completed in nov 2025",
              "incidents on nov 20",                  # a day
              "what about 2 nov?",
              "work order 11570",
              "how many in 2025",
              "the old scale",
              "CNC-02 since may 2026",
              ""]:
        assert app.incomplete_year(q) is None, q


def test_the_question_back_offers_the_years_the_data_has():
    q = app.year_question(("nov 202", 11), DATES)
    assert "November 2025 or November 2024" in q and "30 Jun 2026" in q
    assert "won't guess" in q
    assert "March 2026 or March 2025" in app.year_question(("mar 202", 3), DATES)
    plain = app.year_question(("nov 202", 11), None)     # where the data ends unknown
    assert "Which year did you mean?" in plain and "2026" not in plain
    assert "Which year did you mean?" in app.year_question(("year 202", None), DATES)


def test_a_short_year_is_asked_about_and_the_model_is_never_called(monkeypatch):
    def never(client, model, messages, tools):
        raise AssertionError("the model must not be called")
    monkeypatch.setattr(app, "complete", never)
    lg, h = _logger()
    messages, usage = app.new_conversation(), {}
    answer, sources = app.ask(None, client=None, messages=messages,
                              question="how many work orders were completed in nov 202",
                              logger=lg, usage=usage)
    assert "nov 202" in answer and "Which year" in answer and sources == []
    assert usage["model_calls"] == 0
    # the question and the reply are both kept, so "2025" reaches the model with its question
    assert [m["role"] for m in messages[1:]] == ["user", "assistant"]
    assert messages[1]["content"].endswith("nov 202") and messages[2]["content"] == answer
    kinds = [e["event"] for e in h.events]
    assert kinds == ["year_unclear", "turn"] and h.events[0]["written"] == "nov 202"


def test_the_reply_with_the_year_reaches_the_model_with_the_question(monkeypatch):
    seen = []

    def answer(client, model, messages, tools):
        seen.append([m["content"] for m in messages if m["role"] in ("user", "assistant")])
        return FakeResponse(FakeMessage(content="(answer)"))
    monkeypatch.setattr(app, "complete", answer)
    messages = app.new_conversation()
    app.ask(None, None, messages, "how many work orders were completed in nov 202")
    app.ask(None, None, messages, "2025")
    assert seen[0][-3:-1] == ["how many work orders were completed in nov 202",
                              app.year_question(("nov 202", 11), None)]
    assert seen[0][-1] == "2025"


# ------------------------------------------------------------- "completed" ---

def _user(text):
    return [{"role": "system", "content": "s"}, {"role": "user", "content": text}]


def test_completed_or_finished_work_orders_get_the_note():
    for word, q in [("completed", "how many work orders were completed in November 2025?"),
                    ("complete", "list the complete work orders for CNC-02"),
                    ("finished", "which jobs were finished last month")]:
        note = app.completion_note("list_work_orders", _user(q))
        assert note and f'"{word}"' in note and "CLOSED" in note and "raised" in note, q
    assert app.completion_note("count_incidents", _user("completed work orders")) is None
    assert app.completion_note("list_work_orders", _user("how many are open?")) is None
    # a follow-up that names a status of its own ends the look-back
    later = _user("how many were completed?") + [{"role": "assistant", "content": "12"},
                                                 {"role": "user", "content": "and open?"}]
    assert app.completion_note("list_work_orders", later) is None
    assert app.completion_note("list_work_orders", _user("how many are incomplete?")) is None
    assert app.completion_note("list_work_orders", _user("list the unfinished ones")) is None


def test_completed_carries_into_a_follow_up_that_names_no_status():
    """After the year was asked about, the user's reply is only "November 2025": the
    question it completes said "completed", so the note still applies."""
    messages = _user("how many work orders were completed in nov 202") + [
        {"role": "assistant", "content": app.year_question(("nov 202", 11), None)},
        {"role": "user", "content": "November 2025"}]
    assert "read as status CLOSED" in app.completion_note("list_work_orders", messages)
    more = messages + [{"role": "assistant", "content": "12"},
                       {"role": "user", "content": "and in 2024?"}]
    assert app.completion_note("list_work_orders", more)


def test_the_note_reaches_the_model_and_the_page(monkeypatch):
    call = FakeToolCall("c1", "list_work_orders",
                        '{"status": "closed", "date_from": "2025-11-01", "date_to": "2025-11-30"}')
    monkeypatch.setattr(app, "complete", scripted(
        FakeResponse(FakeMessage(content=None, tool_calls=[call])),
        FakeResponse(FakeMessage(content="12 work orders were closed."))))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: FakeToolResult())
    lg, h = _logger()
    messages, notes = _user("how many work orders were completed in November 2025?"), []
    app.run_turn(None, None, messages, logger=lg, notes_out=notes)
    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert "read as status CLOSED" in tool_msg["content"]
    assert any("read as status CLOSED" in n for n in notes)
    assert "completed_read_as_closed" in [e["event"] for e in h.events]


def test_a_note_is_added_to_a_json_result_or_after_a_plain_one():
    body = json.loads(app._with_note('{"ok": true, "notes": ["a"]}', "b"))
    assert body["notes"] == ["a", "b"]
    assert app._with_note("plain text", "b") == "plain text\nb"


# ------------------------------------------- the eval runner's grounding (run_evals.py) ---

def _conversation(args: dict, result: dict):
    return [{"role": "system", "content": "s"},
            {"role": "user", "content": "How many work orders were completed in November 2025?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "list_work_orders", "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps(result)}]


def test_the_period_a_successful_call_ran_for_grounds_the_answer():
    """Eval run of 10 Nov 2026 (wo-completed-read-as-closed): 'raised between 1 November 2025
    and 30 November 2025' failed grounding, though it is exactly the period the tool ran for:
    no work order fell on either day, so neither date was in the tool's result."""
    import run_evals as ev
    msgs = _conversation({"status": "closed", "date_from": "2025-11-01",
                          "date_to": "2025-11-30"}, {"ok": True, "data": {"count": 43}})
    answer = ("In November 2025, 43 work orders were closed. They were raised between "
              "1 November 2025 and 30 November 2025.")
    assert ev.call_periods(msgs) == ["2025-11-01", "2025-11-30"]
    assert ev.grounding(answer, ev.evidence_for(msgs)) == ([], [])


def test_a_refused_call_and_other_arguments_ground_nothing():
    import run_evals as ev
    refused = _conversation({"date_from": "2025-11-01", "date_to": "2025-11-30"},
                            {"ok": False, "error": {"code": "no_data"}})
    assert ev.call_periods(refused) == []
    fails, _ = ev.grounding("From 1 November 2025 to 30 November 2025.", ev.evidence_for(refused))
    assert "date 2025-11-30 is in no tool result" in fails
    # a number the model put in its own arguments isn't evidence for that number
    own = _conversation({"at_least_as_severe_as": 7, "date_from": "2025-11-01"},
                        {"ok": True, "data": {"count": 3}})
    fails, _ = ev.grounding("There were 7 such incidents.", ev.evidence_for(own))
    assert "number 7 is in no tool result" in fails