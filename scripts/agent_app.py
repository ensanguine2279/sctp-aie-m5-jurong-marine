#!/usr/bin/env python3
"""The agent's loop: the thing a person actually talks to.

Wires an LLM to `agent_tools.py` with a plain OpenAI-style function-calling loop (no
vector store, no memory beyond this process) and three rules from the harness checklist,
enforced in code rather than hoped for from the prompt. The same turn can also run as a
LangGraph graph (AGENT_ENGINE=graph, agent_graph.py, 11 Nov 2026): its nodes call the
steps defined here (before_model, model_round, tool_round, after_model), so the rules below
hold on either engine and the two give the same answers.

    1. Grounding (#1): every fact in the answer comes from a tool call made just now.
       The model never answers from memory; it can only pick a tool and fill in its
       arguments (agent_tools.py validates them and runs the fixed SQL).
    2. Citations (#2): every tool result's `sources` are collected across the turn and
       shown under the answer, so the user can check the claim themselves.
    3. Honest degradation (#4): a tool error (bad arguments, no data, a down database)
       is never covered up with an invented number - the tool's own message goes back
       to the model, which must say so. A failed LLM call, or too many tool-call rounds,
       produces a plain "I couldn't answer this" rather than a crash or a guess.

    4. Guardrails (#3): `ask()` puts guardrail.py on both sides of the model. A question
       that is a prompt injection, asks for a person's name or personal details (PDPA),
       or is plainly out of scope is refused in code, before any model call; a model
       answer carrying a staff name, an NRIC, a phone number or an e-mail address is
       replaced before the user sees it. Neither leaves the offending text in `messages`,
       so it never reaches a later prompt either.

Multi-turn context (#5) is the `messages` list itself: every earlier question, answer and
tool result goes back to the model with the follow-up. A relative period ("and what about
last month?") is resolved in code: `ask()` puts today's date (Singapore) and the dates the
data runs to into the system prompt (today, data_dates, dates_note), so "last month" is the
calendar's, never the model's guess. A tool asked for a period after the data ends is run
again by the loop for the latest full month the data has (period_check), so the model gets
both results and can't stop at "nothing found". Cross-restart state is kept by the page,
not here: `messages` is a plain list
that agent_ui.py saves after every answer and loads back (scripts/sessions.py). No
human-in-the-loop gate (#6): this app is read-only, so none is needed. The eval suite (#8) is separate:
`scripts/run_evals.py` over `evals/cases.yaml`.

A turn is all or nothing for the conversation (11 Nov 2026): `ask()` works on a copy and
puts it back only when the turn has finished, so a turn cut off half way (the page clicked
while it ran: Streamlit stops the script where it is) leaves `messages` as it was. Before
that, it could leave a tool call with no result, and the chat API then refused every later
question in that conversation (400: "tool_calls must be followed by tool messages"). A
conversation saved like that is mended when it is next asked (answered_only).

Words the data can't take as written are handled in code too (10 Nov 2026): a year written
short ("nov 202") gets a question back, with no model call (incomplete_year, year_question);
"completed" work orders, which the CMMS has no status or date for, are read as status
CLOSED by the date raised, and a note on the tool result makes the answer say so
(completion_note).

Usage
    python scripts/agent_app.py "is CNC-02 running differently now?"
    python scripts/agent_app.py                      # interactive session
    python scripts/agent_app.py --debug "..."         # also prints which tools ran

Needs, in .env (same file as the ingest scripts, plus the llm proxy):
    AGENT_DB_USERNAME / AGENT_DB_PASSWORD       # the read-only agent_ro user
    OPENAI_API_KEY                              # your member key for the llm proxy
    OPENAI_BASE_URL                             # the proxy's URL (https://github.com/ensanguine2279/sctp-aie-m5-jurong-marine-llm-proxy)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_tools as at
import guardrail

MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
# A tool asking for a clarification is passed to the user as-is; the model never gets to
# answer over it (trap C1: a severity number with no scale must be asked, not guessed).
CLARIFY_CODES = ("needs_clarification", "ambiguous_machine")
MAX_TOOL_ROUNDS = 6   # a round is one model call; stops a loop, never a crash (#4)

SYSTEM_PROMPT = """\
You are the maintenance and safety assistant for Jurong Marine & Engineering (JME), a \
ship-repair and precision-engineering shop. You answer from the shop's own data only, \
through the tools you are given.

