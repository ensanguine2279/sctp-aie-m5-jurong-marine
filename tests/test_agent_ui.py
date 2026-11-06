"""Tests for scripts/agent_ui.py's helpers (no Streamlit needed: it is imported inside main())."""
from __future__ import annotations

import agent_app as app
import agent_ui as ui
from test_agent_app import (FakeMessage, FakeResponse, FakeToolCall, FakeToolResult,
                            scripted)


def test_a_question_becomes_one_row_per_model_and_tool_call(monkeypatch):
    call = FakeToolCall("c1", "count_incidents", '{"date_from": "2025-01-01"}')
    first = FakeResponse(FakeMessage(content=None, tool_calls=[call]))
    second = FakeResponse(FakeMessage(content="One incident."))
    monkeypatch.setattr(app, "complete", scripted(first, second))
    ok = FakeToolResult(sources=[{"label": "CMMS incidents"}])
    ok.ok, ok.error = True, None
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: ok)

    lg, handler = ui.trace_logger()
    answer, sources = app.ask(None, client=None, messages=app.new_conversation(),
                              question="how many?", logger=lg)
    rows = ui.trace_rows(handler.events)

    assert answer == "One incident." and sources == ["CMMS incidents"]
    assert [r["step"] for r in rows] == ["model, round 1", "tool, round 1", "model, round 2"]
    assert "count_incidents" in rows[1]["detail"] and rows[1]["detail"].endswith("ok")
    assert "2025-01-01" in rows[1]["detail"]


def test_a_refusal_and_a_failed_chat_call_show_in_the_rows(monkeypatch):
    call = FakeToolCall("c1", "count_incidents", "{}")
    monkeypatch.setattr(app, "complete",
                        scripted(FakeResponse(FakeMessage(content=None, tool_calls=[call]))))
    refused = FakeToolResult()
    refused.ok, refused.error = False, {"code": "needs_clarification", "message": "Which scale?"}
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: refused)
    lg, handler = ui.trace_logger()
    app.ask(None, client=None, messages=app.new_conversation(), question="q", logger=lg)
    details = [r["detail"] for r in ui.trace_rows(handler.events)]
    assert any("refused: needs_clarification" in d for d in details)
    assert any("passed to the user" in d for d in details)

    def boom(client, model, messages, tools):
        raise app.ChatError("budget exceeded")
    monkeypatch.setattr(app, "complete", boom)
    lg, handler = ui.trace_logger()
    app.ask(None, client=None, messages=app.new_conversation(), question="q", logger=lg)
    assert "failed: budget exceeded" in ui.trace_rows(handler.events)[0]["detail"]


def test_the_page_password_check():
    assert ui.password_ok("s3cret", "s3cret")
    assert not ui.password_ok("S3cret", "s3cret")
    assert not ui.password_ok("", "s3cret")


def test_logging_to_the_platform_without_a_file(monkeypatch, capsys):
    monkeypatch.setattr(ui, "LOG_FILE", "off")
    lg, handler = ui.trace_logger(log_to_file=True)
    lg.info('{"event": "turn", "question": "q"}')
    assert handler.events[0]["event"] == "turn"                      # still feeds the table
    assert '"event": "turn"' in capsys.readouterr().err               # and goes to stderr
    assert ui._file_handler is None                                   # no file was opened