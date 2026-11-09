#!/usr/bin/env python3
"""The eval suite runner (HARNESS_CHECKLIST.md section 8): ask the REAL model, through the
REAL loop (agent_app.ask), over the REAL database (as agent_ro), and check every answer.

The cases live in evals/cases.yaml. Each case is one conversation of one or more turns;
each turn checks, from what actually happened (not from what the model says happened):

    tools      which tools ran, which succeeded, which refused and with what code, the
               arguments they ran with (after the severity-scale guard), values in their data
    facts      words, regexes and values the answer must (or must not) contain; an `oracle`
               re-runs a tool directly (no LLM, no cost) and the answer must carry its values
    sources    how many source labels came back, and which ones
    grounding  nothing in the answer that no tool returned: every number, date and
               id-like token (CNC-02, BRG-6310, SOP-CONFINED-03) must appear in a tool result
               of this conversation or in the user's own words
    events     log events that must or must not happen (e.g. scale_guess_blocked)
    calls      how many model calls the turn may make (max_model_calls; 0 = answered in
               code, e.g. a guardrail refusal), counted from the chat_call events
    PDPA       on EVERY turn of every case: no employee's name in the answer. The names are
               read from the CMMS dump (--dump), independently of the app's own guardrail,
               and a failure names the employee code, never the name. A question can carry
               a name as {name:EMP001}, filled in from the dump at run time, so cases.yaml,
               the printed output and the results JSON never hold one

Unit tests (tests/test_*.py) stub the model and the database; this doesn't. A real model
call takes 5-40 s and spends from the key's cap, so the suite is small, every run prints
its spend, and --max-usd stops starting new cases once the run has spent that much.

Usage
    python scripts/run_evals.py --check                 # validate cases.yaml; no calls, free
    python scripts/run_evals.py --list                  # the cases and their tags
    python scripts/run_evals.py                         # the whole suite
    python scripts/run_evals.py --only severity-current-scale,cnc02-running-differently
    python scripts/run_evals.py --tag killshot          # one group
    python scripts/run_evals.py --repeat 3 --tag severity   # same cases 3 times: flaky?

With uv (personal repo): `uv run python scripts/run_evals.py ...`.
Exit code 0 when every case passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import agent_app as app

at = app.at                     # the same agent_tools module the loop uses

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES = ROOT / "evals" / "cases.yaml"
DEFAULT_OUT = ROOT / "evals" / "results"
DEFAULT_LOG = "logs/agent_app.jsonl"     # same file as the app, so read_log.py replays it
DEFAULT_DUMP = ROOT / "data" / "sql" / "jurongmarine_cmms.sql"   # the employee names (PDPA)

# USD per 1M tokens (input, output), list price; checked 7 Nov 2026 (openai.com/api/pricing).
# Only used when the proxy doesn't report its own cost (x-litellm-response-cost header).
PRICES = {"gpt-4o-mini": (0.15, 0.60)}

TURN_KEYS = {
    "tools_all", "tools_any", "tools_none", "tools_ok", "tools_not_ok", "tool_error",
    "tool_args", "tool_data", "answer_contains", "answer_not_contains", "answer_matches",
    "answer_not_matches", "sources_min", "sources_match", "grounded", "allow_numbers",
    "events_present", "events_absent", "clarification", "oracle", "max_model_calls",
}
CASE_KEYS = {"id", "tags", "note", "turns"}


# ================================================================== cases ===

class CaseError(ValueError):
    """cases.yaml is malformed: caught by --check before any money is spent."""


def load_cases(path: Path | str) -> list[dict]:
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list) or not cases:
        raise CaseError(f"{path}: expected a top-level 'cases:' list")
    seen, problems = set(), []
    for i, c in enumerate(cases):
        where = f"case {i + 1} ({c.get('id', '?') if isinstance(c, dict) else '?'})"
        if not isinstance(c, dict) or not c.get("id"):
            problems.append(f"{where}: needs an id")
            continue
        if c["id"] in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(c["id"])
        extra = set(c) - CASE_KEYS
        if extra:
            problems.append(f"{where}: unknown key(s) {sorted(extra)}")
        turns = c.get("turns")
        if not isinstance(turns, list) or not turns:
            problems.append(f"{where}: needs a non-empty 'turns' list")
            continue
        for j, t in enumerate(turns):
            tw = f"{where} turn {j + 1}"
            if not isinstance(t, dict) or not str(t.get("ask") or "").strip():
                problems.append(f"{tw}: needs 'ask'")
                continue
            exp = t.get("expect") or {}
            bad = set(exp) - TURN_KEYS
            if bad:
                problems.append(f"{tw}: unknown expect key(s) {sorted(bad)}")
            for pat in list(exp.get("answer_matches") or []) + list(exp.get("answer_not_matches") or []) \
                    + list(exp.get("sources_match") or []):
                try:
                    re.compile(pat)
                except re.error as e:
                    problems.append(f"{tw}: bad regex {pat!r}: {e}")
            for o in exp.get("oracle") or []:
                if not o.get("tool") or not o.get("must_appear") and not o.get("equals"):
                    problems.append(f"{tw}: an oracle needs 'tool' and 'must_appear' or 'equals'")
            for bad_ph in re.findall(r"\{name:[^}]*\}", str(t["ask"])):
                if not PLACEHOLDER.fullmatch(bad_ph):
                    problems.append(f"{tw}: bad placeholder {bad_ph} (use {{name:EMP001}})")
            mmc = exp.get("max_model_calls")
            if "max_model_calls" in exp and (isinstance(mmc, bool) or not isinstance(mmc, int)
                                             or mmc < 0):
                problems.append(f"{tw}: max_model_calls must be a whole number, 0 or more")
    if problems:
        raise CaseError("\n".join(problems))
    return cases


def select(cases: list[dict], only: str | None = None, tag: str | None = None) -> list[dict]:
    out = cases
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        missing = [w for w in want if w not in {c["id"] for c in cases}]
        if missing:
            raise CaseError(f"No case with id: {', '.join(missing)}")
        out = [c for c in out if c["id"] in want]
    if tag:
        tags = {x.strip() for x in tag.split(",")}
        out = [c for c in out if tags & set(c.get("tags") or [])]
    return out


# ========================================================== text helpers ===

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_RES = [
    ("iso", re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")),
    ("dmy", re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(\d{4})\b")),
    ("mdy", re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")),
    ("slash", re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")),            # day first (trap I8)
    ("my", re.compile(r"\b" + _MON + r",?\s+(\d{4})\b")),                 # month and year
]
# A token with both letters and digits, possibly hyphenated: CNC-02, BRG-6310, VG68, IR-2025-914
ID_RE = re.compile(r"(?<![\w-])(?=[\w-]*[A-Za-z])(?=[\w-]*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![\w-])")
ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b")
NUM_RE = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?(?![\w])")


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _month(name: str) -> int:
    return MONTHS[name[:3].lower()]


def find_dates(text: str) -> tuple[set[str], set[str], list[tuple[int, int, str, str]]]:
    """(full ISO dates, 'YYYY-MM' months, [(start, end, kind, value)]) in `text`."""
    full, months, spans, taken = set(), set(), [], []
    for kind, rx in DATE_RES:
        for m in rx.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            try:
                if kind == "iso":
                    d = date(int(m[1]), int(m[2]), int(m[3]))
                elif kind == "dmy":
                    d = date(int(m[3]), _month(m[2]), int(m[1]))
                elif kind == "mdy":
                    d = date(int(m[3]), _month(m[1]), int(m[2]))
                elif kind == "slash":
                    d = date(int(m[3]), int(m[2]), int(m[1]))
                else:
                    ym = f"{int(m[2]):04d}-{_month(m[1]):02d}"
                    months.add(ym)
                    spans.append((m.start(), m.end(), "month", ym))
                    taken.append((m.start(), m.end()))
                    continue
            except (ValueError, KeyError):
                continue
            full.add(d.isoformat())
            months.add(d.isoformat()[:7])
            spans.append((m.start(), m.end(), "date", d.isoformat()))
            taken.append((m.start(), m.end()))
    return full, months, spans


def _blank(text: str, spans) -> str:
    out = list(text)
    for s, e, *_ in spans:
        out[s:e] = " " * (e - s)
    return "".join(out)


def numbers_in(text: str) -> list[str]:
    """Number tokens as written ('10,000', '91.4', '-0.01'), ignoring ids and dates."""
    _, _, spans = find_dates(text)
    t = _blank(text, spans)
    t = ORDINAL_RE.sub(r"\1", t)
    t = _blank(t, [(m.start(), m.end()) for m in ID_RE.finditer(t)])
    return NUM_RE.findall(t)


def _num(tok: str) -> float:
    return abs(float(tok.replace(",", "").lstrip("+")))


def _decimals(tok: str) -> int:
    return len(tok.split(".")[1]) if "." in tok else 0


class Evidence:
    """Everything a grounded answer may draw on: the tool results of this conversation,
    the source labels, and the user's own messages."""

    def __init__(self, texts: list[str]):
        self.text = "\n".join(t for t in texts if t)
        self.compact = _compact(self.text)
        self.dates, self.months, spans = find_dates(self.text)
        # Numbers as numbers only: not the digits of a date ('07' in 2025-07-11) or of an id
        # ('02' in CNC-02), or a small count would be 'grounded' by accident.
        t = _blank(self.text, spans)
        t = _blank(t, [(m.start(), m.end()) for m in ID_RE.finditer(t)
                       if m.group()[0].isalpha()])          # keep '12' of '12-minute'
        nums = set()
        for tok in re.findall(r"\d+(?:\.\d+)?", t):                  # every digit run
            nums.add(float(tok))
        for tok in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", t):   # 10,000
            nums.add(float(tok.replace(",", "")))
        self.numbers = nums

    def has_number(self, value: float) -> bool:
        return any(math.isclose(value, n, rel_tol=1e-9, abs_tol=1e-9) for n in self.numbers)

    def rounds_to(self, value: float, decimals: int) -> bool:
        return any(math.isclose(round(n, decimals), value, abs_tol=10 ** -(decimals + 3))
                   for n in self.numbers)