Rules you must follow:
- Every fact, number or date in your answer must come from a tool call made just now. \
Never answer from memory, never estimate, never round a number the tool didn't give you.
- A severity number needs the scale the USER named. Only set severity_scale if the user \
said which scale (current or old), and copy their exact words into scale_quote. If they \
didn't, leave severity_scale out so the tool asks.
- If a tool's error is needs_clarification or ambiguous_machine, ask the user the exact \
question in its message - do not guess a severity scale or pick a machine for them.
- If a tool's error is no_data or tool_failed, say plainly that you could not check that. \
Never give a confident answer anyway.
- find_sop: quote the SOP exactly as the tool returns it, in full. If `found` is false, \
say no SOP in the data covers this - never adapt a different SOP to fit.
- Use query_sql only when no other tool fits the question, and only to read; never to \
guess what the data "should" say.
- Every note in a tool result is a caveat your answer must carry: say each one in your own \
words (e.g. that a value was read off a scan by a person, or wasn't reliably read). Never drop \
a note to keep the answer short.
- Never fill in a year the user didn't give. If they name a month without a year and the \
data has that month in more than one year, ask which year.
- Work orders have no "completed" status and no completion date: their statuses are CLOSED, \
OPEN and AWAITING PARTS, dated when raised. For "completed" or "finished" work orders, count \
CLOSED ones and say that's how you read it.
- Keep answers short and direct. Do not describe which tool you used unless asked.
"""

log = logging.getLogger("agent_app")
log.addHandler(logging.NullHandler())


def _emit(logger, event: str, **fields) -> None:
    """One JSON line per event, always with a UTC timestamp (#7 observability)."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
           "event": event, **fields}
    logger.info(json.dumps(rec, default=str))


SEVERITY_TOOLS = ("count_incidents", "list_incidents")
# Words that actually name a severity scale; "severity 4 or worse" alone names none.
_SCALE_CUE = re.compile(r"scale|worst|most severe|least severe|highest|lowest|"
                        r"\b(current|new|old|previous|legacy|former)\b")


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def scale_guessed(name: str, arguments, messages) -> bool:
    """True when the model gave a severity number AND a severity_scale the user never
    stated (trap C1: the scale must be asked, never guessed). The model has to quote the
    user's own words naming the scale in `scale_quote`; the quote must really appear in a
    user message and must name a scale."""
    if name not in SEVERITY_TOOLS:
        return False
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except (ValueError, TypeError):
        return False
    if not isinstance(args, dict) or args.get("severity_scale") is None:
        return False
    if args.get("at_least_as_severe_as") is None and args.get("exact_severity") is None:
        return False
    quote = _norm(args.get("scale_quote") or "")
    if not quote or not _SCALE_CUE.search(quote):
        return True
    said = " ".join(_norm(m.get("content") or "") for m in messages if m.get("role") == "user")
    return quote not in said


def _without_scale(arguments) -> str:
    """The same arguments with the guessed scale removed, so the tool itself asks."""
    args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
    args.pop("severity_scale", None)
    args.pop("scale_quote", None)
    return json.dumps(args)


# ================================================ words the data can't take as written ===
# 10 Nov 2026: asked "how many work orders were completed in nov 202", the model read "202"
# as 2026 and "completed" as closed, and said neither. A short year is now asked about in
# code, before any model call, as a severity number with no scale is (trap C1). "Completed"
# has only one possible reading in the CMMS (status CLOSED, dated when raised), so it is
# answered, with a note on the tool result that makes the answer say how it was read.

_MONTH = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_MONTH_NO = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                                         "aug", "sep", "oct", "nov", "dec"), 1)}
# A month, or the word "year", followed by three digits that start a year (19x, 20x) and
# nothing after them: "nov 202", "November 202", "Nov. 202", "year 202". A day after a
# month is at most two digits and a year four, so three digits are neither.
_SHORT_YEAR = re.compile(rf"\b(?:(?P<month>{_MONTH})\.?\s*'?|year\s+)(?P<year>(?:19|20)\d)(?!\d)",
                         re.I)


def incomplete_year(question: str):
    """(the text as written, its month number or None) for a year written short, such as
    'nov 202'; None when the question has none."""
    m = _SHORT_YEAR.search(question or "")
    if not m:
        return None
    month = (m.group("month") or "").lower()[:3]
    return m.group(0).strip(), _MONTH_NO.get(month)


def year_question(found, dates: dict | None = None) -> str:
    """The question sent back for a year written short: the two latest years the data has
    that month in, when the month and the data's end are known."""
    from datetime import date as _date
    text, month = found
    end = max(dates.values()) if dates else None
    if month and end:
        years = [y for y in range(end.year, end.year - 3, -1) if _date(y, month, 1) <= end][:2]
        names = " or ".join(f"{_date(y, month, 1):%B %Y}" for y in years)
        return (f"\"{text}\" isn't a full year, so I won't guess it. Which did you mean: "
                f"{names}? The shop's data runs to {_day(end)}.")
    return f"\"{text}\" isn't a full year, so I won't guess it. Which year did you mean?"


WORK_ORDER_TOOLS = ("list_work_orders",)
_DONE_WORDS = re.compile(r"\b(complet\w*|finish\w*)\b", re.I)
# A question that names a status itself ends the look-back: "and open?" asks about open ones.
_STATUS_WORDS = re.compile(r"\b(open|closed?|awaiting|pending|outstanding|unfinished|"
                           r"incomplete)\b", re.I)
