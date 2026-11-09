#!/usr/bin/env python3
"""A chat front end for the agent, for the demo: person -> app -> LLM -> tools -> back.

A thin Streamlit page over `agent_app.py`; it adds no logic of its own. Under every
answer it shows the sources the tools cited, the photo behind each scanned source (from
the database, hash-checked; incident forms only with the person and signature removed),
and an expander with what happened on the
way: each model call (with its time) and each tool call (with the arguments the model
sent, whether the tool refused, and how long it took). That expander is the live view of
the path a question takes.

Usage
    streamlit run scripts/agent_ui.py

Needs the same .env as agent_app.py (AGENT_DB_*, OPENAI_API_KEY, OPENAI_BASE_URL) and
`streamlit` (pip install streamlit; with uv: uv add streamlit).
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_app as app  # noqa: E402

EXAMPLES = [
    "Is CNC-02 running differently now?",
    "What oil does CNC-03 take?",
    "What is the procedure for entering the blasting chamber?",
    "What is the welding SOP?",
    "How many incidents in 2025 were severity 4 or worse?",
    "What is the name of employee EMP015?",          # the guardrail: a PDPA refusal, in code
]


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
    """One file handler for the whole page process (not one per question)."""
    global _file_handler
    if _file_handler is None:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        _file_handler = logging.FileHandler(LOG_FILE)
        _file_handler.setFormatter(logging.Formatter("%(message)s"))
    return _file_handler


def trace_logger(log_to_file: bool = False) -> tuple[logging.Logger, TraceHandler]:
    """A logger for one question: feeds the on-page table, and with `log_to_file`
    also appends the same JSON lines to logs/agent_app.jsonl."""
    handler = TraceHandler()
    lg = logging.getLogger(f"agent_ui.{id(handler)}")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg.addHandler(handler)
    if log_to_file:
        global _stderr_handler
        if _stderr_handler is None:           # Cloud Logging and similar read stderr
            _stderr_handler = logging.StreamHandler(sys.stderr)
            _stderr_handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(_stderr_handler)
        if LOG_FILE and LOG_FILE.lower() != "off":
            lg.addHandler(_shared_file_handler())
    return lg, handler


def photo_items(db, raw_sources: list[dict], fetch=None) -> list[dict]:
    """The photos to show under an answer: one per image source, fetched from the
    database and hash-checked (agent_tools.fetch_image). Forms come only redacted. A source
    whose photo isn't stored, or fails the check, gets a note instead of a picture.
    The model never produces these: they come from the tools' sources."""
    import agent_tools as at

    fetch = fetch or at.fetch_image
    items = []
    for s in at.image_sources(raw_sources):
        img = fetch(db, s["sha256"])
        what = s.get("what") or "Scan"
        if img is None:
            items.append({"note": f"{what}: {s.get('file')} - photo not available here."})
            continue
        caption = f"{what}: {img['source_file']}"
        if img["variant"] == "redacted":
            caption += " (person and signature removed)"
        items.append({"bytes": img["bytes"], "mime": img["mime"], "caption": caption,
                      "file": img["source_file"]})
    return items


def show_notes(st, notes: list[str]) -> None:
    """The tools' caveats, under every answer, in a closed expander: one click away even
    when the model leaves them out (7 Nov 2026: incident 911's 'read off the scan by a
    person' was dropped once). Closed because the answer usually says them already, in
    plainer words; the tools' wording is written for the model and names the trap-list
    item, which is what an auditor wants to see."""
    if notes:
        with st.expander(f"Caveats from the data ({len(notes)}), as the tools gave them"):
            st.markdown("\n".join(f"- {n}" for n in notes))


def img_html(item: dict) -> str:
    """The photo as an <img> with the bytes inside it (a data URL) and its caption.

    Not st.image: that keeps the bytes in the server's memory and gives the browser a
    /media/... link to fetch separately. On Cloud Run that second request can miss (another
    or restarted container, media released after a rerun, a tab from before a redeploy)
    and the photo shows broken; seen on the live page, 7 Nov 2026. A data URL travels with
    the page."""
    import base64
    import html

    mime = item.get("mime") or "image/jpeg"
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        mime = "image/jpeg"
    data = base64.b64encode(item["bytes"]).decode("ascii")
    cap = html.escape(item.get("caption") or "")
    return (f'<figure style="margin:0 0 1rem 0"><img src="data:{mime};base64,{data}" '
            f'alt="{cap}" style="max-width:100%;height:auto;border:1px solid #ddd"/>'
            f'<figcaption style="font-size:0.85rem;color:#666">{cap}</figcaption></figure>')