def grounding(answer: str, ev: Evidence, allow_numbers=()) -> tuple[list[str], list[str]]:
    """(failures, warnings): claims in the answer that no tool returned and the user didn't
    say. Numbers, full dates, months and id-like tokens are checked; words are not (that's
    what answer_not_contains / answer_not_matches are for). A number that is a tool's number
    rounded is a warning, not a failure: rounded, not invented."""
    fails, warns = [], []
    allowed = {float(a) for a in allow_numbers or []}
    full, months, spans = find_dates(answer or "")
    for _, _, kind, value in spans:
        if kind == "date" and value not in ev.dates:
            fails.append(f"date {value} is in no tool result")
        if kind == "month" and value not in ev.months:
            fails.append(f"month {value} is in no tool result")
    rest = _blank(answer or "", spans)
    for m in ID_RE.finditer(ORDINAL_RE.sub(r"\1   ", rest)):
        tok = m.group()
        if _compact(tok) not in ev.compact:
            fails.append(f"'{tok}' is in no tool result")
    for tok in numbers_in(answer or ""):
        v = _num(tok)
        if v in allowed or ev.has_number(v):
            continue
        d = _decimals(tok)
        if ev.rounds_to(v, d):
            warns.append(f"{tok} is a tool's number rounded")
        else:
            fails.append(f"number {tok} is in no tool result")
    return list(dict.fromkeys(fails)), list(dict.fromkeys(warns))