COMPLETED_NOTE = (
    "The user asked about \"{word}\" work orders. The CMMS has no completion date and no "
    "'completed' status: its statuses are CLOSED, OPEN and AWAITING PARTS, and each work "
    "order is dated when it was raised. Count CLOSED work orders as completed, and say both "
    "in the answer: that 'completed' was read as status CLOSED, and that the date is when "
    "each work order was raised, not when it was finished.")


def completion_note(name: str, messages) -> str | None:
    """The note for a work-order tool result when the question is about completed or
    finished work orders; None otherwise. The user's questions are read newest first, so a
    follow-up that names no status ("November 2025", after the year was asked about; "and
    in 2024?") keeps the earlier "completed"; one that names a status itself ("and open?")
    ends the look-back."""
    if name not in WORK_ORDER_TOOLS:
        return None
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        text = str(m.get("content") or "")
        hit = _DONE_WORDS.search(text)
        if hit:
            return COMPLETED_NOTE.format(word=hit.group(1).lower())
        if _STATUS_WORDS.search(text):
            return None
    return None


def _with_note(content: str, note: str) -> str:
    """A tool result for the model with one more note: in its `notes` when it is a JSON
    object, else after it."""
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        body = None
    if isinstance(body, dict):
        body["notes"] = list(body.get("notes") or []) + [note]
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{content}\n{note}"


class ChatError(Exception):
    """A chat-completion call failed: API error, budget cap, network, timeout."""


COST_HEADER = "x-litellm-response-cost"   # what the LiteLLM proxy charged for the call


class Priced:
    """A chat response with the cost the proxy charged for it (USD, or None when the proxy
    didn't say). Reads like the response itself: resp.choices, resp.usage, ..."""

    def __init__(self, resp, cost_usd: float | None):
        self._resp, self.cost_usd = resp, cost_usd

    def __getattr__(self, name):
        return getattr(self._resp, name)


def _cost(headers) -> float | None:
    try:
        v = headers.get(COST_HEADER) if headers is not None else None
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def complete(client, model, messages, tools):
    """One call to the chat API. Wraps SDK/network errors as ChatError, so a failed
    call degrades honestly (#4) instead of raising out of the loop. Returns the response
    with the proxy's cost for it (#7): the SDK's raw-response API gives the headers, and a
    client without it (a stand-in, another provider) just gives cost None."""
    kw = dict(model=model, messages=messages, tools=tools, tool_choice="auto", temperature=0)
    try:
        raw_api = getattr(client.chat.completions, "with_raw_response", None)
        if raw_api is None:
            return Priced(client.chat.completions.create(**kw), None)
        raw = raw_api.create(**kw)
        return Priced(raw.parse(), _cost(getattr(raw, "headers", None)))
    except Exception as e:   # openai.APIError and friends, plus plain network errors
        raise ChatError(str(e)) from e


def _assistant_message(msg) -> dict:
    """The assistant turn, in the shape the next call must send back. Works on a real
    SDK message object or a lightweight stand-in with the same attributes (tests)."""
    out = {"role": "assistant", "content": msg.content}
    calls = getattr(msg, "tool_calls", None)
    if calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    return out


def answered_only(messages) -> tuple[list[dict], int]:
    """(a copy of the conversation the chat API will accept, how many pieces were dropped).

    The API refuses a whole conversation if any tool call in it has no tool message
    answering it, or if a tool message answers no call (400, seen on the page 11 Nov 2026:
    a turn cut off between the model's tool call and the tool's result). So: a tool call
    with no result right after it is dropped, an assistant message left with no calls and
    no text goes with it, and a tool message that answers no call just before it is dropped.
    Everything else is copied as it is (each message a new dict, so the caller's list isn't
    changed until ask() puts the finished turn back)."""
    out, dropped, i, n = [], 0, 0, len(messages or [])
    while i < n:
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") == "tool":
            dropped += 1                           # a result for no call just before it
            i += 1
            continue
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if not calls:
            out.append(dict(m))
            i += 1
            continue
        j, answers = i + 1, {}
        while j < n and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
            cid = messages[j].get("tool_call_id")
            if cid in answers:
                dropped += 1                       # a second result for the same call
            else:
                answers[cid] = messages[j]
            j += 1
        kept = [c for c in calls if isinstance(c, dict) and c.get("id") in answers]
        dropped += len(calls) - len(kept)          # calls nobody answered
        dropped += len(answers) - len(kept)        # results for calls that aren't there
        if kept:
            out.append({**m, "tool_calls": kept})
            out.extend(dict(answers[c["id"]]) for c in kept)
        elif m.get("content"):
            out.append({k: v for k, v in m.items() if k != "tool_calls"})
        i = j
    return out, dropped


def format_notes(notes: list[str]) -> list[str]:
    """Each tool note once, in the order first seen."""
    return list(dict.fromkeys(n for n in notes if n))


