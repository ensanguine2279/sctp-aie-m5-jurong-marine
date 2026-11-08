"""Tests for scripts/run_evals.py: the eval runner's own checking logic, and evals/cases.yaml
itself. The model and the tools are stubbed (scripted replies, a fake run_tool), so these
need no database and no real LLM call, and cost nothing. The real run is
`python scripts/run_evals.py` (docs/evals.md)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_app as app
import run_evals as ev

CASES = Path(__file__).resolve().parent.parent / "evals" / "cases.yaml"


# ------------------------------------------------------------- stand-ins ---

class Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class Call:
    def __init__(self, cid, name, arguments):
        self.id, self.function = cid, Fn(name, arguments)


class Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class Usage:
    prompt_tokens, completion_tokens = 2500, 50


class Resp:
    def __init__(self, msg):
        self.choices = [type("C", (), {"message": msg})()]
        self.usage = Usage()


def scripted(*msgs):
    it = iter(msgs)

    def complete(client, model, messages, tools):
        try:
            return Resp(next(it))
        except StopIteration:
            raise AssertionError("the model was called more often than scripted") from None
    return complete


class Result:
    """Stands in for agent_tools.ToolResult: what run_turn reads."""

    def __init__(self, tool, ok=True, data=None, sources=None, notes=None, error=None):
        self.tool, self.ok, self.data = tool, ok, data
        self.sources, self.notes, self.error = sources or [], notes or [], error

    def for_model(self):
        return json.dumps({"tool": self.tool, "ok": self.ok, "data": self.data,
                           "sources": self.sources, "notes": self.notes, "error": self.error},
                          separators=(",", ":"))


INCIDENTS = {"label": "CMMS incidents: jurongmarine_cmms.sql, lines 290, 291 (cmms_safety_incidents_audit)"}


def count_incidents(args):
    """Behaves like the real tool on the severity rule: a number with no scale is refused."""
    if args.get("at_least_as_severe_as") and not args.get("severity_scale"):
        return Result("count_incidents", False, error={
            "code": "needs_clarification",
            "message": "Severity 4 means opposite things on the two scales. Ask the user: the "
                       "current scale (5 = most severe, used since Jul 2025) or the old one "
                       "(1 = most severe)?"})
    return Result("count_incidents", True, sources=[INCIDENTS], data={
        "count": 5, "incident_ids": [904, 913, 939, 953, 978],
        "unplaced": [{"incident_id": 906, "incident_date": "2025-07-11"},
                     {"incident_id": 907, "incident_date": "2025-07-04"}]})


@pytest.fixture
def tools(monkeypatch):
    def run_tool(db, name, arguments):
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if name == "count_incidents":
            return count_incidents(args)
        raise AssertionError(f"unexpected tool {name}")
    monkeypatch.setattr(app.at, "run_tool", run_tool)
    monkeypatch.setattr(app.at, "tool_schemas", lambda: [])


@pytest.fixture
def capture():
    c = ev.EventCapture()
    app.log.setLevel("INFO")
    app.log.addHandler(c)
    yield c
    app.log.removeHandler(c)


def oracle(tool, args):
    return json.loads(count_incidents(args).for_model())


def severity_case():
    return next(c for c in ev.load_cases(CASES) if c["id"] == "severity-current-scale")


Q1 = '{"date_from":"2025-01-01","date_to":"2025-12-31","at_least_as_severe_as":4'


# ------------------------------------------------------------- cases.yaml ---

def test_the_suite_meets_the_checklist():
    """HARNESS_CHECKLIST.md section 8: 20+ cases, at least two whose right answer is a
    refusal; section 3: the kill-shot question in at least five phrasings."""
    cases = ev.load_cases(CASES)
    assert len(cases) >= 20
    assert sum("refusal" in c.get("tags", []) for c in cases) >= 2
    killshots = [c for c in cases if "killshot" in c.get("tags", [])]
    assert len({c["turns"][0]["ask"] for c in killshots}) >= 5


def test_every_killshot_phrasing_must_ask_and_never_count():
    for c in ev.load_cases(CASES):
        if "killshot" in c.get("tags", []):
            exp = c["turns"][0]["expect"]
            assert exp["clarification"] is True, c["id"]
            assert {"count_incidents", "list_incidents"} <= set(exp["tools_not_ok"]), c["id"]


def test_a_malformed_case_is_refused_before_any_call(tmp_path):
    p = tmp_path / "cases.yaml"
    p.write_text("cases:\n"
                 "- id: a\n  turns: [{ask: q, expect: {answer_contain: [x]}}]\n"
                 "- id: a\n  turns: [{ask: q, expect: {answer_matches: ['(']}}]\n"
                 "- id: b\n  turns: []\n", encoding="utf-8")
    with pytest.raises(ev.CaseError) as e:
        ev.load_cases(p)
    msg = str(e.value)
    assert "answer_contain" in msg and "duplicate id" in msg and "bad regex" in msg
    assert "non-empty 'turns'" in msg


def test_select_by_id_and_tag():
    cases = ev.load_cases(CASES)
    assert [c["id"] for c in ev.select(cases, only="oil-cnc03")] == ["oil-cnc03"]
    assert all("killshot" in c["tags"] for c in ev.select(cases, tag="killshot"))
    with pytest.raises(ev.CaseError):
        ev.select(cases, only="no-such-case")


# -------------------------------------------------------------- grounding ---

def test_a_number_no_tool_returned_is_not_grounded():
    e = ev.Evidence(['{"count":5,"unplaced":[906,907]}', "how many in 2025?"])
    fails, _ = ev.grounding("There were 5 incidents in 2025, plus 906 and 907.", e)
    assert fails == []
    fails, _ = ev.grounding("There were 12 incidents in 2025.", e)
    assert fails == ["number 12 is in no tool result"]


def test_a_rounded_number_is_a_warning_not_a_failure():
    e = ev.Evidence(['{"baseline_mean":51.93,"recent_mean":55.97}'])
    fails, warns = ev.grounding("It went from 51.9 to 56.0 °C.", e)
    assert fails == [] and len(warns) == 2


def test_ids_are_checked_and_their_digits_are_not_counted_as_numbers():
    e = ev.Evidence(['{"parts":[{"part_no":"BRG-6310","machine_id":"CNC-02"}]}'])
    assert ev.grounding("BRG-6310 is for CNC-02.", e) == ([], [])
    fails, _ = ev.grounding("Order BRG-6205 for CNC-02.", e)
    assert fails == ["'BRG-6205' is in no tool result"]


def test_dates_match_whatever_format_the_answer_uses():
    e = ev.Evidence(['{"incident_date":"2024-01-24"}'])
    for text in ("on 2024-01-24", "on 24 Jan 2024", "on January 24, 2024", "on 24/01/2024"):
        assert ev.grounding(f"Incident 917 happened {text}.", ev.Evidence(
            ['{"incident_id":917,"incident_date":"2024-01-24"}'])) == ([], []), text
    fails, _ = ev.grounding("It happened on 25 Jan 2024.", e)
    assert fails == ["date 2024-01-25 is in no tool result"]


def test_thousands_separators_and_compact_json_lists():
    e = ev.Evidence(["Max load: CRN-01 = 10,000 kg", '{"ids":[906,907]}'])
    assert ev.grounding("CRN-01 takes 10,000 kg; see 906 and 907.", e) == ([], [])
    assert ev.grounding("CRN-01 takes 10000 kg.", e) == ([], [])


def test_allow_numbers_is_the_escape_hatch():
    e = ev.Evidence(["nothing"])
    assert ev.grounding("Call 995.", e, allow_numbers=[995]) == ([], [])


def test_paths_into_a_tool_result():
    r = {"data": {"count": 5, "unplaced": [{"incident_id": 906}, {"incident_id": 907}]}}
    assert ev.at_path(r, "data.count") == [5]
    assert ev.at_path(r, "data.unplaced[*].incident_id") == [906, 907]
    assert ev.at_path(r, "data.unplaced[1].incident_id") == [907]
    assert ev.at_path(r, "data.missing") == []


def test_a_tool_value_appears_in_the_answer_as_a_number_not_inside_an_id():
    assert ev.appears_in(10000, "10,000 kg")
    assert ev.appears_in(2, "severity 2")
    assert not ev.appears_in(2, "on CNC-02")
    assert ev.appears_in("SOP-CONFINED-03", "see sop confined 03")


# --------------------------------------------- the severity case, end to end ---

def test_the_severity_case_passes_when_the_guard_strips_the_guess(monkeypatch, tools, capture):
    """Turn 1: the model guesses the scale (as on 5 Nov); the guard strips it, the tool asks.
    Turn 2: the user names the scale; the count comes back with 906 and 907 apart."""
    monkeypatch.setattr(app, "complete", scripted(
        Msg(tool_calls=[Call("c1", "count_incidents",
                             Q1 + ',"severity_scale":"5_is_worst","scale_quote":"5 is worst"}')]),
        Msg(tool_calls=[Call("c2", "count_incidents",
                             Q1 + ',"severity_scale":"5_is_worst","scale_quote":"the current scale"}')]),
        Msg(content="On the current scale, 5 incidents in 2025 were severity 4 or worse. "
                    "Incidents 906 and 907 (July 2025) can't be placed on one scale, so they "
                    "aren't counted.")))
    r = ev.run_case(severity_case(), None, None, capture, oracle_runner=oracle)
    assert r.status == "PASS", r.failures
    assert r.turns[0]["tools"][0]["error_code"] == "needs_clarification"
    assert any(e["event"] == "scale_guess_blocked" for e in r.turns[0]["events"])
    assert r.turns[1]["tools"][0]["arguments"]["severity_scale"] == "5_is_worst"
    assert r.model_calls == 3 and r.prompt_tokens == 3 * 2500


def test_the_severity_case_fails_on_the_5_nov_behaviour(monkeypatch, capture):
    """Without the guard (the tool accepts the guessed scale) the model answers turn 1 with a
    count: the case must fail on turn 1, not pass."""
    def run_tool(db, name, arguments):
        args = json.loads(arguments)
        args.setdefault("severity_scale", "5_is_worst")          # the guess, unchecked
        return count_incidents(args)
    monkeypatch.setattr(app.at, "run_tool", run_tool)
    monkeypatch.setattr(app.at, "tool_schemas", lambda: [])
    monkeypatch.setattr(app, "complete", scripted(
        Msg(tool_calls=[Call("c1", "count_incidents", Q1 + "}")]),
        Msg(content="5 incidents in 2025 were severity 4 or worse."),
        Msg(tool_calls=[Call("c2", "count_incidents", Q1 + "}")]),
        Msg(content="5.")))
    r = ev.run_case(severity_case(), None, None, capture, oracle_runner=oracle)
    assert r.status == "FAIL"
    assert any("turn 1: count_incidents succeeded but must not" in f for f in r.failures)
    assert any("turn 1: expected a clarification question" in f for f in r.failures)


def test_an_invented_count_fails_grounding_and_the_oracle(monkeypatch, tools, capture):
    monkeypatch.setattr(app, "complete", scripted(
        Msg(tool_calls=[Call("c1", "count_incidents", Q1 + "}")]),
        Msg(tool_calls=[Call("c2", "count_incidents",
                             Q1 + ',"severity_scale":"5_is_worst","scale_quote":"the current scale"}')]),
        Msg(content="7 incidents; 906 and 907 are excluded.")))
    r = ev.run_case(severity_case(), None, None, capture, oracle_runner=oracle)
    assert r.status == "FAIL"
    assert "turn 2: not grounded: number 7 is in no tool result" in r.failures
    assert "turn 2: answer lacks 5 (count_incidents data.count)" in r.failures


def test_a_failed_model_call_is_an_error_not_a_verdict(monkeypatch, tools, capture):
    def down(client, model, messages, tools):
        raise app.ChatError("budget exceeded")
    monkeypatch.setattr(app, "complete", down)
    r = ev.run_case(severity_case(), None, None, capture, oracle_runner=oracle)
    assert r.status == "ERROR"
    assert len(r.turns) == 1                       # stopped: no point asking turn 2


def test_a_pinned_value_that_changed_is_reported():
    rec = ev.TurnRecord("q", answer="5", sources=["x"])
    exp = {"oracle": [{"tool": "count_incidents", "args": {}, "equals": {"data.count": 4}}],
           "grounded": False}
    fails, _, _ = ev.check_turn(rec, exp, ev.Evidence([]), oracle_runner=oracle)
    assert any("data.count = [5], expected 4" in f for f in fails)


# ------------------------------------------------------------------ spend ---

def test_the_proxy_cost_header_is_read_per_call():
    class Raw:
        headers = {"x-litellm-response-cost": "0.000412"}

        def parse(self):
            return "parsed"

    class Inner:
        class with_raw_response:
            @staticmethod
            def create(**kw):
                return Raw()

    client = ev.CostTrackingClient(type("Cl", (), {"chat": type("Ch", (), {"completions": Inner})()})())
    assert client.chat.completions.create(model="m") == "parsed"
    assert client.costs == [0.000412]


def test_spend_falls_back_to_list_price_from_tokens():
    assert ev.estimate_usd(1_000_000, 1_000_000, "gpt-4o-mini") == pytest.approx(0.75)
    assert ev.estimate_usd(1, 1, "unknown-model") is None
    rs = [ev.CaseResult("a", "PASS", prompt_tokens=10_000, completion_tokens=500, proxy_cost=None),
          ev.CaseResult("b", "PASS", proxy_cost=0.01)]
    assert ev.spent(rs, "gpt-4o-mini") == pytest.approx(0.0018 + 0.01)