def at_path(obj, path: str) -> list:
    """Values at a dotted path, '[*]' for every item of a list: data.unplaced[*].incident_id"""
    parts = re.findall(r"\[\*\]|\[-?\d+\]|[^.\[\]]+", path)
    vals = [obj]
    for p in parts:
        nxt = []
        for v in vals:
            if p == "[*]":
                nxt += v if isinstance(v, list) else []
            elif p.startswith("["):
                i = int(p[1:-1])
                if isinstance(v, list) and -len(v) <= i < len(v):
                    nxt.append(v[i])
            elif isinstance(v, dict) and p in v:
                nxt.append(v[p])
        vals = nxt
    return vals


def same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
        return a is b or a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)
    return str(a).strip().lower() == str(b).strip().lower()


def appears_in(value, answer: str) -> bool:
    """A tool's value is in the answer: numbers as numbers (10000 == '10,000'), text
    ignoring case, spaces and punctuation."""
    if value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return any(math.isclose(_num(t), abs(float(value)), abs_tol=1e-9)
                   for t in numbers_in(answer or ""))
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value in find_dates(answer or "")[0]
    return _compact(value) in _compact(answer)


# ===================================================== employee names (PDPA) ===
# The eval suite's own copy of the rule "no employee name ever reaches an answer" (trap
# list C16). Deliberately NOT the app's guardrail: a check that reused the guard's matcher
# would only prove the guard agrees with itself. This one reads the names straight from
# the CMMS dump, as written, and looks for them in every answer.

PLACEHOLDER = re.compile(r"\{name:(EMP\d{3})\}")
_EMP_INSERT = re.compile(r"INSERT INTO `employees`[^;]*?VALUES(.*?\));", re.S)
_EMP_ROW = re.compile(r"\(\s*\d+\s*,\s*'(EMP\d+)'\s*,\s*'((?:[^']|'')*)'")


def employee_names(dump_text: str) -> dict[str, str]:
    """{emp_code: name} from the dump's employees INSERT ('' is an escaped quote)."""
    out = {}
    for block in _EMP_INSERT.findall(dump_text):
        for code, name in _EMP_ROW.findall(block):
            out[code] = name.replace("''", "'")
    return out


def load_employee_names(path: Path | str | None) -> dict[str, str] | None:
    """The names, or None when there is no dump to read them from (the check is then
    skipped, with a warning on every case, never silently)."""
    p = Path(path) if path else None
    if not p or not p.is_file():
        return None
    return employee_names(p.read_text(encoding="utf-8", errors="replace")) or None


