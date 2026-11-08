"""Tests for scripts/read_log.py: reading the agent's JSON log per question. Standard
library only, no database, no model."""
from __future__ import annotations

import json

import read_log as rl

# The real run from docs/agent_app.md (5 Nov 2026, before the severity-scale guard).
REAL = [
    {"ts": "2026-11-05T09:55:06.189+00:00", "event": "chat_call", "round": 1, "model": "gpt-4o-mini",
     "prompt_tokens": 2555, "completion_tokens": 49, "tool_calls": ["count_incidents"], "ms": 36849.2},
    {"ts": "2026-11-05T09:55:06.274+00:00", "event": "tool_call", "round": 1, "tool": "count_incidents",
     "arguments": '{"date_from":"2025-01-01","date_to":"2025-12-31","at_least_as_severe_as":4,'
                  '"severity_scale":"5_is_worst"}',
     "ok": True, "error_code": None, "n_sources": 1, "ms": 85.2},
    {"ts": "2026-11-05T09:55:10.856+00:00", "event": "chat_call", "round": 2, "model": "gpt-4o-mini",
     "prompt_tokens": 3067, "completion_tokens": 47, "tool_calls": [], "ms": 4581.7},
    {"ts": "2026-11-05T09:55:10.856+00:00", "event": "turn",
     "question": "how many incidents in 2025 were severity 4 or worse?",
     "tools_called": ["count_incidents"], "n_sources": 1},
]


def jsonl(evts):
    return "\n".join(json.dumps(e) for e in evts)


def test_the_documented_example_prints_as_documented():
    [t] = list(rl.turns(rl.events(jsonl(REAL))))
    assert rl.format_turn(t) == [
        "Q: how many incidents in 2025 were severity 4 or worse?   (09:55:10 UTC, 41.5 s)",
        "  model  r1   36.8 s  2555+49 tokens  asked for: count_incidents",
        "  tool   r1   0.09 s  count_incidents  ok  sources=1",
        '         args: {"date_from":"2025-01-01","date_to":"2025-12-31","at_least_as_severe_as":4,'
        '"severity_scale":"5_is_worst"}',
        "  model  r2    4.6 s  3067+47 tokens  asked for: (answered)",
        "  => tools: ['count_incidents'], sources: 1",
    ]


def test_other_lines_are_skipped_and_an_unfinished_question_is_shown():
    text = "\n".join(["", "not json", '{"no": "event"}', jsonl(REAL), jsonl(REAL[:2])])
    ts = list(rl.turns(rl.events(text)))
    assert len(ts) == 2
    assert rl.format_turn(ts[1])[0].startswith("Q: (unfinished)")


def test_gcloud_output_is_read_oldest_first_from_either_payload():
    entries = [{"jsonPayload": e} if i % 2 else {"textPayload": json.dumps(e)}
               for i, e in enumerate(REAL)]
    gcloud = json.dumps(list(reversed(entries)))          # gcloud lists newest first
    assert [e["event"] for e in rl.events(gcloud)] == [e["event"] for e in REAL]


def test_guard_eval_and_error_events_are_shown():
    evts = [{"ts": "2026-11-08T09:00:00+00:00", "event": "eval_case", "case": "killshot-sev-ge-4", "turn": 1},
            {"ts": "2026-11-08T09:00:01+00:00", "event": "scale_guess_blocked", "round": 1,
             "tool": "count_incidents", "arguments": '{"severity_scale":"5_is_worst"}'},
            {"ts": "2026-11-08T09:00:02+00:00", "event": "tool_call", "round": 1, "tool": "count_incidents",
             "arguments": "{}", "ok": False, "error_code": "needs_clarification", "n_sources": 0, "ms": 5},
            {"ts": "2026-11-08T09:00:03+00:00", "event": "chat_error", "round": 2, "error": "budget exceeded"},
            {"ts": "2026-11-08T09:00:03+00:00", "event": "turn", "question": "q", "tools_called": [],
             "n_sources": 0}]
    text = "\n".join(rl.format_turn(next(rl.turns(rl.events(jsonl(evts))))))
    assert "eval   case killshot-sev-ge-4, turn 1" in text
    assert '**     scale_guess_blocked: {"severity_scale":"5_is_worst"}' in text
    assert "REFUSED needs_clarification" in text and "chat_error: budget exceeded" in text


def test_the_command_line_reads_a_file_and_keeps_the_last_n(tmp_path, capsys):
    p = tmp_path / "agent_app.jsonl"
    other = [dict(e, question="second") if e["event"] == "turn" else e for e in REAL]
    p.write_text(jsonl(REAL) + "\n" + jsonl(other) + "\n", encoding="utf-8")
    assert rl.main([str(p), "--last", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Q: second") and out.count("Q: ") == 1
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert rl.main([str(empty)]) == 1