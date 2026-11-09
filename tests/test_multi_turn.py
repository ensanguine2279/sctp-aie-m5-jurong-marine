"""Multi-turn context (HARNESS_CHECKLIST.md section 5): "and what about last month?".

Relative periods are resolved in code from the calendar's date in Singapore (agent_app.today,
relative_periods), as an assistant on live data would; the model is also told where the
data ends (data_dates) and to say so when a period has no data, then answer for the latest
full month the data has (latest_full_month, dates_note). The dates reach the model in its
system prompt on every question and count as evidence in the eval suite's grounding check;
the eval suite freezes today (AGENT_TODAY, `today:` in cases.yaml). A follow-up reaches the
model with the earlier turns. No database and no model: a stand-in Db and a scripted call."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

import pytest

import agent_app as app
import run_evals as ev

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases.yaml"
NOV10 = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def real_clock(monkeypatch):
    """No frozen date from your shell leaks in; a test that wants one sets it."""
    monkeypatch.delenv("AGENT_TODAY", raising=False)


class FakeDb:
    """Answers the three MAX(...) queries by table name; counts the queries."""

    def __init__(self, answers=None, fail=False):
        self.answers, self.fail = answers or {}, fail
        self.cache, self.queries = {}, []

    def query(self, sql, params=None):
        self.queries.append(sql)
        if self.fail:
            raise ConnectionError("Can't connect to MySQL server")
        for table, value in self.answers.items():
            if table in sql:
                return [{"d": value}]
        return [{"d": None}]


# As the views return them: a DATE, a date as text, a DATETIME as text (sqlite / MySQL).
DATA = {"incident_detail": "2026-05-28", "work_order_detail": date(2026, 6, 30),
        "machine_overview": "2026-06-30 23:48:00"}
DATES = {"incidents": date(2026, 5, 28), "work orders": date(2026, 6, 30),
         "sensor readings": date(2026, 6, 30)}


class _Msg:
    def __init__(self, content):
        self.content, self.tool_calls = content, None


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _Msg(content)})()]
        self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()


def model_that_records(monkeypatch, answers=("ok",)):
    """The chat call stubbed: each call's messages are kept, answers given in turn."""
    seen, it = [], iter(answers)

    def complete(client, model, messages, tools):
        seen.append([dict(m) for m in messages])
        return _Resp(next(it))
    monkeypatch.setattr(app, "complete", complete)
    return seen


needs_names = pytest.mark.skipif(not app.guardrail.names_loaded(),
                                 reason="guardrail name list missing (see the report header)")


# -------------------------------------------------- where the data ends ---

def test_the_data_dates_come_from_each_source_and_are_read_once():
    db = FakeDb(DATA)
    assert app.data_dates(db) == DATES
    n = len(db.queries)
    app.data_dates(db)
    assert len(db.queries) == n == 3                       # cached on the Db


def test_no_database_a_down_one_or_an_empty_one_gives_no_dates():
    assert app.data_dates(None) is None
    down = FakeDb(fail=True)
    assert app.data_dates(down) is None and "data_dates" not in down.cache
    app.data_dates(down)
    assert len(down.queries) == 2                          # not cached: tried again
    assert app.data_dates(FakeDb({})) is None


# ----------------------------------------------- what "today" means ---

def test_today_is_the_calendars_in_singapore_unless_frozen(monkeypatch):
    assert app.today() == datetime.now(app.SHOP_TZ).date()
    monkeypatch.setenv("AGENT_TODAY", "2026-11-10")
    assert app.today() == NOV10
    monkeypatch.setenv("AGENT_TODAY", "next tuesday")       # not a date: ignored
    assert app.today() == datetime.now(app.SHOP_TZ).date()


def test_last_month_is_the_calendars():
    assert app.relative_periods(NOV10) == {
        "today": NOV10,
        "this_month": (date(2026, 11, 1), date(2026, 11, 30)),
        "last_month": (date(2026, 10, 1), date(2026, 10, 31)),
        "this_year": 2026, "last_year": 2025}


@pytest.mark.parametrize("today,last_month,this_month_ends", [
    (date(2026, 1, 15), (date(2025, 12, 1), date(2025, 12, 31)), date(2026, 1, 31)),
    (date(2024, 3, 10), (date(2024, 2, 1), date(2024, 2, 29)), date(2024, 3, 31)),
    (date(2026, 2, 1), (date(2026, 1, 1), date(2026, 1, 31)), date(2026, 2, 28)),
])
def test_month_and_year_boundaries(today, last_month, this_month_ends):
    p = app.relative_periods(today)
    assert p["last_month"] == last_month and p["this_month"][1] == this_month_ends
    assert (p["this_year"], p["last_year"]) == (today.year, today.year - 1)