def _name_words(text: str) -> list[str]:
    """Lower-case words, apostrophes dropped: "Marcus D'Silva" -> ['marcus', 'dsilva']."""
    return re.findall(r"[a-z]+", re.sub(r"['’]", "", str(text).lower()))


def _name_phrases(name: str) -> set[str]:
    """Every run of two or more words of a name: 'aminah binte', 'binte yusof', ...
    A single word on its own (a surname like Low, Ho, Ng) is too common to test for."""
    w = _name_words(name)
    return {" ".join(w[i:j]) for i in range(len(w)) for j in range(i + 2, len(w) + 1)}


def names_in(answer: str, names: dict[str, str]) -> list[str]:
    """The employee codes whose name (or two or more consecutive words of it) is in the
    answer, in code order. Codes, not names, so a failure report doesn't leak the name."""
    words = f" {' '.join(_name_words(answer))} "
    return sorted(code for code, name in names.items()
                  if any(f" {p} " in words for p in _name_phrases(name)))


def mask_names(text: str, names: dict[str, str] | None) -> str:
    """The text with every employee name phrase replaced by [name], for what the runner
    prints and saves (latest.txt is committed)."""
    if not names or not text:
        return text
    for p in sorted({p for n in names.values() for p in _name_phrases(n)}, key=len, reverse=True):
        rx = r"\b" + r"[\s'’]*".join(
            r"['’]?".join(map(re.escape, w)) for w in p.split()) + r"\b"
        text = re.sub(rx, "[name]", text, flags=re.I)
    return text


def fill_names(ask: str, names: dict[str, str] | None) -> str:
    """{name:EMP001} -> that employee's name from the dump. CaseError if it can't."""
    codes = PLACEHOLDER.findall(ask)
    if not codes:
        return ask
    if names is None:
        raise CaseError("this question needs the CMMS dump (--dump) to fill in "
                        + ", ".join(f"{{name:{c}}}" for c in codes))
    missing = [c for c in codes if c not in names]
    if missing:
        raise CaseError(f"no employee {', '.join(missing)} in the CMMS dump")
    return PLACEHOLDER.sub(lambda m: names[m[1]], ask)


# ========================================================== running a turn ===

@dataclass
class ToolRun:
    name: str
    arguments: dict | str          # as RUN (after the severity-scale guard), from the log
    sent: dict | str | None        # as the model sent them
    ok: bool | None
    error_code: str | None
    result: dict                   # the tool's result as the model saw it (for_model())


@dataclass
class TurnRecord:
    question: str
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tools: list[ToolRun] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    proxy_cost: float | None = None       # summed x-litellm-response-cost, if every call had it
    model_calls: int = 0


class EventCapture(logging.Handler):
    """Keeps the agent_app logger's JSON events in memory, to check them per turn."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.events: list[dict] = []

    def emit(self, record):
        try:
            self.events.append(json.loads(record.getMessage()))
        except ValueError:
            pass


class _RawCompletions:
    """with_raw_response, passed through and recorded: agent_app.complete() reads the cost
    header itself (for its log and the page), and the runner records the same call."""

    def __init__(self, inner_raw, costs: list):
        self._inner_raw, self._costs = inner_raw, costs

    def create(self, **kw):
        raw = self._inner_raw.create(**kw)
        h = raw.headers.get("x-litellm-response-cost")
        try:
            self._costs.append(float(h) if h is not None else None)
        except ValueError:
            self._costs.append(None)
        return raw


class _Completions:
    def __init__(self, inner, costs: list):
        self._inner, self._costs = inner, costs

    @property
    def with_raw_response(self):
        raw_api = getattr(self._inner, "with_raw_response", None)
        return None if raw_api is None else _RawCompletions(raw_api, self._costs)

    def create(self, **kw):
        raw_api = getattr(self._inner, "with_raw_response", None)
        if raw_api is None:
            self._costs.append(None)
            return self._inner.create(**kw)
        raw = raw_api.create(**kw)
        h = raw.headers.get("x-litellm-response-cost")
        try:
            self._costs.append(float(h) if h is not None else None)
        except ValueError:
            self._costs.append(None)
        return raw.parse()


class CostTrackingClient:
    """Wraps an OpenAI client so each chat call records the cost the LiteLLM proxy says it
    charged (x-litellm-response-cost, see llm-budget-proxy-setup.md). The loop only uses
    client.chat.completions.create, so that's all this provides."""

    def __init__(self, client):
        self.costs: list[float | None] = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(client.chat.completions, self.costs)


def _parse(s):
    try:
        return json.loads(s) if isinstance(s, str) else s
    except ValueError:
        return s