def show_photos(st, items: list[dict]) -> None:
    if not items:
        return
    with st.expander(f"Photo{'s' if len(items) > 1 else ''} ({len(items)})", expanded=True):
        for it in items:
            if "bytes" in it:
                st.markdown(img_html(it), unsafe_allow_html=True)
            else:
                st.caption(it["note"])


def password_ok(typed: str, expected: str) -> bool:
    """Constant-time check of the page password."""
    return hmac.compare_digest(typed.encode(), expected.encode())


def trace_rows(events: list[dict]) -> list[dict]:
    """One table row per model call or tool call, in the order they happened."""
    rows = []
    for e in events:
        kind = e.get("event")
        if kind == "chat_call":
            tools = ", ".join(e.get("tool_calls") or []) or "(answered)"
            rows.append({"step": f"model, round {e['round']}", "detail": f"asked for: {tools}",
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
        elif kind == "output_blocked":
            rows.append({"step": "guardrail", "seconds": None,
                         "detail": f"answer replaced before it was shown ({e.get('rule')}: "
                                   f"{e.get('reason')})"})
    return rows


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="JME maintenance assistant", page_icon=None)
    st.title("JME maintenance assistant")
    st.caption("Answers come only from the shop's own data, through fixed tools, "
               "with the sources shown.")

    expected = os.environ.get("APP_PASSWORD")
    if expected and not st.session_state.get("authed"):
        typed = st.text_input("Password", type="password")
        if typed and password_ok(typed, expected):
            st.session_state.authed = True
            st.rerun()
        if typed:
            st.error("Wrong password.")
        st.stop()

    import guardrail
    try:
        guardrail.require_names()              # fail closed: no page without the name list
    except guardrail.GuardNotReady as e:
        st.error(str(e))
        st.stop()

    @st.cache_resource
    def connect():
        import agent_tools as at
        return at.connect(), app.build_client()

    try:
        db, client = connect()
    except SystemExit as e:           # build_client exits with a readable message
        st.error(str(e))
        st.stop()
    except Exception as e:
        st.error(f"Couldn't connect as the agent user: {e}")
        st.stop()

    if "messages" not in st.session_state:
        st.session_state.messages = app.new_conversation()
        st.session_state.shown = []   # what to draw: role, text, sources, trace

    with st.sidebar:
        st.subheader("Try")
        picked = None
        for q in EXAMPLES:
            if st.button(q, use_container_width=True):
                picked = q
        if st.button("New conversation", use_container_width=True):
            st.session_state.messages = app.new_conversation()
            st.session_state.shown = []
            st.rerun()
        st.caption(f"Model: {app.MODEL}")

    for m in st.session_state.shown:
        with st.chat_message(m["role"]):
            st.write(m["text"])
            if m.get("sources"):
                st.markdown("**Sources**\n" + "\n".join(f"- {s}" for s in m["sources"]))
            show_notes(st, m.get("notes") or [])
            show_photos(st, m.get("photos") or [])
            if m.get("trace"):
                with st.expander("What happened"):
                    st.table(m["trace"])

    question = st.chat_input("Ask about a machine, an incident or an SOP") or picked
    if not question:
        return

    st.session_state.shown.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        lg, handler = trace_logger(log_to_file=True)
        raw: list[dict] = []
        tool_notes: list[str] = []
        with st.spinner("Asking the model and checking the data..."):
            answer, sources = app.ask(db, client, st.session_state.messages, question,
                                      logger=lg, raw_sources=raw, notes=tool_notes)
            photos = photo_items(db, raw)
            notes = app.format_notes(tool_notes)
        rows = trace_rows(handler.events)
        st.write(answer)
        if sources:
            st.markdown("**Sources**\n" + "\n".join(f"- {s}" for s in sources))
        show_notes(st, notes)
        show_photos(st, photos)
        if rows:
            with st.expander("What happened", expanded=True):
                st.table(rows)
    st.session_state.shown.append({"role": "assistant", "text": answer,
                                   "sources": sources, "notes": notes, "photos": photos,
                                   "trace": rows})


if __name__ == "__main__":
    main()