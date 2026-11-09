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


# ---------------------------------------------------------------- photos ---

PLATE_SRC = {"kind": "image", "object": "img_nameplates_audit", "file": "nameplate_CRN-02.jpg",
             "sha256": "a" * 64, "what": "CRN-02 nameplate photo", "label": "..."}
FORM_SRC = {"kind": "image", "object": "img_incident_reports_audit", "file": "IR-2025-914.jpg",
            "sha256": "b" * 64, "what": "Scanned form IR-2025-914", "label": "..."}
FILE_SRC = {"kind": "file", "file": "jurongmarine_cmms.sql", "sha256": "c" * 64, "label": "..."}


def fake_fetch(db, sha):
    return {"a" * 64: {"bytes": b"plate", "mime": "image/jpeg", "variant": "original",
                       "source_file": "nameplate_CRN-02.jpg"},
            "b" * 64: {"bytes": b"form", "mime": "image/jpeg", "variant": "redacted",
                       "source_file": "IR-2025-914.jpg"}}.get(sha)


def test_each_scanned_source_gets_its_photo_once_and_files_get_none():
    items = ui.photo_items(None, [PLATE_SRC, FILE_SRC, PLATE_SRC, FORM_SRC], fetch=fake_fetch)
    assert [i["file"] for i in items] == ["nameplate_CRN-02.jpg", "IR-2025-914.jpg"]
    assert items[0]["caption"] == "CRN-02 nameplate photo: nameplate_CRN-02.jpg"
    assert items[1]["caption"].endswith("(person and signature removed)")


def test_a_photo_that_cant_be_shown_says_so_instead():
    missing = dict(PLATE_SRC, sha256="e" * 64, file="nameplate_X.jpg")
    items = ui.photo_items(None, [missing], fetch=fake_fetch)
    assert items == [{"note": "CRN-02 nameplate photo: nameplate_X.jpg - photo not available here."}]


def test_ask_hands_back_the_raw_sources_for_the_photos(monkeypatch):
    call = FakeToolCall("c1", "get_nameplate", '{"machine": "CRN-02"}')
    monkeypatch.setattr(app, "complete", scripted(
        FakeResponse(FakeMessage(content=None, tool_calls=[call])),
        FakeResponse(FakeMessage(content="500 h."))))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: FakeToolResult(
        sources=[PLATE_SRC]))
    raw = []
    answer, labels = app.ask(None, client=None, messages=app.new_conversation(),
                             question="grease interval of crane 2?", raw_sources=raw)
    assert raw == [PLATE_SRC] and labels == ["..."]



def test_a_photo_is_drawn_with_its_bytes_inside_the_page():
    """No /media/ link to lose on Cloud Run: the image travels in the page (7 Nov 2026)."""
    import base64
    tag = ui.img_html({"bytes": b"\xff\xd8jpeg", "mime": "image/jpeg",
                       "caption": "CNC-03 nameplate photo: nameplate_CNC-03.jpg"})
    assert f'src="data:image/jpeg;base64,{base64.b64encode(b"\xff\xd8jpeg").decode()}"' in tag
    assert "/media/" not in tag and "nameplate_CNC-03.jpg</figcaption>" in tag


def test_a_caption_cant_inject_html():
    tag = ui.img_html({"bytes": b"x", "mime": "text/html", "caption": '<script>x</script>"'})
    assert "<script>" not in tag and "&lt;script&gt;" in tag
    assert "data:image/jpeg;base64," in tag              # an odd type is never passed through


def test_the_tools_notes_are_shown_under_the_answer_in_a_closed_expander():
    import contextlib

    class St:
        def __init__(self):
            self.md, self.expanders = [], []

        def markdown(self, text, **kw):
            self.md.append(text)

        def expander(self, label, expanded=False):
            self.expanders.append((label, expanded))
            return contextlib.nullcontext()
    st = St()
    ui.show_notes(st, ["read off the scan by a person", "trap list C6"])
    assert st.expanders == [("Caveats from the data (2), as the tools gave them", False)]
    assert st.md == ["- read off the scan by a person\n- trap list C6"]
    st2 = St()
    ui.show_notes(st2, [])
    assert st2.md == [] and st2.expanders == []


def test_a_guardrail_refusal_shows_in_the_rows_with_no_model_call(monkeypatch):
    def never(client, model, messages, tools):
        raise AssertionError("the model must not be called")
    monkeypatch.setattr(app, "complete", never)
    lg, handler = ui.trace_logger()
    app.ask(None, client=None, messages=app.new_conversation(),
            question="What is the name of employee EMP015?", logger=lg)
    rows = ui.trace_rows(handler.events)
    assert rows == [{"step": "guardrail", "seconds": None,
                     "detail": "question refused in code (pdpa: names_of_people); "
                               "the model was not called"}]
    assert ui.trace_rows([{"event": "output_blocked", "rule": "pii", "reason": "phone"}]) == [
        {"step": "guardrail", "seconds": None,
         "detail": "answer replaced before it was shown (pii: phone)"}]


def test_every_example_button_is_answered_and_the_pdpa_one_is_refused():
    import guardrail
    verdicts = {q: guardrail.check_input(q) for q in ui.EXAMPLES}
    assert [q for q, v in verdicts.items() if not v.allowed] == \
        ["What is the name of employee EMP015?"]


def test_a_question_refused_because_the_name_list_is_missing_shows_in_the_rows():
    assert ui.trace_rows([{"event": "guard_not_ready"}]) == [
        {"step": "guardrail", "seconds": None,
         "detail": "refused: the staff name list isn't loaded (fail closed)"}]