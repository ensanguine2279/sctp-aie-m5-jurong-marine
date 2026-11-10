#!/usr/bin/env python3
"""A chat front end for the agent, for the demo: person -> app -> LLM -> tools -> back.

A thin Streamlit page over `agent_app.py`; it adds no logic of its own. Under every
answer it shows what the answer rests on, in tabs: the sources the tools cited, the tools'
caveats, the photo behind each scanned source (from the database, hash-checked; incident
forms only with the person and signature removed), and what happened on the way: each model
call (with its time) and each tool call (with the arguments the model sent, whether the tool
refused, and how long it took). That table is the live view of the path a question takes.

Layout (10 Nov 2026: the sidebar held examples, recent conversations and spend, and was
congested). Each part has one place:
- sidebar: conversations only (New conversation, the newest saved ones, whether this one
  is saved);
- top bar: the title, an Examples popover once a conversation has started, and a usage
  popover (model, this conversation's cost, the key's spend against its cap). Near or over
  the cap, or with the proxy unreachable, a warning also shows across the page;
- a new conversation opens on the example questions as a grid, which goes once one is asked;
- each answer: its text, one cost line, then tabs (Sources, Caveats, Photos, What happened).
  On the newest answer What happened is the first tab, so it is open for the demo.

The conversation survives a restart (#5): its id is in the page's link (?session=...), and
after every answer it is saved through sessions.py (a table only the agent_sessions user can
touch). Opening the link again, after a redeploy or on another device, reloads it, so a
follow-up still has the earlier context. If the store isn't set up or is down, the sidebar
says conversations aren't being saved and the page carries on in memory.

The conversation's spend is added up from the answers on the page (conversation_spend), so a
conversation reopened from its link or from the list shows what it cost, not $0.

Usage
    streamlit run scripts/agent_ui.py

Needs the same .env as agent_app.py (AGENT_DB_*, OPENAI_API_KEY, OPENAI_BASE_URL) and
`streamlit` 1.36 or later (st.popover, and st.columns' vertical_alignment).
Team repo: pip install -r requirements-app.txt.
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_app as app  # noqa: E402

TITLE = "JME maintenance assistant"
CAPTION = "Answers come only from the shop's own data, through fixed tools, with the sources shown."

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
    """The tools' caveats, as a list. They sit in their own Caveats tab under every answer:
    one click away even when the model leaves them out (7 Nov 2026: incident 911's 'read off
    the scan by a person' was dropped once). Not open by default, because the answer usually
    says them already, in plainer words; the tools' wording is written for the model and
    names the trap-list item, which is what an auditor wants to see."""
    if notes:
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
    """The photos, one after another (inside the answer's Photos tab)."""
    for it in items or []:
        if "bytes" in it:
            st.markdown(img_html(it), unsafe_allow_html=True)
        else:
            st.caption(it["note"])


def evidence_tabs(m: dict, latest: bool = False) -> list[tuple[str, str]]:
    """(kind, label) of the tabs under one answer, only for what it has: Sources,
    Caveats, Photos, What happened, each with a count. On the newest answer What happened
    comes first: Streamlit opens the first tab, and the demo shows the path a question took
    (it used to be an open expander)."""
    tabs = []
    for kind, label in (("sources", "Sources"), ("notes", "Caveats"), ("photos", "Photos")):
        if m.get(kind):
            tabs.append((kind, f"{label} ({len(m[kind])})"))
    trace = m.get("trace")
    if trace:
        tab = ("trace", f"What happened ({len(trace)} step{'s' if len(trace) != 1 else ''})")
        if latest:
            tabs.insert(0, tab)
        else:
            tabs.append(tab)
    return tabs


def last_answer(shown: list[dict]) -> int | None:
    """Index of the newest answer on the page, or None."""
    for i in range(len(shown) - 1, -1, -1):
        if isinstance(shown[i], dict) and shown[i].get("role") == "assistant":
            return i
    return None


def split_latest(shown: list[dict]) -> tuple[list[dict], list[dict]]:
    """(earlier, latest): the latest exchange is the last question and everything after it
    (its answer). The page draws it open and folds everything earlier into one expander, so
    the page stays short and still reads top to bottom (10 Nov 2026, instead of putting the
    newest message first)."""
    for i in range(len(shown) - 1, -1, -1):
        if isinstance(shown[i], dict) and shown[i].get("role") == "user":
            return shown[:i], shown[i:]
    return [], list(shown)


FOLD_KEY = "fold_earlier"        # the sidebar toggle's state: fold earlier questions or not


def history_parts(shown: list[dict], new_question: bool,
                  fold: bool = True) -> tuple[list[dict], list[dict]]:
    """(folded, open) for drawing the conversation. Folding off: everything open, as a
    normal chat. Folding on: the latest exchange open and the rest in one expander; with a
    new question on its way, the previous exchange folds too, so only the new one is open."""
    if not fold:
        return [], list(shown)
    if new_question:
        return list(shown), []
    return split_latest(shown)


def earlier_label(earlier: list[dict]) -> str:
    """The folded earlier part's label: 'Earlier in this conversation (3 questions)'."""
    n = sum(1 for m in earlier if isinstance(m, dict) and m.get("role") == "user")
    return f"Earlier in this conversation ({n} question{'s' if n != 1 else ''})"


def draw_messages(st, messages: list[dict], newest: int | None = None) -> None:
    """Questions and answers in order; `newest` is the index (in `messages`) of the answer
    whose What happened tab comes first."""
    for i, m in enumerate(messages):
        with st.chat_message(m["role"]):
            if m["role"] == "assistant":
                render_answer(st, m, latest=(i == newest))
            else:
                st.write(m["text"])


def render_answer(st, m: dict, latest: bool = False) -> None:
    """One answer: its text, what it cost, then its evidence in tabs."""
    st.write(m["text"])
    if m.get("cost"):
        st.caption(m["cost"])
    tabs = evidence_tabs(m, latest)
    if not tabs:
        return
    for (kind, _), tab in zip(tabs, st.tabs([label for _, label in tabs])):
        with tab:
            if kind == "sources":
                st.markdown("\n".join(f"- {s}" for s in m["sources"]))
            elif kind == "notes":
                show_notes(st, m["notes"])
            elif kind == "photos":
                show_photos(st, m["photos"])
            elif kind == "trace":
                st.table(m["trace"])


def example_help(question: str) -> str | None:
    """Tooltip for an example button: only the one the guardrail refuses gets one, so the
    audience knows the refusal is the point."""
    if app.guardrail.check_input(question).allowed:
        return None
    return "Refused in code by the guardrail, before the model is called (PDPA)."


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
    """(cost, known) of one answer on the page. An answer from 10 Nov 2026 on carries its
    cost as a number (cost_usd, cost_complete). One saved before that has it only in its
    cost line ('2 model calls, 9,936 tokens, $0.0012'), so it is read back from there, to
    the line's 4 decimal places. A line with no figure leaves the total a lower bound."""
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
    """(cost in USD, questions, complete) of the conversation on the page, added up from
    what each answer recorded. Derived from the conversation itself, so a conversation
    reopened from its link or from Recent conversations shows what it has cost, and
    New conversation shows $0, with no separate counter to keep in step (10 Nov 2026: the
    old counter lived in page memory and started at $0 on every load).
    complete is False when any answer's cost wasn't known: the total is then a lower bound."""
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


USAGE_ICON = ":material/payments:"   # the usage popover's button: an icon, no words
# The Examples button too: its word was cut off in its narrow column (10 Nov 2026).
EXAMPLES_ICON = ":material/lightbulb:"
EXAMPLES_HELP = "Example questions"


def usage_help(model: str, cost: float, complete: bool = True) -> str:
    """Tooltip on the usage icon: the model and this conversation's cost, escaped for
    Streamlit (which reads Markdown, and $...$ as maths), so it can be seen without a click."""
    return md_plain(f"{model} · {'at least ' if not complete else ''}${cost:.4f}")


# Less room above the title: Streamlit pads the top of the page by about 6rem. Both
# selectors, because the container's test id is newer than its class.
# The usage popover's panel took the width of its narrow icon column and cut its lines off
# (10 Nov 2026), so it gets a minimum width of its own.
TOP_CSS = """<style>
[data-testid="stMainBlockContainer"], .block-container { padding-top: 2.5rem; }
[data-testid="stPopoverBody"] { min-width: 24rem; }
[data-testid="stPopover"] button { padding-left: 0.5rem; padding-right: 0.5rem; }
[data-testid="stHorizontalBlock"]:has(#jme-topbar) { gap: 0.25rem; }
[data-testid="stHorizontalBlock"]:has(#jme-topbar) > [data-testid="stColumn"]:not(:first-child),
[data-testid="stHorizontalBlock"]:has(#jme-topbar) > [data-testid="column"]:not(:first-child) {
  flex: 0 0 auto !important; width: auto !important; min-width: 0 !important;
}
</style>"""
# Marks the top bar for the CSS above, which shrinks its two icon columns to their buttons
# so the icons sit side by side at the right edge (10 Nov 2026: in equal columns they had a
# wide gap between them). The title column takes the rest.
TOP_MARKER = '<span id="jme-topbar"></span>'
# Column weights, used as-is only by a browser without :has() (all current ones have it).
# Not narrower: the icon buttons hold the icon and Streamlit's dropdown arrow, and in
# columns of 0.7 against 10 the arrow pushed the icon out of view (10 Nov 2026).
TOP_BAR = [6, 1, 1]


def scroll_script(nonce: int) -> str:
    """HTML for an invisible component that scrolls the page to the newest question.

    A question picked from the examples isn't typed into st.chat_input, so Streamlit doesn't
    scroll to it, and it landed below the visible part of the page unseen (10 Nov 2026).
    The script runs in a component's iframe, which shares the page's origin, and scrolls
    the last chat message into view. `nonce` (how many messages are on the page) makes each
    question's component different, so Streamlit runs it again rather than reusing the last."""
    return (f"<script>/* {nonce} */"
            "const d = window.parent.document;"
            "const m = d.querySelectorAll('[data-testid=\"stChatMessage\"]');"
            "if (m.length) m[m.length - 1].scrollIntoView({behavior: 'smooth', block: 'start'});"
            "</script>")


# What is saved of each message: no photos, no trace. cost_usd and cost_complete let the
# page add up a reopened conversation's spend exactly (conversation_spend).
DISPLAY_KEYS = ("role", "text", "sources", "notes", "cost", "cost_usd", "cost_complete")


def kept_question(text: str) -> str:
    """A question as it may be stored or redrawn: as typed, or, if the guardrail refuses it,
    only the placeholder ask() puts in `messages`. A refused question can hold a staff name
    or an injection; the page used to save it as typed in the display copy (found 9 Nov
    2026, while adding the sidebar's list of saved conversations)."""
    v = app.guardrail.check_input(text)
    return text if v.allowed else f"[question withheld by the guardrail: {v.rule}]"


def _kept(m: dict) -> dict:
    if m.get("role") == "user" and isinstance(m.get("text"), str):
        return {**m, "text": kept_question(m["text"])}
    return m


def display_copy(shown: list[dict]) -> list[dict]:
    """What is saved of the page's display: the text and what was shown under it. Photos
    are left out (large, and re-fetchable from the database), and so is the trace. A
    question the guardrail refuses is saved only as its placeholder (kept_question)."""
    return [{k: m[k] for k in DISPLAY_KEYS if m.get(k) not in (None, [], "")}
            for m in map(_kept, shown)]


def restored(loaded) -> tuple[list[dict], list[dict]] | None:
    """(messages, shown) for a conversation loaded by sessions.SessionStore, the system
    prompt fresh from code; None if nothing was found. Questions go through kept_question
    again, so a row saved before that existed is cleaned when it is drawn."""
    if not (loaded.ok and loaded.found):
        return None
    shown = [_kept(m) for m in loaded.display if isinstance(m, dict) and m.get("role") in
             ("user", "assistant") and isinstance(m.get("text"), str)]
    return app.new_conversation() + loaded.messages, shown


RECENT = 8                       # saved conversations listed in the sidebar


def ago(when, now: datetime | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', '2 d ago' for a UTC time from the store."""
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return ""
    if not isinstance(when, datetime):
        return ""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    s = (now - when.replace(tzinfo=None)).total_seconds()
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} d ago"


_MD = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|~$])")