def format_sources(sources: list[dict]) -> list[str]:
    """Every source's label, once each, in the order first seen."""
    seen, labels = set(), []
    for s in sources:
        label = (s or {}).get("label")
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def _new_usage() -> dict:
    return {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
            "cost_complete": True}


def _parse_args(arguments) -> dict:
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
    except (ValueError, TypeError):
        return {}
    return a if isinstance(a, dict) else {}


def _span(start, end) -> str:
    return f"{_day(start)} to {_day(end)}" if end else f"from {_day(start)}"


def period_check(db, name, arguments, result, logger=log, round_no=None):
    """(content for the model, extra sources, extra notes) for one tool result, with the
    period it was asked for checked against where the data ends, in code (#4, #5).

    A period that STARTS after the data ends has no data, so the loop runs the same tool
    for the latest full month the data has and gives the model both results, with a note
    to say so and present the second as that month, never as the one asked about. Found on
    the eval run of 10 Nov 2026: asked "and what about last month?", the model queried
    October 2026 (right), got nothing, and answered "no work orders in October": the prompt
    told it to fall back to June, and it didn't. A period that runs PAST the data's end gets
    a note that it covers only up to that day. Anything else is passed through unchanged."""
    plain = result.for_model()
    args = _parse_args(arguments)
    start, stop = _as_date(args.get("date_from")), _as_date(args.get("date_to"))
    dates = data_dates(db) if start else None
    if not dates:
        return plain, [], []
    end = max(dates.values())
    if start <= end:
        if stop and stop > end and getattr(result, "ok", False):
            note = (f"The period asked runs to {_day(stop)}, but the data ends on {_day(end)}: "
                    f"this covers only {_span(start, end)}. Say so in the answer.")
            _emit(logger, "period_past_data", round=round_no, tool=name,
                  asked=_span(start, stop), data_ends=end.isoformat())
            try:
                body = json.loads(plain)
                body["notes"] = list(body.get("notes") or []) + [note]
                return json.dumps(body, ensure_ascii=False, separators=(",", ":")), [], [note]
            except (ValueError, TypeError, AttributeError):
                return plain, [], [note]
        return plain, [], []
    f_from, f_to = latest_full_month(end)
    f_args = {**args, "date_from": f_from.isoformat(), "date_to": f_to.isoformat()}
    fallback = at.run_tool(db, name, f_args)
    month = f"{f_from:%B %Y}"
    note = (f"Nothing in the data for the period asked, {_span(start, stop)}: the shop's data "
            f"runs to {_day(end)}. In the answer, say that first, with those dates. Then give "
            f"the result for {month} ({_span(f_from, f_to)}), the latest full month with data, "
            f"from latest_full_month below, and say that's what it is. Never present it as "
            f"the period asked about.")
    _emit(logger, "period_after_data", round=round_no, tool=name, asked=_span(start, stop),
          data_ends=end.isoformat(), fallback=_span(f_from, f_to),
          fallback_arguments=json.dumps(f_args)[:500], fallback_ok=getattr(fallback, "ok", None))
    try:
        asked_body, fb_body = json.loads(plain), json.loads(fallback.for_model())
    except (ValueError, TypeError):
        asked_body, fb_body = plain, fallback.for_model()
    body = {"tool": name, "ok": getattr(result, "ok", None), "notes": [note],
            "period_asked": {"from": start.isoformat(), "to": stop.isoformat() if stop else None},
            "data_ends": end.isoformat(), "no_data_for_period_asked": True,
            "result_for_period_asked": asked_body,
            "latest_full_month": {"month": month, "from": f_from.isoformat(),
                                  "to": f_to.isoformat(), "arguments": f_args,
                                  "result": fb_body}}
    return (json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str),
            list(getattr(fallback, "sources", None) or []),
            [note] + list(getattr(fallback, "notes", None) or []))


def run_turn(db, client, messages, *, model=MODEL, max_rounds=MAX_TOOL_ROUNDS,
             complete_fn=None, logger=log, notes_out=None, usage_out=None):
    """Run one user turn to completion: call the model, run any tools it picks, feed
    the results back, repeat until it answers in plain text or `max_rounds` is hit.
    Returns (answer_text, sources, tool_names_called). Mutates `messages` in place, so
    the caller's conversation keeps growing turn to turn.

    Never raises: a chat-API failure or an exhausted round budget both return a plain
    English message instead (#4), and the per-tool honesty (never invented, cited,
    clarify-don't-guess) already comes from agent_tools.run_tool, which also never
    raises."""
    sources: list[dict] = []
    tool_names: list[str] = []
    tally = usage_out if usage_out is not None else _new_usage()   # (`usage` is the API's)
    for round_no in range(1, max_rounds + 1):
        msg = model_round(client, messages, round_no, model=model, complete_fn=complete_fn,
                          logger=logger, tally=tally)
        if msg is None:
            return CHAT_FAILED, sources, tool_names
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            return msg.content, sources, tool_names
        clarify = tool_round(db, calls, messages, round_no, logger=logger, sources=sources,
                             tool_names=tool_names, notes_out=notes_out)
        if clarify:
            return clarify, sources, tool_names
    return rounds_exhausted(logger, max_rounds), sources, tool_names