def run_turn_record(db, client, messages, question, capture: EventCapture,
                    model: str = app.MODEL) -> TurnRecord:
    rec = TurnRecord(question)
    first_msg, first_ev = len(messages), len(capture.events)
    costs = getattr(client, "costs", None)
    first_cost = len(costs) if costs is not None else 0
    raw_sources, notes = [], []
    t0 = time.monotonic()
    answer, labels = app.ask(db, client, messages, question, model=model,
                             raw_sources=raw_sources, notes=notes)
    rec.seconds = round(time.monotonic() - t0, 1)
    rec.answer, rec.sources, rec.notes = answer or "", labels, app.format_notes(notes)
    rec.events = capture.events[first_ev:]
    new = messages[first_msg:]
    sent = {}
    for m in new:
        for c in m.get("tool_calls") or []:
            sent[c["id"]] = c["function"]["arguments"]
    results = [m for m in new if m.get("role") == "tool"]
    calls = [e for e in rec.events if e.get("event") == "tool_call"]
    for e, m in zip(calls, results):
        rec.tools.append(ToolRun(e.get("tool"), _parse(e.get("arguments")),
                                 _parse(sent.get(m.get("tool_call_id"))), e.get("ok"),
                                 e.get("error_code"), _parse(m.get("content")) or {}))
    chats = [e for e in rec.events if e.get("event") == "chat_call"]
    rec.model_calls = len(chats)
    rec.prompt_tokens = sum(e.get("prompt_tokens") or 0 for e in chats)
    rec.completion_tokens = sum(e.get("completion_tokens") or 0 for e in chats)
    if costs is not None:
        mine = costs[first_cost:]
        rec.proxy_cost = sum(mine) if mine and all(c is not None for c in mine) else None
    return rec


# ================================================================== checks ===

def _as_list(x):
    return x if isinstance(x, list) else [] if x is None else [x]


def evidence_for(messages: list[dict]) -> Evidence:
    texts = []
    for m in messages:
        if m.get("role") in ("tool", "user"):
            texts.append(str(m.get("content") or ""))
    return Evidence(texts)


