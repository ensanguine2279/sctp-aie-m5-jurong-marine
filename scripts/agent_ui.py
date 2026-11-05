#!/usr/bin/env python3
"""A chat front end for the agent, for the demo: person -> app -> LLM -> tools -> back.

A thin Streamlit page over `agent_app.py`; it adds no logic of its own. Under every
answer it shows the sources the tools cited, and an expander with what happened on the
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
        with st.spinner("Asking the model and checking the data..."):
            answer, sources = app.ask(db, client, st.session_state.messages, question,
                                      logger=lg)
        rows = trace_rows(handler.events)
        st.write(answer)
        if sources:
            st.markdown("**Sources**\n" + "\n".join(f"- {s}" for s in sources))
        if rows:
            with st.expander("What happened", expanded=True):
                st.table(rows)
    st.session_state.shown.append({"role": "assistant", "text": answer,
                                   "sources": sources, "trace": rows})


if __name__ == "__main__":
    main()