# The steps of a turn, shared by run_turn above and the LangGraph engine (agent_graph.py,
# 11 Nov 2026): each graph node calls one of these, so both engines run the same code and
# can't drift apart. Split out of run_turn and _ask unchanged.

CHAT_FAILED = ("I couldn't reach the language model just now, so I can't answer "
               "this turn. Please try again in a moment.")
ROUNDS_EXHAUSTED = ("I wasn't able to settle on an answer within the allowed number of tool "
                    "calls. Try asking a narrower question.")


def rounds_exhausted(logger, max_rounds) -> str:
    _emit(logger, "max_rounds_exceeded", rounds=max_rounds)
    return ROUNDS_EXHAUSTED


def model_round(client, messages, round_no, *, model=MODEL, complete_fn=None, logger=log,
                tally=None):
    """One model call of a turn: the reply is appended to `messages`, logged (chat_call) and
    added to `tally`. Returns the model's message (its tool calls, if any, in .tool_calls),
    or None when the call failed (chat_error logged; the caller answers CHAT_FAILED)."""
    complete_fn = complete_fn or complete   # looked up here, not at def time, so a
                                             # monkeypatched module-level `complete` is honoured
    tally = tally if tally is not None else _new_usage()
    t0 = time.monotonic()
    try:
        resp = complete_fn(client, model, messages, at.tool_schemas())
    except ChatError as e:
        _emit(logger, "chat_error", round=round_no, model=model, error=str(e))
        return None

    msg = resp.choices[0].message
    usage = getattr(resp, "usage", None)
    messages.append(_assistant_message(msg))
    calls = getattr(msg, "tool_calls", None) or []
    cost = getattr(resp, "cost_usd", None)
    pt, ct = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
    _emit(logger, "chat_call", round=round_no, model=model,
          prompt_tokens=pt, completion_tokens=ct,
          tool_calls=[c.function.name for c in calls],
          cost_usd=cost, ms=round((time.monotonic() - t0) * 1000, 1))
    tally["model_calls"] += 1
    tally["prompt_tokens"] += pt or 0
    tally["completion_tokens"] += ct or 0
    if cost is None:
        tally["cost_complete"] = False         # a call the proxy didn't price
    else:
        tally["cost_usd"] += cost
    return msg


def tool_round(db, calls, messages, round_no, *, logger=log, sources, tool_names,
               notes_out=None) -> str | None:
    """Run every tool call the model made this round, in order, and append one tool message
    per call. Extends `sources`, `tool_names` and `notes_out`. Returns the question to pass
    straight to the user when a tool asked for a clarification (it is appended to
    `messages` as the answer, and the model is not called again), else None."""
    clarify = None
    for call in calls:
        t1 = time.monotonic()
        arguments = call.function.arguments
        if scale_guessed(call.function.name, arguments, messages):
            _emit(logger, "scale_guess_blocked", round=round_no,
                  tool=call.function.name, arguments=str(arguments)[:500])
            arguments = _without_scale(arguments)
        result = at.run_tool(db, call.function.name, arguments)
        err = getattr(result, "error", None)
        _emit(logger, "tool_call", round=round_no, tool=call.function.name,
              arguments=str(arguments)[:500],
              ok=getattr(result, "ok", None),
              error_code=err.get("code") if isinstance(err, dict) else None,
              n_sources=len(result.sources),
              ms=round((time.monotonic() - t1) * 1000, 1))
        tool_names.append(call.function.name)
        if clarify is None and isinstance(err, dict) and err.get("code") in CLARIFY_CODES:
            clarify = err.get("message") or "Could you clarify your question?"
        content, extra_sources, extra_notes = period_check(
            db, call.function.name, arguments, result, logger, round_no)
        done_note = completion_note(call.function.name, messages)
        if done_note:                          # "completed" read as CLOSED, said aloud
            content = _with_note(content, done_note)
            extra_notes = list(extra_notes) + [done_note]
            _emit(logger, "completed_read_as_closed", round=round_no,
                  tool=call.function.name)
        sources.extend(result.sources)
        sources.extend(extra_sources)
        if notes_out is not None:
            notes_out.extend(getattr(result, "notes", None) or [])
            notes_out.extend(extra_notes)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    if clarify:
        _emit(logger, "clarification_returned", round=round_no)
        messages.append({"role": "assistant", "content": clarify})
    return clarify


def _loggable(question: str, rule: str | None) -> str:
    """The question as the log may keep it: a PDPA question can hold a person's name or
    personal details, so only the fact that one was asked is logged."""
    return "[withheld: pdpa]" if rule == "pdpa" else question[:200]


