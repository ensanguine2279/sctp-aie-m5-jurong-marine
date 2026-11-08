#!/usr/bin/env python3
"""The agent's loop: the thing a person actually talks to.

Wires an LLM to `agent_tools.py` with a plain OpenAI-style function-calling loop (no
LangGraph, no vector store, no memory beyond this process) and three rules from the
harness checklist, enforced in code rather than hoped for from the prompt:

    1. Grounding (#1): every fact in the answer comes from a tool call made just now.
       The model never answers from memory; it can only pick a tool and fill in its
       arguments (agent_tools.py validates them and runs the fixed SQL).
    2. Citations (#2): every tool result's `sources` are collected across the turn and
       shown under the answer, so the user can check the claim themselves.
    3. Honest degradation (#4): a tool error (bad arguments, no data, a down database)
       is never covered up with an invented number - the tool's own message goes back
       to the model, which must say so. A failed LLM call, or too many tool-call rounds,
       produces a plain "I couldn't answer this" rather than a crash or a guess.

What this script does NOT do yet (by design, not oversight - see the capstone task
list): no guardrail layer in front of the model (#3), no cross-restart conversation
state (#5), no human-in-the-loop gate (#6: this app is read-only, so none is needed),
and no eval suite (#8, `evals/cases.yaml`, separate). Those are later steps; this is
the minimum that answers a question end to end, grounded, with citations, without
crashing or inventing.

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
from datetime import datetime, timezone
from pathlib import Path

import agent_tools as at

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


class ChatError(Exception):
    """A chat-completion call failed: API error, budget cap, network, timeout."""


def complete(client, model, messages, tools):
    """One call to the chat API. Wraps SDK/network errors as ChatError, so a failed
    call degrades honestly (#4) instead of raising out of the loop."""
    try:
        return client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto", temperature=0)
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


def run_turn(db, client, messages, *, model=MODEL, max_rounds=MAX_TOOL_ROUNDS,
             complete_fn=None, logger=log, notes_out=None):
    """Run one user turn to completion: call the model, run any tools it picks, feed
    the results back, repeat until it answers in plain text or `max_rounds` is hit.
    Returns (answer_text, sources, tool_names_called). Mutates `messages` in place, so
    the caller's conversation keeps growing turn to turn.

    Never raises: a chat-API failure or an exhausted round budget both return a plain
    English message instead (#4), and the per-tool honesty (never invented, cited,
    clarify-don't-guess) already comes from agent_tools.run_tool, which also never
    raises."""
    complete_fn = complete_fn or complete   # looked up here, not at def time, so a
                                             # monkeypatched module-level `complete` is honoured
    sources: list[dict] = []
    tool_names: list[str] = []
    for round_no in range(1, max_rounds + 1):
        t0 = time.monotonic()
        try:
            resp = complete_fn(client, model, messages, at.tool_schemas())
        except ChatError as e:
            _emit(logger, "chat_error", round=round_no, model=model, error=str(e))
            return ("I couldn't reach the language model just now, so I can't answer "
                    "this turn. Please try again in a moment."), sources, tool_names

        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        messages.append(_assistant_message(msg))
        calls = getattr(msg, "tool_calls", None) or []
        _emit(logger, "chat_call", round=round_no, model=model,
              prompt_tokens=getattr(usage, "prompt_tokens", None),
              completion_tokens=getattr(usage, "completion_tokens", None),
              tool_calls=[c.function.name for c in calls],
              ms=round((time.monotonic() - t0) * 1000, 1))

        if not calls:
            return msg.content, sources, tool_names

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
            sources.extend(result.sources)
            if notes_out is not None:
                notes_out.extend(getattr(result, "notes", None) or [])
            messages.append({"role": "tool", "tool_call_id": call.id,
                              "content": result.for_model()})
        if clarify:
            _emit(logger, "clarification_returned", round=round_no)
            messages.append({"role": "assistant", "content": clarify})
            return clarify, sources, tool_names

    _emit(logger, "max_rounds_exceeded", rounds=max_rounds)
    return ("I wasn't able to settle on an answer within the allowed number of tool "
            "calls. Try asking a narrower question."), sources, tool_names


def new_conversation() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def ask(db, client, messages, question, *, model=MODEL, logger=log, raw_sources=None,
        notes=None):
    """Append the user's question, run it to completion, return (answer, source
    labels). `messages` is mutated in place so the caller can keep the conversation.
    raw_sources: a list to extend with the tools' source records (agent_ui.py shows the
    photo behind each image source from them). notes: a list to extend with the tools'
    notes, the caveats the page shows under the answer whether or not the model said them."""
    messages.append({"role": "user", "content": question})
    answer, sources, tool_names = run_turn(db, client, messages, model=model, logger=logger,
                                           notes_out=notes)
    _emit(logger, "turn", question=question[:200],
          tools_called=tool_names, n_sources=len(sources))
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