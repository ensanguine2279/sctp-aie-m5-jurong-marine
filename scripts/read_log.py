#!/usr/bin/env python3
"""Read the agent's JSON log one question at a time: what the model asked for, which tools
ran with which arguments, what each step took, and where a turn went wrong.

Reads the lines agent_app.py writes (logs/agent_app.jsonl, also written by agent_ui.py and
run_evals.py), or the output of `gcloud logging read --format=json` for the hosted page.
A `turn` event closes a question: everything since the previous `turn` belongs to it.
Events after the last `turn` (a question still running, or one that crashed) are shown
as "(unfinished)".

Usage
    python scripts/read_log.py logs/agent_app.jsonl                 # the whole file
    tail -n 50 logs/agent_app.jsonl | python scripts/read_log.py    # only the latest
    python scripts/read_log.py --last 3 logs/agent_app.jsonl        # the last 3 questions
    gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="jme-agent"' \\
        --freshness=1h --limit=200 --format=json | python scripts/read_log.py

With uv (personal repo): `uv run python scripts/read_log.py ...`. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime


def _json(s: str):
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


def events(text: str):
    """Events from agent_app.jsonl, or from `gcloud logging read --format=json`. Lines
    that aren't one of the agent's JSON events (other loggers, blank lines) are skipped."""
    text = text.strip()
    if text.startswith("["):                              # gcloud: a list, newest first
        for entry in reversed(_json(text) or []):
            e = entry.get("jsonPayload") or _json(entry.get("textPayload", ""))
            if isinstance(e, dict) and "event" in e and "ts" in e:
                yield e
        return
    for line in text.splitlines():
        e = _json(line.strip()) if line.strip().startswith("{") else None
        if isinstance(e, dict) and "event" in e and "ts" in e:
            yield e


def turns(evts):
    """Lists of events, one per question; a trailing list without a `turn` is unfinished."""
    cur = []
    for e in evts:
        cur.append(e)
        if e["event"] == "turn":
            yield cur
            cur = []
    if cur:
        yield cur


def when(e) -> datetime:
    return datetime.fromisoformat(e["ts"])


def _secs(e) -> float:
    return (e.get("ms") or 0) / 1000


def format_turn(t: list[dict]) -> list[str]:
    """The lines printed for one question."""
    last = t[-1]
    done = last["event"] == "turn"
    start = when(t[0]).timestamp() - _secs(t[0])          # events are stamped at their END
    took = when(last).timestamp() - start
    q = last.get("question", "?") if done else "(unfinished)"
    out = [f"Q: {q}   ({when(last).strftime('%H:%M:%S')} UTC, {took:.1f} s)"]
    for e in (t[:-1] if done else t):
        k = e["event"]
        if k == "chat_call":
            asked = ", ".join(e.get("tool_calls") or []) or "(answered)"
            out.append(f"  model  r{e.get('round')}  {_secs(e):5.1f} s  "
                       f"{e.get('prompt_tokens')}+{e.get('completion_tokens')} tokens  asked for: {asked}")
        elif k == "tool_call":
            state = "ok" if e.get("ok") else f"REFUSED {e.get('error_code')}"
            out.append(f"  tool   r{e.get('round')}  {_secs(e):5.2f} s  {e.get('tool')}  {state}  "
                       f"sources={e.get('n_sources')}")
            out.append(f"         args: {e.get('arguments')}")
        elif k == "eval_case":                             # written by run_evals.py
            out.append(f"  eval   case {e.get('case')}, turn {e.get('turn')}")
        elif k == "chat_error":
            out.append(f"  **     chat_error: {e.get('error')}")
        else:                                              # scale_guess_blocked, ...
            detail = e.get("arguments") or e.get("tool") or ""
            out.append(f"  **     {k}: {detail}".rstrip(": "))
    if done:
        out.append(f"  => tools: {last.get('tools_called')}, sources: {last.get('n_sources')}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="log file (default: read standard input)")
    ap.add_argument("--last", type=int, help="only the last N questions")
    a = ap.parse_args(argv)
    text = open(a.file, encoding="utf-8").read() if a.file else sys.stdin.read()
    ts = list(turns(events(text)))
    if a.last:
        ts = ts[-a.last:]
    if not ts:
        print("No agent events found.", file=sys.stderr)
        return 1
    print("\n\n".join("\n".join(format_turn(t)) for t in ts))
    return 0


if __name__ == "__main__":
    sys.exit(main())