def new_conversation() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


# ============================================================ relative dates ===
# "And what about last month?" (HARNESS_CHECKLIST.md section 5) needs a "today". The model
# has none of its own (it would guess one from its training), so the code gives it one: the
# calendar's date in Singapore, as any assistant on live data would use (10 Nov 2026 chat:
# "follow convention"). The client's data ends earlier (30 Jun 2026), so the model is also
# told where it ends, read from the database, and to say so when a period has no data, then
# answer for the latest full month the data has. The same dates count as evidence in the
# eval suite's grounding check (run_evals.py), which freezes today (AGENT_TODAY).

DATA_SOURCES = (                 # (what, query): the last day each source has data for
    ("incidents", "SELECT MAX(incident_date) AS d FROM incident_detail"),
    ("work orders", "SELECT MAX(wo_date) AS d FROM work_order_detail"),
    ("sensor readings", "SELECT MAX(last_reading) AS d FROM machine_overview"),
)


def _as_date(v):
    from datetime import date as _date
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, _date):
        return v
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v.strip()):
        try:
            return _date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def data_dates(db) -> dict | None:
    """{source: the last day it has data for}, from the views agent_ro reads; None when
    there is no database (tests) or it can't be read. Cached on the Db once read; a failure
    isn't cached, so the next question tries again."""
    if db is None:
        return None
    cache = getattr(db, "cache", None)
    if isinstance(cache, dict) and "data_dates" in cache:
        return cache["data_dates"]
    out = {}
    try:
        for what, sql in DATA_SOURCES:
            rows = db.query(sql)
            d = _as_date(rows[0].get("d")) if rows else None
            if d:
                out[what] = d
    except Exception:                           # down, refused: the note says so (#4)
        return None
    if not out:
        return None
    if isinstance(cache, dict):
        cache["data_dates"] = out
    return out


def _day(d) -> str:
    return f"{d.day} {d:%b %Y}"                 # 30 Jun 2026: the eval's date reader knows it


SHOP_TZ = timezone(timedelta(hours=8))         # Singapore: UTC+8 all year, no DST


def today():
    """Today in Singapore, the shop's time. AGENT_TODAY=YYYY-MM-DD freezes it: the eval
    suite does (run_evals.py, `today:` in cases.yaml), so its expected dates don't move
    with the calendar. Anything else in AGENT_TODAY is ignored."""
    from datetime import date as _date
    fixed = (os.environ.get("AGENT_TODAY") or "").strip()
    if fixed:
        try:
            return _date.fromisoformat(fixed)
        except ValueError:
            pass
    return datetime.now(SHOP_TZ).date()