def check_turn(rec: TurnRecord, expect: dict, ev: Evidence, db=None,
               oracle_runner=None, employees: dict[str, str] | None = None
               ) -> tuple[list[str], list[str], dict]:
    """(failures, warnings, oracle values) for one turn. `employees` ({code: name}, from
    the dump) switches on the PDPA check, which runs on every turn whatever `expect` says."""
    f, w, oracle_seen = [], [], {}
    expect = expect or {}
    names = [t.name for t in rec.tools]
    ok_names = {t.name for t in rec.tools if t.ok}
    answer = rec.answer or ""

    if employees:
        leaked = names_in(answer, employees)
        if leaked:
            f.append(f"PDPA: an employee's name is in the answer ({', '.join(leaked)})")

    if any(e.get("event") == "chat_error" for e in rec.events):
        f.append("the model call failed (chat_error): see the log")
    if any(e.get("event") == "max_rounds_exceeded" for e in rec.events):
        f.append("stopped at MAX_TOOL_ROUNDS")
    if "max_model_calls" in expect:
        # Counted from the log, not from what the answer says: every chat_call event, plus
        # every chat_error (a call that was attempted and failed is still a call the code
        # let through). max_model_calls: 0 = the turn must be settled in code, no LLM.
        tried = sum(1 for e in rec.events if e.get("event") in ("chat_call", "chat_error"))
        if tried > expect["max_model_calls"]:
            f.append(f"{tried} model call(s), at most {expect['max_model_calls']} allowed")

    for t in _as_list(expect.get("tools_all")):
        if t not in names:
            f.append(f"tool {t} did not run (ran: {names or 'none'})")
    anyof = _as_list(expect.get("tools_any"))
    if anyof and not set(anyof) & set(names):
        f.append(f"none of {anyof} ran (ran: {names or 'none'})")
    for t in _as_list(expect.get("tools_none")):
        if t in names:
            f.append(f"tool {t} ran but must not")
    for t in _as_list(expect.get("tools_ok")):
        if t not in ok_names:
            f.append(f"no successful {t} call (ran: {_tool_summary(rec)})")
    for t in _as_list(expect.get("tools_not_ok")):
        if t in ok_names:
            f.append(f"{t} succeeded but must not (a number was computed: {_tool_summary(rec)})")
    for t, code in (expect.get("tool_error") or {}).items():
        if not any(x.name == t and x.error_code == code for x in rec.tools):
            f.append(f"{t} did not refuse with {code} (ran: {_tool_summary(rec)})")
    for spec in _as_list(expect.get("tool_args")):
        runs = [x for x in rec.tools if x.name == spec["tool"] and x.ok]
        want = spec.get("args") or {}
        if not any(isinstance(x.arguments, dict)
                   and all(same(x.arguments.get(k), v) for k, v in want.items()) for x in runs):
            got = [x.arguments for x in runs] or "no successful call"
            f.append(f"{spec['tool']} never ran with {want} (ran with: {got})")
    for spec in _as_list(expect.get("tool_data")):
        f += _check_values([x.result for x in rec.tools if x.name == spec["tool"]], spec,
                           f"{spec['tool']} {spec['path']}")

    for item in _as_list(expect.get("answer_contains")):
        options = _as_list(item)
        if not any(str(o).lower() in answer.lower() for o in options):
            f.append(f"answer lacks {' or '.join(repr(str(o)) for o in options)}")
    for item in _as_list(expect.get("answer_not_contains")):
        if str(item).lower() in answer.lower():
            f.append(f"answer contains {str(item)!r}")
    for pat in _as_list(expect.get("answer_matches")):
        if not re.search(pat, answer, re.I | re.S):
            f.append(f"answer doesn't match /{pat}/")
    for pat in _as_list(expect.get("answer_not_matches")):
        m = re.search(pat, answer, re.I | re.S)
        if m:
            f.append(f"answer matches forbidden /{pat}/: {m.group()!r}")

    clarification = expect.get("clarification")
    if clarification:
        if any(e.get("event") == "clarification_returned" for e in rec.events):
            pass
        elif not ok_names & {"count_incidents", "list_incidents"} and "?" in answer:
            w.append("the model asked the question itself; no tool refused")
        else:
            f.append("expected a clarification question, got an answer")

    smin = expect.get("sources_min", 1)
    if len(rec.sources) < smin:
        f.append(f"{len(rec.sources)} source(s), expected at least {smin}")
    for pat in _as_list(expect.get("sources_match")):
        if not any(re.search(pat, s, re.I) for s in rec.sources):
            f.append(f"no source matches /{pat}/ (sources: {rec.sources or 'none'})")

    names_ev = {e.get("event") for e in rec.events}
    for e in _as_list(expect.get("events_present")):
        if e not in names_ev:
            f.append(f"event {e} not logged")
    for e in _as_list(expect.get("events_absent")):
        if e in names_ev:
            f.append(f"event {e} logged")

    if expect.get("grounded", True):
        gf, gw = grounding(answer, ev, expect.get("allow_numbers") or [])
        f += [f"not grounded: {x}" for x in gf]
        w += gw

    for o in _as_list(expect.get("oracle")):
        if oracle_runner is None:
            w.append(f"oracle {o['tool']} skipped (no database)")
            continue
        res = oracle_runner(o["tool"], o.get("args") or {})
        if not res.get("ok"):
            f.append(f"oracle {o['tool']} failed: {res.get('error')}")
            continue
        for path in _as_list(o.get("must_appear")):
            vals = at_path(res, path)
            oracle_seen[f"{o['tool']}.{path}"] = vals
            if not vals:
                f.append(f"oracle {o['tool']} has no value at {path}")
            for v in vals:
                if not appears_in(v, answer):
                    f.append(f"answer lacks {v!r} ({o['tool']} {path})")
        for path, want in (o.get("equals") or {}).items():
            vals = at_path(res, path)
            oracle_seen[f"{o['tool']}.{path}"] = vals
            if not vals or not all(same(v, want) for v in vals):
                f.append(f"oracle {o['tool']} {path} = {vals}, expected {want!r} "
                         f"(the data changed, or the pinned value is wrong)")
    return f, w, oracle_seen


def _check_values(results: list[dict], spec: dict, label: str) -> list[str]:
    if not results:
        return [f"{label}: the tool never ran"]
    vals = [v for r in results for v in at_path(r, spec["path"])]
    if "equals" in spec:
        if not any(same(v, spec["equals"]) for v in vals):
            return [f"{label} = {vals}, expected {spec['equals']!r}"]
    if "in" in spec:
        if not any(any(same(v, x) for x in spec["in"]) for v in vals):
            return [f"{label} = {vals}, expected one of {spec['in']!r}"]
    if "contains" in spec:
        flat = [x for v in vals for x in (v if isinstance(v, list) else [v])]
        for want in _as_list(spec["contains"]):
            if not any(same(x, want) for x in flat):
                return [f"{label} = {vals}, expected it to include {want!r}"]
    if "exists" in spec and bool(vals) != bool(spec["exists"]):
        return [f"{label}: {'missing' if spec['exists'] else 'present'}"]
    return []


def _tool_summary(rec: TurnRecord) -> str:
    return ", ".join(f"{t.name}:{'ok' if t.ok else t.error_code}" for t in rec.tools) or "none"


# ============================================================ running cases ===

@dataclass
class CaseResult:
    id: str
    status: str                       # PASS, FAIL, ERROR, SKIPPED
    turns: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    proxy_cost: float | None = 0.0
    model_calls: int = 0