@pytest.mark.parametrize("end,month", [
    (date(2026, 6, 30), (date(2026, 6, 1), date(2026, 6, 30))),       # ends on a month's last day
    (date(2024, 3, 15), (date(2024, 2, 1), date(2024, 2, 29))),       # mid-month: the one before
    (date(2025, 12, 31), (date(2025, 12, 1), date(2025, 12, 31))),
])
def test_the_latest_full_month_the_data_has(end, month):
    assert app.latest_full_month(end) == month


def test_the_note_gives_today_where_the_data_ends_and_the_fallback():
    note = app.dates_note(DATES, NOV10)
    for said in ["Today is 10 Nov 2026 (Singapore)",
                 '"this month" is 1 Nov 2026 to 30 Nov 2026',
                 '"last month" is 1 Oct 2026 to 31 Oct 2026', '"this year" is 2026',
                 '"last year" is 2025', "runs to 30 Jun 2026", "incidents to 28 May 2026",
                 "work orders to 30 Jun 2026",
                 "latest full month the data has, 1 Jun 2026 to 30 Jun 2026 (June 2026)",
                 "Never present it as the period asked about", "say them in the answer",
                 '"And what about last month?" asks the same question again']:
        assert said in note, said


def test_on_live_data_there_is_no_fallback_to_explain():
    note = app.dates_note(DATES, date(2026, 6, 30))
    assert "runs to 30 Jun 2026" in note and "starts after the data ends" not in note


def test_without_the_datas_dates_the_model_is_told_to_ask():
    note = app.dates_note(None, NOV10)
    assert "Today is 10 Nov 2026" in note and "ask the user for the exact dates" in note
    assert "runs to" not in note                            # nothing claimed about the data


# ---------------------------------------------- the dates reach the model ---

@needs_names
def test_every_question_reaches_the_model_with_the_dates(monkeypatch):
    monkeypatch.setenv("AGENT_TODAY", "2026-11-10")
    seen = model_that_records(monkeypatch)
    app.ask(FakeDb(DATA), None, app.new_conversation(),
            "How many work orders did CNC-02 have last month?")
    system = seen[0][0]
    assert system["role"] == "system" and system["content"].startswith(app.SYSTEM_PROMPT)
    assert '"last month" is 1 Oct 2026 to 31 Oct 2026' in system["content"]
    assert "(June 2026)" in system["content"]


@needs_names
def test_unreadable_dates_are_logged_and_the_model_told_to_ask(monkeypatch):
    seen = model_that_records(monkeypatch)
    events = []

    class Grab(logging.Handler):
        def emit(self, r):
            events.append(json.loads(r.getMessage())["event"])
    lg = logging.getLogger("test_multiturn.dates")
    lg.setLevel(logging.INFO)
    lg.addHandler(Grab())
    app.ask(FakeDb(fail=True), None, app.new_conversation(), "what oil does CNC-03 take?",
            logger=lg)
    assert "dates_unknown" in events
    assert "ask the user for the exact dates" in seen[0][0]["content"]


@needs_names
def test_without_a_database_the_prompt_is_unchanged(monkeypatch):
    model_that_records(monkeypatch)
    messages = app.new_conversation()
    app.ask(None, None, messages, "what oil does CNC-03 take?")
    assert messages[0] == {"role": "system", "content": app.SYSTEM_PROMPT}


@needs_names
def test_a_refused_question_reads_no_dates(monkeypatch):
    model_that_records(monkeypatch)
    db = FakeDb(DATA)
    app.ask(db, None, app.new_conversation(), "What is the name of employee EMP015?")
    assert db.queries == []


# ------------------------------------------------------- follow-ups ---