def relative_periods(today_) -> dict:
    """The periods a question can mean on `today_`, by the calendar: {today, this_month:
    (first, last), last_month: (first, last), this_year, last_year}."""
    nxt = (today_.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_end = today_.replace(day=1) - timedelta(days=1)
    return {"today": today_,
            "this_month": (today_.replace(day=1), nxt - timedelta(days=1)),
            "last_month": (last_end.replace(day=1), last_end),
            "this_year": today_.year, "last_year": today_.year - 1}


def latest_full_month(end) -> tuple:
    """(first, last) of the latest calendar month the data covers to its last day: the
    month of `end` if `end` is its last day, else the month before."""
    if (end + timedelta(days=1)).day == 1:
        return end.replace(day=1), end
    prev_end = end.replace(day=1) - timedelta(days=1)
    return prev_end.replace(day=1), prev_end


def dates_note(dates: dict | None, today_=None) -> str:
    """The system prompt's dates section: today (the calendar's, in Singapore), what "last
    month" and "this year" mean, where the data ends, and what to do when the period asked
    about has no data: say so, then answer for the latest full month the data has, saying
    that's what was done. Without the data's dates, the model is told to ask for exact
    dates rather than assume they're covered."""
    today_ = today_ or today()
    p = relative_periods(today_)
    (ls, le), (ts, te) = p["last_month"], p["this_month"]
    head = (
        "\nDates:\n"
        f"- Today is {_day(today_)} (Singapore). So \"this month\" is {_day(ts)} to {_day(te)}, "
        f"\"last month\" is {_day(ls)} to {_day(le)}, \"this year\" is {p['this_year']}, "
        f"\"last year\" is {p['last_year']}, and \"the past N days, weeks or months\" counts "
        f"back from today.\n")
    tail = (
        "- When a question uses a relative period, put the exact dates in the tool call and "
        "say them in the answer.\n"
        "- A follow-up keeps everything from the earlier question that it doesn't change: the "
        "machine, the period, the severity scale and the filters. \"And what about last "
        "month?\" asks the same question again for last month.\n")
    if not dates:
        return (head + "- Where the shop's data ends couldn't be read just now. If a question "
                "uses a relative period, ask the user for the exact dates; don't assume the "
                "data covers them.\n" + tail)
    end = max(dates.values())
    each = ", ".join(f"{what} to {_day(d)}" for what, d in dates.items())
    lf, ll = latest_full_month(end)
    data = f"- The shop's data runs to {_day(end)} ({each}). Nothing after that is in it.\n"
    if end >= today_:
        return head + data + tail
    return (head + data +
            "- A period that starts after the data ends has no data. Say that plainly, with the "
            f"period's dates and where the data ends, then answer the same question for the "
            f"latest full month the data has, {_day(lf)} to {_day(ll)} ({lf:%B %Y}), and say "
            "that's what you did. Never present it as the period asked about.\n"
            "- A period that runs past the data's end is answered for the part with data, "
            "saying where the data stops.\n" + tail)


def ask(db, client, messages, question, *, model=MODEL, logger=log, raw_sources=None,
        notes=None, usage=None, on_step=None):
    """Append the user's question, run it to completion, return (answer, source
    labels). `messages` is changed in place so the caller can keep the conversation.
    Runs on the engine AGENT_ENGINE names (see engine(); the loop by default). on_step: a
    function called with a small dict after each step of the turn, as it happens (graph
    engine only; the loop doesn't call it). It never gets the question or answer text.
    raw_sources: a list to extend with the tools' source records (agent_ui.py shows the
    photo behind each image source from them). notes: a list to extend with the tools'
    notes, the caveats the page shows under the answer whether or not the model said them.
    usage: a dict to fill with this question's model_calls, prompt_tokens, completion_tokens
    and cost_usd (as the LiteLLM proxy charged it; cost_complete is False if any call came
    back without a cost, so the total is then a lower bound).

    All or nothing (11 Nov 2026): the turn runs on a copy of `messages`, and the copy is put
    back only when the turn has finished. A turn stopped half way (Streamlit stops the
    script when the page is clicked while it runs; a bug that raises) leaves `messages` as
    it was, never with a tool call and no result, which the chat API refuses for the rest
    of the conversation. The copy is also mended first (answered_only), so a conversation
    saved before this fix works again; `history_repaired` is logged when it was needed.

    The guardrail (#3) runs here, on both sides of the model, so every caller (the CLI, the
    page, the eval suite) gets it:
      - input: a refused question gets the guard's own message and no model call. Only a
        placeholder goes into `messages`, so the question (which may hold a name or an
        injection) never reaches a later prompt; the `turn` log line withholds a PDPA
        question's text for the same reason.
      - output: an answer with personal data is replaced, in `messages` too, so the next
        turn's prompt doesn't carry it either.
    After the guard, a year written short ("nov 202") is asked about, also with no model
    call: the question and the reply both go into `messages`, so the user's answer ("2025")
    reaches the model with the question it completes."""
    work, dropped = answered_only(messages)
    if dropped:
        _emit(logger, "history_repaired", dropped=dropped)
    if engine() == "graph" and graph_ready():
        import agent_graph                     # here, not at the top: it imports this module
        answer, labels = agent_graph.run(db, client, work, question, model=model,
                                         logger=logger, raw_sources=raw_sources, notes=notes,
                                         usage=usage, on_step=on_step)
    else:
        if engine() == "graph":                # asked for, not installed: say so, keep going
            _emit(logger, "engine_unavailable", engine="graph", using="loop")
        answer, labels = _ask(db, client, work, question, model=model, logger=logger,
                              raw_sources=raw_sources, notes=notes, usage=usage)
    messages[:] = work                         # the finished turn, all at once
    return answer, labels


# ================================================================== engines ===
# Two ways to run the same turn (11 Nov 2026): "loop" (_ask and run_turn below, the default)
# and "graph" (agent_graph.py, LangGraph). Both call the same steps (before_model,
# model_round, tool_round, after_model), so they give the same answers, messages and log
# events; the graph adds a `graph_step` event per node and can report each step as it
# happens (on_step, used by the API's /ask/stream). AGENT_ENGINE picks one; anything else,
# or "graph" without langgraph installed, runs the loop.

ENGINES = ("loop", "graph")


def engine() -> str:
    v = (os.environ.get("AGENT_ENGINE") or "loop").strip().lower()
    return v if v in ENGINES else "loop"


def graph_ready() -> bool:
    """True when langgraph can be imported (the graph engine needs it; the loop doesn't)."""
    import importlib.util
    try:
        return importlib.util.find_spec("langgraph") is not None
    except (ImportError, ValueError):
        return False


def start_usage(usage: dict | None) -> dict:
    """This question's tally (model calls, tokens, cost): the caller's `usage` dict, emptied
    and filled with zeros, so it is filled even on a refusal; a new dict without one."""
    tally = _new_usage()
    if usage is not None:
        usage.clear()
        usage.update(tally)
        tally = usage
    return tally


def _ask(db, client, messages, question, *, model, logger, raw_sources, notes, usage):
    """ask()'s turn, on the copy ask() passes in: the loop engine."""
    tally = start_usage(usage)
    early = before_model(db, messages, question, logger=logger)
    if early is not None:
        return early, []
    answer, sources, tool_names = run_turn(db, client, messages, model=model, logger=logger,
                                           notes_out=notes, usage_out=tally)
    return after_model(answer, sources, tool_names, messages, question, logger=logger,
                       tally=tally, raw_sources=raw_sources)


def before_model(db, messages, question, *, logger=log) -> str | None:
    """Everything a question goes through before the model is called. Returns the reply when
    it is answered here with no model call (the guard's name list missing, the guardrail
    refusing the question, a year written short), with `messages` and the log updated as
    for any answer. Otherwise None: the system prompt has today's dates and the question is
    appended to `messages`, ready for the model."""
    if not guardrail.names_loaded():           # fail closed: no name list, no answers
        _emit(logger, "guard_not_ready")
        _emit(logger, "turn", question="[withheld: guard not ready]", tools_called=[],
              n_sources=0, refused="not_ready")
        return guardrail.MESSAGES["not_ready"]
    verdict = guardrail.check_input(question)
    if not verdict.allowed:
        messages.append({"role": "user", "content": f"[question withheld by the guardrail: "
                                                    f"{verdict.rule}]"})
        messages.append({"role": "assistant", "content": verdict.message})
        _emit(logger, "input_refused", rule=verdict.rule, reason=verdict.reason)
        _emit(logger, "turn", question=_loggable(question, verdict.rule), tools_called=[],
              n_sources=0, refused=verdict.rule)
        return verdict.message

    short = incomplete_year(question)          # "nov 202": asked, never guessed
    if short:
        reply = year_question(short, data_dates(db))
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": reply})
        _emit(logger, "year_unclear", written=short[0])
        _emit(logger, "turn", question=question[:200], tools_called=[], n_sources=0,
              model_calls=0, cost_usd=0.0)
        return reply

    if db is not None and messages and messages[0].get("role") == "system":
        dates = data_dates(db)                 # what "last month" means (#5), in code
        if dates is None:
            _emit(logger, "dates_unknown")
        messages[0]["content"] = SYSTEM_PROMPT + dates_note(dates)
    messages.append({"role": "user", "content": question})
    return None


def after_model(answer, sources, tool_names, messages, question, *, logger=log, tally,
                raw_sources=None) -> tuple[str, list[str]]:
    """Everything an answer goes through after the model: the output guardrail (an answer
    with personal data replaced, in `messages` too), the `turn` log line, and the sources.
    Returns (answer as shown, source labels)."""
    out = guardrail.check_output(answer or "")
    if not out.allowed:
        _emit(logger, "output_blocked", rule=out.rule, reason=out.reason)   # never the text
        answer = out.message
        for m in reversed(messages):              # the leaked text leaves the history too
            if m.get("role") == "assistant" and m.get("content"):
                m["content"] = answer
                break
    _emit(logger, "turn", question=question[:200],
          tools_called=tool_names, n_sources=len(sources), model_calls=tally["model_calls"],
          cost_usd=round(tally["cost_usd"], 6) if tally["cost_complete"] else None)
    if raw_sources is not None:
        raw_sources.extend(sources)
    return answer, format_sources(sources)


def build_client():
    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("The openai package is needed: pip install openai (or uv add openai).")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("Set OPENAI_API_KEY (your llm proxy member key) in .env. "
                  "See https://github.com/ensanguine2279/sctp-aie-m5-jurong-marine-llm-proxy.")
    return OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None, api_key=api_key)