def estimate_usd(prompt: int, completion: int, model: str, prices=None) -> float | None:
    p = (prices or PRICES).get(model)
    return None if p is None else (prompt * p[0] + completion * p[1]) / 1e6


def run_case(case: dict, db, client, capture: EventCapture, *, model: str = app.MODEL,
             oracle_runner=None, employees: dict[str, str] | None = None) -> CaseResult:
    res = CaseResult(case["id"], "PASS")
    messages = app.new_conversation()
    proxy_costs = []
    if employees is None:
        res.warnings.append("PDPA employee-name check skipped: no CMMS dump to read names "
                            "from (--dump)")
    for i, turn in enumerate(case["turns"], 1):
        app._emit(app.log, "eval_case", case=case["id"], turn=i)
        try:
            question = fill_names(turn["ask"], employees)
        except CaseError as e:
            res.status = "ERROR"
            res.failures.append(f"turn {i}: {e}")
            break
        try:
            rec = run_turn_record(db, client, messages, question, capture, model)
        except Exception as e:                       # a bug, not a wrong answer
            res.status = "ERROR"
            res.failures.append(f"turn {i}: {type(e).__name__}: {e}")
            break
        rec.question = turn["ask"]                   # as written: {name:EMP001}, never the name
        f, w, oracle = check_turn(rec, turn.get("expect") or {}, evidence_for(messages),
                                  db, oracle_runner, employees)
        res.failures += [f"turn {i}: {x}" for x in f]
        res.warnings += [f"turn {i}: {x}" for x in w]
        res.seconds += rec.seconds
        res.prompt_tokens += rec.prompt_tokens
        res.completion_tokens += rec.completion_tokens
        res.model_calls += rec.model_calls
        proxy_costs.append(rec.proxy_cost)
        d = asdict(rec)
        if employees:                                # what gets printed and saved: answer,
            d = json.loads(mask_names(json.dumps(d, default=str), employees))   # events, all
        d["oracle"] = oracle
        d["failures"], d["warnings"] = f, w
        res.turns.append(d)
        if any(e.get("event") == "chat_error" for e in rec.events):
            res.status = "ERROR"                     # the model was unreachable: not a verdict
            break
    res.seconds = round(res.seconds, 1)
    res.proxy_cost = sum(proxy_costs) if proxy_costs and all(c is not None for c in proxy_costs) else None
    if res.status == "PASS" and res.failures:
        res.status = "FAIL"
    return res


def make_oracle(db):
    cache = {}

    def run(tool: str, args: dict) -> dict:
        key = (tool, json.dumps(args, sort_keys=True))
        if key not in cache:
            cache[key] = json.loads(at.run_tool(db, tool, args).for_model())
        return cache[key]
    return run


# ===================================================================== CLI ===

def _short(text: str, n: int = 160) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def print_case(r: CaseResult, out, model: str = app.MODEL) -> None:
    est = estimate_usd(r.prompt_tokens, r.completion_tokens, model)
    cost = (f"${r.proxy_cost:.4f}" if r.proxy_cost is not None else
            f"~${est:.4f} list" if est is not None else "cost n/a")
    print(f"{r.status:7} {r.id:38} {r.seconds:6.1f} s  {r.model_calls} calls  "
          f"{r.prompt_tokens}+{r.completion_tokens} tok  {cost}", file=out)
    for t in r.turns:
        tools = ", ".join(f"{x['name']}:{'ok' if x['ok'] else x['error_code']}" for x in t["tools"])
        print(f"          Q: {_short(t['question'], 90)}", file=out)
        print(f"          tools: {tools or 'none'} | sources: {len(t['sources'])}", file=out)
        for k, v in (t.get("oracle") or {}).items():
            print(f"          oracle {k} = {v}", file=out)
        if r.status != "PASS":
            print(f"          A: {_short(t['answer'], 300)}", file=out)
    for x in r.failures:
        print(f"          FAIL {x}", file=out)
    for x in r.warnings:
        print(f"          warn {x}", file=out)


def summary(results: list[CaseResult], model: str, wall: float, prices=None) -> str:
    n = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "ERROR", "SKIPPED")}
    ran = [r for r in results if r.status != "SKIPPED"]
    pt = sum(r.prompt_tokens for r in ran)
    ct = sum(r.completion_tokens for r in ran)
    calls = sum(r.model_calls for r in ran)
    est = estimate_usd(pt, ct, model, prices)
    proxy = [r.proxy_cost for r in ran]
    proxy_total = sum(proxy) if proxy and all(c is not None for c in proxy) else None
    lines = [
        f"Cases: {len(results)}  passed {n['PASS']}  failed {n['FAIL']}  errors {n['ERROR']}"
        f"  skipped {n['SKIPPED']}",
        f"Model calls: {calls} ({model})  tokens: {pt} in + {ct} out  time: {wall:.0f} s",
        "Spend: " + (f"${proxy_total:.4f} as charged by the proxy (x-litellm-response-cost)"
                     if proxy_total is not None else "proxy cost not reported")
        + (f"; ${est:.4f} at list price" if est is not None else ""),
    ]
    return "\n".join(lines)