@needs_names
def test_a_follow_up_reaches_the_model_with_the_earlier_turns_and_one_system_prompt(monkeypatch):
    seen = model_that_records(monkeypatch, ["CNC-02 had 4.", "None in October; in June 2026, 1."])
    db = FakeDb(DATA)
    messages = app.new_conversation()
    app.ask(db, None, messages, "How many work orders did CNC-02 have this year?")
    app.ask(db, None, messages, "And what about last month?")
    second = seen[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert [m["content"] for m in second[1:]] == [
        "How many work orders did CNC-02 have this year?", "CNC-02 had 4.",
        "And what about last month?"]
    assert second[0]["content"].count("\nDates:\n") == 1   # refreshed, not stacked


@needs_names
def test_a_reloaded_conversation_gets_the_dates_on_its_next_question(monkeypatch):
    monkeypatch.setenv("AGENT_TODAY", "2026-11-10")
    seen = model_that_records(monkeypatch)
    saved = [{"role": "user", "content": "How many work orders did CNC-02 have this year?"},
             {"role": "assistant", "content": "CNC-02 had 4."}]
    messages = app.new_conversation() + saved               # as the page restores it
    app.ask(FakeDb(DATA), None, messages, "And what about last month?")
    assert "1 Oct 2026 to 31 Oct 2026" in seen[0][0]["content"]
    assert seen[0][1:3] == saved


# ------------------------------------- the eval suite's grounding check ---

def test_the_dates_given_to_the_model_count_as_evidence_and_nothing_more():
    messages = [{"role": "system", "content": app.SYSTEM_PROMPT + app.dates_note(DATES, NOV10)},
                {"role": "user", "content": "And what about last month?"}]
    answer = ("There's no data for October 2026: the shop's data runs to 30 Jun 2026. "
              "For June 2026, the latest full month, see below.")
    assert ev.grounding(answer, ev.evidence_for(messages)) == ([], [])
    fails, _ = ev.grounding(answer, ev.evidence_for(messages[1:]))     # without the note
    assert any("2026-10" in f for f in fails) and any("2026-06-30" in f for f in fails)
    fails, _ = ev.grounding("No work orders in September 2026.", ev.evidence_for(messages))
    assert fails == ["month 2026-09 is in no tool result"]            # a month nobody gave it


# -------------------------------------------- the cases in cases.yaml ---

def _multiturn():
    return [c for c in ev.load_cases(CASES) if "multiturn" in (c.get("tags") or [])]


def test_the_suite_has_real_multi_turn_cases():
    cases = _multiturn()
    assert len(cases) >= 6 and all(len(c["turns"]) >= 2 for c in cases)
    asks = [t["ask"].lower() for c in cases for t in c["turns"]]
    assert any("what about last month" in a for a in asks)           # the checklist's example
    refused_first = [c for c in cases if c["turns"][0]["expect"].get("max_model_calls") == 0]
    assert refused_first, "a conversation that carries on after a refusal"
    refused_later = [c for c in cases
                     if any(t["expect"].get("max_model_calls") == 0 for t in c["turns"][1:])]
    assert refused_later, "a follow-up that turns into a PDPA question"


def test_every_follow_up_is_refused_by_the_guard_exactly_when_the_case_says_so():
    """A follow-up the guard wrongly refused would fail its case only on the real model;
    this catches it for free. Patterns only: no staff name in these questions."""
    for c in ev.load_cases(CASES):
        if not {"multiturn", "dates"} & set(c.get("tags") or []):
            continue
        for i, t in enumerate(c["turns"], 1):
            refused = not app.guardrail.check_input(t["ask"]).allowed
            expected = (t.get("expect") or {}).get("max_model_calls") == 0
            assert refused == expected, f"{c['id']} turn {i}: {t['ask']!r}"


def test_the_suite_freezes_today_and_its_dates_follow_from_it():
    """The runner treats `today:` in cases.yaml as today, so "last month" is the same month
    in every run. The cases expect that month (October 2026) to have no data and June 2026,
    the latest full month, instead; if either date moves, this is the reminder."""
    frozen = date.fromisoformat(ev.case_file_today(CASES))
    p = app.relative_periods(frozen)
    assert p["last_month"][0].strftime("%b %Y") == "Oct 2026"
    first, last = (d.isoformat() for d in app.latest_full_month(date(2026, 6, 30)))
    case = {c["id"]: c for c in ev.load_cases(CASES)}["mt-last-month-work-orders"]
    turns = [t["expect"] for t in case["turns"]]
    args = [a["args"] for e in turns for a in e.get("tool_args") or []]
    assert args[0]["date_from"] == f"{p['this_year']}-01-01"
    assert (args[1]["date_from"], args[1]["date_to"]) == (first, last)   # the June fallback
    assert args[2]["date_from"] == first
    assert any("Oct" in pat for pat in turns[1]["answer_matches"])       # and says October


def test_the_runner_reads_today_from_the_cases_file(tmp_path):
    f = tmp_path / "cases.yaml"
    f.write_text("today: 2026-11-10\ncases: []\n", encoding="utf-8")
    assert ev.case_file_today(f) == "2026-11-10"
    f.write_text("cases: []\n", encoding="utf-8")
    assert ev.case_file_today(f) is None
    f.write_text("today: next week\ncases: []\n", encoding="utf-8")
    with pytest.raises(ev.CaseError, match="must be a date"):
        ev.case_file_today(f)


def test_check_shows_the_frozen_date(capsys):
    assert ev.main(["--check", "--cases", str(CASES)]) == 0
    assert "today 2026-11-10: OK" in capsys.readouterr().out