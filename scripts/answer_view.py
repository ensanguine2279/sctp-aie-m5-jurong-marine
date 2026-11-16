#!/usr/bin/env python3
"""How an answer is shown and kept: the pieces the API builds its replies and saved
conversations from, and the page reads back.

Until 15 Nov 2026 these lived in agent_ui.py (the Streamlit page) and agent_api.py imported
the page to use them. Since phase 3 the page talks only to the API, so the dependency runs
one way: the API (server side) builds every answer with these, and the page only draws what
the API sends. Nothing here needs Streamlit, the database or the model; only kept_question()
loads the guardrail, and only when it is called (server side).

    trace_logger, TraceHandler   one logger per question, collecting agent_app's JSON events
    trace_rows                   those events as the "What happened" table
    cost_line                    one answer's cost, under the answer
    image_refs                   the scans an answer cites (sha256, what, file), for its photos
    display_copy, kept_question  what is saved of a conversation's display (no photos, no
                                 trace; a refused question only as its placeholder)
    conversation_spend, _line    a conversation's total, added up from its answers
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from pathlib import Path

RECENT = 8                       # saved conversations a front end lists (the page's sidebar)


# ===================================================================== trace ===

class TraceHandler(logging.Handler):
    """Collects the JSON events agent_app logs during one question."""

    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    def emit(self, record):
        try:
            self.events.append(json.loads(record.getMessage()))
        except ValueError:
            pass


# The same file agent_app.py writes. AGENT_LOG_FILE=off turns the file off (a hosted
# container's disk is temporary; its stderr goes to the platform's logs instead).
LOG_FILE = os.environ.get("AGENT_LOG_FILE", "logs/agent_app.jsonl")
_file_handler: logging.Handler | None = None
_stderr_handler: logging.Handler | None = None


def _shared_file_handler() -> logging.Handler:
    """One file handler for the whole process (not one per question)."""
    global _file_handler
    if _file_handler is None:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(LOG_FILE)
        _file_handler.setFormatter(logging.Formatter("%(message)s"))
    return _file_handler


def trace_logger(log_to_file: bool = False) -> tuple[logging.Logger, TraceHandler]:
    """A logger for one question: feeds the What happened table, and with `log_to_file`
    also writes the same JSON lines to stderr (Cloud Logging reads it) and to LOG_FILE."""
    handler = TraceHandler()
    lg = logging.getLogger(f"answer_view.{id(handler)}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(handler)
    if log_to_file:
        global _stderr_handler
        if _stderr_handler is None:
            _stderr_handler = logging.StreamHandler(sys.stderr)
            _stderr_handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(_stderr_handler)
        if LOG_FILE and LOG_FILE.lower() != "off":
            lg.addHandler(_shared_file_handler())
    return lg, handler


def trace_rows(events: list[dict]) -> list[dict]:
    """One table row per model call or tool call, in the order they happened."""
    rows = []
    for e in events:
        kind = e.get("event")
        if kind == "chat_call":
            tools = ", ".join(e.get("tool_calls") or []) or "(answered)"
            cost = e.get("cost_usd")
            rows.append({"step": f"model, round {e['round']}",
                         "detail": f"asked for: {tools}"
                                   + (f" (${cost:.4f})" if cost is not None else ""),
                         "seconds": round(e["ms"] / 1000, 1)})
        elif kind == "tool_call":
            state = "ok" if e.get("ok") else f"refused: {e.get('error_code')}"
            rows.append({"step": f"tool, round {e['round']}",
                         "detail": f"{e['tool']} {e['arguments']} -> {state}",
                         "seconds": round(e["ms"] / 1000, 2)})
        elif kind == "chat_error":
            rows.append({"step": f"model, round {e['round']}", "detail": f"failed: {e['error']}",
                         "seconds": None})
        elif kind == "clarification_returned":
            rows.append({"step": "app", "detail": "tool asked a question; passed to the user",
                         "seconds": None})
        elif kind == "max_rounds_exceeded":
            rows.append({"step": "app", "detail": "stopped: too many tool calls", "seconds": None})
        elif kind == "input_refused":
            rows.append({"step": "guardrail", "seconds": None,
                         "detail": f"question refused in code ({e.get('rule')}: "
                                   f"{e.get('reason')}); the model was not called"})
        elif kind == "guard_not_ready":
            rows.append({"step": "guardrail", "seconds": None,
                         "detail": "refused: the staff name list isn't loaded (fail closed)"})
        elif kind == "year_unclear":
            rows.append({"step": "app", "seconds": None,
                         "detail": f"year written short ({e.get('written')}): asked which "
                                   f"year; the model was not called"})
        elif kind == "completed_read_as_closed":
            rows.append({"step": "app", "seconds": None,
                         "detail": "'completed' read as status CLOSED (the CMMS has no "
                                   "completion date); the answer must say so"})
        elif kind == "history_repaired":
            rows.append({"step": "app", "seconds": None,
                         "detail": f"conversation mended: {e.get('dropped')} unanswered tool "
                                   f"call(s) or stray result(s) from a cut-off question removed"})
        elif kind == "output_blocked":
            rows.append({"step": "guardrail", "seconds": None,
                         "detail": f"answer replaced before it was shown ({e.get('rule')}: "
                                   f"{e.get('reason')})"})
    return rows


# ====================================================================== cost ===

def cost_line(usage: dict) -> str:
    """Under each answer: what the question took. The cost is what the LiteLLM proxy
    charged (x-litellm-response-cost); 'at least' when a call came back without one."""
    n = usage.get("model_calls", 0)
    if not n:
        return "No model calls: answered in code."
    calls = f"{n} model call{'s' if n != 1 else ''}"
    tokens = f"{usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0):,} tokens"
    cost = usage.get("cost_usd") or 0.0
    if not usage.get("cost_complete", True):
        return f"{calls}, {tokens}, at least ${cost:.4f} (a call came back without a cost)"
    return f"{calls}, {tokens}, ${cost:.4f}"


_DOLLARS = re.compile(r"\$(\d+(?:\.\d+)?)")


def _cost_of(m: dict) -> tuple[float, bool]:
    """(cost, known) of one saved answer. An answer from 10 Nov 2026 on carries its cost as a
    number (cost_usd, cost_complete). One saved before that has it only in its cost line
    ('2 model calls, 9,936 tokens, $0.0012'), so it is read back from there, to the line's 4
    decimal places. A line with no figure leaves the total a lower bound."""
    c = m.get("cost_usd")
    if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and c >= 0:
        return float(c), m.get("cost_complete") is not False
    line = m.get("cost")
    if not isinstance(line, str):
        return 0.0, False
    if line.startswith("No model calls"):
        return 0.0, True                                  # refused or answered in code
    found = _DOLLARS.findall(line)
    if not found:
        return 0.0, False
    return float(found[-1]), "at least" not in line


def conversation_spend(shown: list[dict]) -> tuple[float, int, bool]:
    """(cost in USD, questions, complete) of a conversation, added up from what each answer
    recorded. Derived from the conversation itself, so a reopened one shows what it has cost
    (10 Nov 2026: a counter in page memory started at $0 on every load). complete is False
    when any answer's cost wasn't known: the total is then a lower bound."""
    total, questions, complete = 0.0, 0, True
    for m in shown:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            questions += 1
        elif m.get("role") == "assistant":
            cost, known = _cost_of(m)
            total += cost
            complete = complete and known
    return total, questions, complete


def conversation_line(cost: float, questions: int, complete: bool = True) -> str:
    q = f"{questions} question{'s' if questions != 1 else ''}"
    return f"This conversation: {'' if complete else 'at least '}${cost:.4f} over {q}"


# ==================================================================== photos ===

_SHA256 = re.compile(r"[0-9a-f]{64}")


def image_refs(sources: list[dict]) -> list[dict]:
    """The scans an answer cites, once each, in order: {sha256, what, file}. The page fetches
    each photo from the API by its sha256 (GET /photos/{sha256}); the model never handles
    images or links. Small enough to keep with the saved answer, so a reopened conversation
    shows its photos again (until 15 Nov 2026 they were lost on reload)."""
    seen, out = set(), []
    for s in sources or []:
        if not isinstance(s, dict) or s.get("kind") != "image":
            continue
        sha = str(s.get("sha256") or "")
        if _SHA256.fullmatch(sha) and sha not in seen:
            seen.add(sha)
            out.append({"sha256": sha, "what": s.get("what") or "Scan", "file": s.get("file")})
    return out


# ============================================================== saved copies ===

# What is saved of each message: no photos (their bytes; the references are kept), no trace.
# cost_usd and cost_complete let a front end add up a reopened conversation's spend exactly.
DISPLAY_KEYS = ("role", "text", "sources", "notes", "images", "cost", "cost_usd",
                "cost_complete")


def kept_question(text: str) -> str:
    """A question as it may be stored or redrawn: as typed, or, if the guardrail refuses it,
    only the placeholder the agent puts in the conversation. A refused question can hold a
    staff name or an injection (found 9 Nov 2026: the page saved it as typed)."""
    import guardrail

    v = guardrail.check_input(text)
    return text if v.allowed else f"[question withheld by the guardrail: {v.rule}]"


def _kept(m: dict) -> dict:
    if m.get("role") == "user" and isinstance(m.get("text"), str):
        return {**m, "text": kept_question(m["text"])}
    return m


def display_copy(shown: list[dict]) -> list[dict]:
    """What is saved of a conversation's display: each message's text and what was shown
    under it. Photo bytes and the trace are left out. A question the guardrail refuses is
    saved only as its placeholder (kept_question)."""
    return [{k: m[k] for k in DISPLAY_KEYS if m.get(k) not in (None, [], "")}
            for m in map(_kept, shown)]