def spent(results: list[CaseResult], model: str, prices=None) -> float:
    total = 0.0
    for r in results:
        if r.proxy_cost is not None:
            total += r.proxy_cost
        else:
            total += estimate_usd(r.prompt_tokens, r.completion_tokens, model, prices) or 0.0
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--tag", help="comma-separated tags: run cases with any of them")
    ap.add_argument("--list", action="store_true", help="list the cases and exit (free)")
    ap.add_argument("--check", action="store_true", help="validate cases.yaml and exit (free)")
    ap.add_argument("--repeat", type=int, default=1, help="run each case N times (flakiness)")
    ap.add_argument("--max-usd", type=float, default=0.50,
                    help="stop starting new cases once the run has spent this much (default 0.50)")
    ap.add_argument("--model", default=app.MODEL)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--log-file", default=DEFAULT_LOG,
                    help="the agent's JSON events (same file as the app); '' for none")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT),
                    help="where the run's JSON and latest.txt go")
    ap.add_argument("--dump", default=str(DEFAULT_DUMP),
                    help="the CMMS dump, read only for employee names: every answer is checked "
                         "for them (PDPA), and {name:EMP001} in a question is filled in")
    a = ap.parse_args(argv)

    try:
        cases = select(load_cases(a.cases), a.only, a.tag)
    except CaseError as e:
        print(f"cases.yaml has problems:\n{e}", file=sys.stderr)
        return 2
    if a.list or a.check:
        for c in cases:
            print(f"{c['id']:38} {len(c['turns'])} turn(s)  [{', '.join(c.get('tags') or [])}]")
        print(f"{len(cases)} case(s), {sum(len(c['turns']) for c in cases)} turn(s): OK")
        return 0
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 2

    try:
        app.guardrail.require_names()          # fail closed: no point running, or paying for,
                                               # a suite where every answer is the not-ready refusal
    except app.guardrail.GuardNotReady as e:
        print(str(e), file=sys.stderr)
        return 2

    capture = EventCapture()
    app.log.setLevel(logging.INFO)
    app.log.addHandler(capture)
    if a.log_file:
        Path(a.log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(a.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        app.log.addHandler(fh)

    try:
        db = at.connect(a.env_file)
    except Exception as e:
        print(f"Couldn't connect to the database as the agent user: {e}", file=sys.stderr)
        return 2
    client = CostTrackingClient(app.build_client())
    oracle = make_oracle(db)
    employees = load_employee_names(a.dump)
    if employees is None:
        print(f"Warning: no employee names read from {a.dump}: the PDPA check is skipped and "
              f"cases with {{name:...}} will be ERROR.", file=sys.stderr)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_lines = []

    class Tee:
        def write(self, s):
            sys.stdout.write(s)
            text_lines.append(s)

        def flush(self):
            sys.stdout.flush()

    tee = Tee()
    print(f"Eval run {stamp}: {len(cases)} case(s) x {a.repeat}, model {a.model}, "
          f"stop at ${a.max_usd:g}\n", file=tee)
    results: list[CaseResult] = []
    t0 = time.monotonic()
    for rep in range(a.repeat):
        for c in cases:
            if spent(results, a.model) >= a.max_usd:
                results.append(CaseResult(c["id"], "SKIPPED",
                                          failures=[f"--max-usd {a.max_usd} reached"]))
                print(f"SKIPPED {c['id']} (spend cap ${a.max_usd:g} reached)", file=tee)
                continue
            r = run_case(c, db, client, capture, model=a.model, oracle_runner=oracle,
                         employees=employees)
            results.append(r)
            print_case(r, tee, a.model)
    wall = time.monotonic() - t0
    s = summary(results, a.model, wall)
    print("\n" + s, file=tee)

    payload = {"run": stamp, "model": a.model, "cases_file": a.cases, "repeat": a.repeat,
               "summary": s, "results": [asdict(r) for r in results]}
    (out_dir / f"{stamp}.json").write_text(json.dumps(payload, indent=2, default=str,
                                                      ensure_ascii=False), encoding="utf-8")
    (out_dir / "latest.txt").write_text("".join(text_lines), encoding="utf-8")
    print(f"\nSaved: {out_dir / (stamp + '.json')} and {out_dir / 'latest.txt'}", file=sys.stdout)
    return 0 if all(r.status == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())