def md_plain(text: str) -> str:
    """Text for a Streamlit label, which reads Markdown (and $...$ as maths): escaped, so
    a title shows exactly as stored."""
    return _MD.sub(r"\\\1", text)


def recent_label(r, current_sid: str | None, now: datetime | None = None) -> str:
    """One line in the sidebar's list: the first question, how many, and when (or that it
    is the conversation on the page)."""
    when = "open now" if r.sid == current_sid else ago(r.updated_at, now)
    n = f"{r.turns} question{'s' if r.turns != 1 else ''}"
    return md_plain(f"{r.title} ({n}, {when})" if when else f"{r.title} ({n})")


def save_line(store_on: bool, why: str | None, save_error: str | None) -> tuple[bool, str]:
    """(ok, sidebar text) for whether this conversation is being kept."""
    if not store_on:
        return False, f"Conversations aren't being saved ({why}). This one lasts until the page closes."
    if save_error:
        return False, f"This conversation isn't being saved right now: {save_error}."
    return True, "Saved after every answer. Keep this page's link to come back to it."


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
        elif kind == "output_blocked":
            rows.append({"step": "guardrail", "seconds": None,
                         "detail": f"answer replaced before it was shown ({e.get('rule')}: "
                                   f"{e.get('reason')})"})
    return rows


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title=TITLE, page_icon=None, layout="wide")
    st.markdown(TOP_CSS, unsafe_allow_html=True)

    expected = os.environ.get("APP_PASSWORD")
    if expected and not st.session_state.get("authed"):
        st.title(TITLE)
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

    import spend

    @st.cache_data(ttl=30, show_spinner=False)
    def key_budget():
        return spend.key_spend()                   # never raises; cached 30 s

    import sessions

    @st.cache_resource
    def session_store():
        store, why = sessions.store_from_env()    # never raises
        if store:
            store.purge()                          # retention, once per process start
        return store, why

    store, store_why = session_store()

    def start_fresh():
        st.session_state.messages = app.new_conversation()
        st.session_state.shown = []   # what to draw: role, text, sources, trace, cost
        st.session_state.sid = sessions.new_session_id()
        st.session_state.save_error = None
        st.query_params["session"] = st.session_state.sid

    if "messages" not in st.session_state:        # a new page load, or the app restarted
        sid = st.query_params.get("session")      # read before start_fresh replaces it
        start_fresh()                             # a new id, unless the link's loads below:
        if store and sessions.valid_id(sid):
            loaded = store.load(sid)
            back = restored(loaded)
            if back:
                st.session_state.messages, st.session_state.shown = back
                st.session_state.sid = sid
                st.query_params["session"] = sid
            elif not loaded.ok:
                st.session_state.save_error = loaded.error

    def open_saved(sid: str) -> None:
        """Switch the page to a saved conversation, as if its link had been opened."""
        loaded = store.load(sid)
        back = restored(loaded)
        if back:
            st.session_state.messages, st.session_state.shown = back
            st.session_state.sid = sid
            st.session_state.save_error = None
            st.query_params["session"] = sid
        else:
            st.session_state.save_error = loaded.error or "that conversation is no longer saved"
        st.rerun()

    # ---- sidebar: conversations only ------------------------------------------------
    with st.sidebar:
        if st.button("New conversation", use_container_width=True, type="primary"):
            start_fresh()
            st.rerun()
        if store:
            # The newest saved conversations (#5). Titles come from `messages`, where a
            # refused question is only its placeholder. Read once per run: one that is
            # saved by this run's answer shows from the next click on.
            st.caption("Recent")
            recent = store.recent(RECENT)          # never raises
            if recent is None:
                st.caption("Couldn't list saved conversations right now.")
            elif not recent:
                st.caption("No saved conversations yet.")
            for r in recent or []:
                here = r.sid == st.session_state.sid
                if st.button(recent_label(r, st.session_state.sid), key=f"recent_{r.sid}",
                             use_container_width=True, disabled=here):
                    open_saved(r.sid)
        st.divider()
        fold = st.toggle("Fold earlier questions", value=True, key=FOLD_KEY,
                         help="On: only the latest question and answer stay open, the rest "
                              "fold into one line. Off: the whole conversation, as a chat.")
        save_box = st.empty()                      # is this conversation being kept (#5)

    def show_saved():
        ok, text = save_line(store is not None, store_why, st.session_state.get("save_error"))
        (save_box.caption if ok else save_box.warning)(text)

    show_saved()

    # ---- top bar: title, examples (once started), usage ------------------------------
    picked = None
    head, ex_col, use_col = st.columns(TOP_BAR, vertical_alignment="center")
    with head:
        st.subheader(TITLE)
        st.caption(CAPTION + TOP_MARKER, unsafe_allow_html=True)   # marker: no element of its own
    if st.session_state.shown:
        with ex_col, st.popover(EXAMPLES_ICON, help=EXAMPLES_HELP):
            for i, q in enumerate(EXAMPLES):
                if st.button(q, key=f"ex_pop_{i}", use_container_width=True,
                             help=example_help(q)):
                    picked = q
    with use_col:
        usage_box = st.empty()                     # filled now, and again after an answer
    warn_box = st.empty()                          # the key near or over its cap (#7)

    def show_spend():
        # Spend (#7): this conversation's cost, added up from each answer's cost header
        # (exact and immediate, and the same after a reload or reopening it); the key's
        # total against its cap from the proxy (about a minute behind).
        cost, questions, complete = conversation_spend(st.session_state.shown)
        ks = key_budget()
        with usage_box.container(), st.popover(USAGE_ICON,
                                               help=usage_help(app.MODEL, cost, complete)):
            st.caption(f"Model: {app.MODEL}")
            st.markdown(conversation_line(cost, questions, complete))
            st.markdown(spend.budget_line(ks))
        if ks.warn or not ks.ok:
            warn_box.warning(spend.budget_line(ks))
        else:
            warn_box.empty()

    show_spend()

    # ---- a new conversation opens on the examples -----------------------------------
    grid_box = st.empty()
    if not st.session_state.shown:
        with grid_box.container():
            st.caption("Try one of these, or ask your own below.")
            cols = st.columns(3)
            for i, q in enumerate(EXAMPLES):
                with cols[i % 3]:
                    if st.button(q, key=f"ex_grid_{i}", use_container_width=True,
                                 help=example_help(q)):
                        picked = q

    # ---- the conversation -----------------------------------------------------------
    # Read before the history is drawn (the box stays pinned to the bottom wherever it is
    # called), so a new question folds the previous exchange at once, not one run later.
    question = st.chat_input("Ask about a machine, an incident or an SOP") or picked

    # Folding on (the sidebar toggle): earlier exchanges in one expander, the latest open.
    earlier, latest = history_parts(st.session_state.shown, bool(question), fold)
    if earlier:
        with st.expander(earlier_label(earlier)):
            draw_messages(st, earlier)
    draw_messages(st, latest, newest=last_answer(latest))

    if not question:
        return

    grid_box.empty()                               # the examples go once one is asked
    st.session_state.shown.append({"role": "user", "text": question})
    with st.chat_message("user"):
        st.write(question)
    import streamlit.components.v1 as components
    components.html(scroll_script(len(st.session_state.shown)), height=0)
    with st.chat_message("assistant"):
        lg, handler = trace_logger(log_to_file=True)
        raw: list[dict] = []
        tool_notes: list[str] = []
        usage: dict = {}
        with st.spinner("Asking the model and checking the data..."):
            answer, sources = app.ask(db, client, st.session_state.messages, question,
                                      logger=lg, raw_sources=raw, notes=tool_notes,
                                      usage=usage)
            photos = photo_items(db, raw)
            notes = app.format_notes(tool_notes)
        # On the page before the top bar is redrawn, so the conversation's total includes it.
        st.session_state.shown.append({
            "role": "assistant", "text": answer, "sources": sources, "notes": notes,
            "photos": photos, "trace": trace_rows(handler.events), "cost": cost_line(usage),
            "cost_usd": float(usage.get("cost_usd") or 0.0),
            "cost_complete": bool(usage.get("cost_complete", True))})
        key_budget.clear()                         # re-read the key's spend from the proxy
        show_spend()                               # and redraw the top bar with this question
        render_answer(st, st.session_state.shown[-1], latest=True)
    if store:                                      # keep it for a restart (#5); never raises
        _, st.session_state.save_error = store.save(
            st.session_state.sid, st.session_state.messages,
            display_copy(st.session_state.shown))
        show_saved()


if __name__ == "__main__":
    main()