def _setup_logging(log_file: str | None) -> None:
    log.setLevel(logging.INFO)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file)
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="?",
                    help="Ask one question and exit. Omit for an interactive session.")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--debug", action="store_true",
                    help="Print which tools ran for each question, to stderr.")
    ap.add_argument("--log-file", default="logs/agent_app.jsonl",
                    help="Where the per-call JSON lines go (#7 observability). "
                         "Pass '' to log to stderr instead.")
    a = ap.parse_args()

    _setup_logging(a.log_file or None)

    try:
        guardrail.require_names()              # fail closed, before anything else
    except guardrail.GuardNotReady as e:
        sys.exit(str(e))
    try:
        db = at.connect(a.env_file)
    except Exception as e:
        sys.exit(f"Couldn't connect to the database as the agent user: {e}")
    client = build_client()
    messages = new_conversation()

    def one(question: str) -> None:
        answer, sources = ask(db, client, messages, question, model=a.model)
        print(answer)
        if sources:
            print("\nSources:")
            for s in sources:
                print(f"  - {s}")
        if a.debug:
            print(f"[debug] log: {a.log_file or 'stderr'}", file=sys.stderr)

    if a.question:
        one(a.question)
        return

    print("Jurong Marine maintenance assistant. Type a question, or 'quit' to exit.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not question or question.lower() in ("quit", "exit"):
            break
        one(question)


if __name__ == "__main__":
    main()