#!/usr/bin/env python3
"""A chat front end for the agent, for the demo: person -> page -> agent API -> LLM -> tools.

Since phase 3 (15 Nov 2026) the page is only a front end. It holds no database login, no
LiteLLM key and no agent code: every question, saved conversation, photo and spend figure
comes from the agent API (scripts/agent_api.py) through scripts/api_client.py, with two
settings, AGENT_API_URL and AGENT_API_KEY. Grounding, the guardrail on both sides of the
model, saving and retention all happen in the API, the same for every client.

Under every answer it shows what the answer rests on, in tabs: the sources the tools cited,
the tools' caveats, the photo behind each scanned source (from the API, which reads it from
the database hash-checked; incident forms only with the person and signature removed), and
what happened on the way: each model call (with its time) and each tool call (with the
arguments the model sent, whether the tool refused, and how long it took). While a question
is being answered, a status line follows the agent's steps as the API streams them.

Layout (10 Nov 2026). Each part has one place:
- sidebar: conversations only (New conversation, the newest saved ones, whether this one
  is saved);
- bottom bar: a bin icon (Delete this conversation) left of the question box;
- top bar: the title, an Examples popover once a conversation has started, and a usage
  popover (model, this conversation's cost, the key's spend against its cap). Near or over
  the cap, or with the spend unavailable, a warning also shows across the page;
- a new conversation opens on the example questions as a grid, which goes once one is asked;
- each answer: its text, one cost line, then tabs (Sources, Caveats, Photos, What happened).
  On the newest answer What happened is the first tab, so it is open for the demo.

The conversation survives a restart (#5): the API saves it after every answer and gives it
an id, which the page puts in its link (?session=...) as soon as the API sends it (the first
event of the stream). Opening the link again, after a redeploy or on another device, reloads
it from the API, photos included. If the API isn't saving conversations, the sidebar says so.
The bin, Delete this conversation (11 Nov 2026), deletes it through the API at once, after a
confirm, and starts a new one.

A click while a question is being answered makes Streamlit stop the script and run it
again. The API answers in its own thread, so the question still finishes and is saved; the
page reloads the conversation and shows the answer if it is there, or says the question was
cut off and may appear when the conversation is reopened (settle_cut_off).

The conversation's spend comes from the API, which adds it up from the answers (every answer
and GET /sessions/{id} carry it), so a reopened conversation shows what it cost, not $0. The
page does no sum of its own, except when conversations aren't saved (spend_after).

Since 16 Nov 2026 (phase 4) the page lives in frontend/ with api_client.py, its only module:
its image is built from that folder alone, so nothing of the agent's can end up in it. On Cloud
Run the API is private: with AGENT_API_AUTH=google the client also sends a Google identity
token for the API's URL, from the metadata server (api_client.GoogleIdToken).

Usage
    streamlit run scripts/agent_ui.py

Needs AGENT_API_URL (the API's address, e.g. http://localhost:8000 or its Cloud Run URL) and
AGENT_API_KEY (the same key the API was started with), optionally APP_PASSWORD, and
`streamlit` 1.36 or later (st.popover, and st.columns' vertical_alignment).
Team repo: pip install -r requirements-app.txt.
"""
from __future__ import annotations

import hmac
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_client  # noqa: E402

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
REFUSED_EXAMPLE = EXAMPLES[-1]

_SID = re.compile(r"[0-9a-f]{32}")


def valid_id(sid) -> bool:
    return isinstance(sid, str) and bool(_SID.fullmatch(sid))


# ==================================================================== photos ===

def photo_items(fetch, images: list[dict]) -> list[dict]:
    """The photos to show under an answer: one per scan it cites (the answer's `images`),
    fetched from the API by SHA-256 (`fetch(sha)` -> {bytes, mime, variant} or None). Forms
    come only redacted. A scan whose photo isn't stored, fails the hash check, or can't be
    fetched right now gets a note instead of a picture. The model never produces these."""
    items = []
    for ref in images or []:
        what, file = ref.get("what") or "Scan", ref.get("file") or "scan"
        try:
            img = fetch(ref.get("sha256") or "")
        except api_client.ApiError:
            items.append({"note": f"{what}: {file} - photo can't be fetched right now."})
            continue
        if img is None:
            items.append({"note": f"{what}: {file} - photo not available here."})
            continue
        caption = f"{what}: {file}"
        if img.get("variant") == "redacted":
            caption += " (person and signature removed)"
        items.append({"bytes": img["bytes"], "mime": img.get("mime"), "caption": caption,
                      "file": file})
    return items


def attach_photos(m: dict, fetch) -> dict:
    """Fetch an answer's photos once, the first time it is drawn (kept in the page's state)."""
    if m.get("role") == "assistant" and m.get("images") and "photos" not in m:
        m["photos"] = photo_items(fetch, m["images"])
    return m


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
    audience knows the refusal is the point. (The page no longer loads the guardrail: the
    API refuses the question, in code, before the model is called.)"""
    if question != REFUSED_EXAMPLE:
        return None
    return "Refused in code by the guardrail, before the model is called (PDPA)."


def step_line(step: dict) -> str:
    """The status line while a question is answered, from one streamed step (no question or
    answer text is in a step: only which node ran and which tools)."""
    node = (step or {}).get("node")
    n = step.get("round")
    if node == "guard_in":
        return ("Question checked by the guardrail" if step.get("passed", True)
                else "Question refused by the guardrail, in code")
    if node == "model":
        if step.get("stopped"):
            return "Stopped: too many tool calls"
        if step.get("failed"):
            return f"Model call failed (round {n})"
        tools = step.get("tool_calls") or []
        return (f"Model (round {n}) asked for: {', '.join(tools)}" if tools
                else f"Model (round {n}) wrote the answer")
    if node == "tools":
        ran = ", ".join(str(t) for t in step.get("tools") or []) or "no tools"
        return f"Tools (round {n}) ran: {ran}"
    if node == "guard_out":
        return ("Answer replaced by the guardrail before it was shown"
                if step.get("answer_replaced") else "Answer checked by the guardrail")
    return f"Step: {node}"


def answer_message(body: dict) -> dict:
    """An /ask answer (the stream's `answer` event) as the page draws it."""
    usage = body.get("usage") or {}
    return {"role": "assistant", "text": body.get("answer") or "",
            "sources": list(body.get("sources") or []), "notes": list(body.get("notes") or []),
            "images": list(body.get("images") or []), "trace": list(body.get("trace") or []),
            "cost": body.get("cost_line"), "cost_usd": float(usage.get("cost_usd") or 0.0),
            "cost_complete": bool(usage.get("cost_complete", True))}


def error_text(data: dict) -> tuple[str, bool]:
    """(what to say, may it still finish) for the stream's `error` event."""
    err = (data or {}).get("error") or {}
    status = data.get("status")
    return (err.get("message") or "The question couldn't be answered.",
            status in (504, 0))


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
            "const m = d.querySelectorAll('[data-testid=\\\"stChatMessage\\\"]');"
            "if (m.length) m[m.length - 1].scrollIntoView({behavior: 'smooth', block: 'start'});"
            "</script>")


def restored(payload: dict | None) -> list[dict] | None:
    """What to draw of a conversation from GET /sessions/{id}: its questions and answers
    (the API keeps a refused question only as its placeholder); None if there is none."""
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None
    return [dict(m) for m in payload["messages"] if isinstance(m, dict) and
            m.get("role") in ("user", "assistant") and isinstance(m.get("text"), str)]


def _questions(shown: list[dict]) -> int:
    return sum(1 for m in shown if isinstance(m, dict) and m.get("role") == "user")


def settle_cut_off(shown: list[dict], sid: str | None, load) -> tuple[list[dict], str | None]:
    """At the start of a run: a question at the end of the page with no answer was cut off
    (the page was clicked while it was being answered, and Streamlit stopped the script).
    The API kept answering it in its own thread, so the conversation is reloaded (`load(sid)`
    -> the GET /sessions/{id} body): if the answer is there, it is drawn and nothing is said.
    Otherwise the question is taken off the page and a note says so. Returns (shown, note)."""
    if not (shown and isinstance(shown[-1], dict) and shown[-1].get("role") == "user"):
        return shown, None
    if valid_id(sid):
        try:
            back = restored(load(sid))
        except api_client.ApiError:
            back = None
        if back and _questions(back) >= _questions(shown) and back[-1].get("role") == "assistant":
            return back, None
    return shown[:-1], interrupted_line(valid_id(sid))


def interrupted_line(may_finish: bool) -> str:
    """The note shown once after a question was cut off, escaped for Streamlit. The question
    itself isn't repeated: the page no longer runs the guardrail that decides what of a
    question may be redrawn."""
    more = (" It may still be answered: reopen this conversation from the list in a moment."
            if may_finish else " Ask it again if you still need it.")
    return md_plain("Your last question wasn't answered here: the page was clicked while it "
                    "was being worked on." + more)


def delete_conversation(client, sid: str | None) -> str | None:
    """Delete the conversation on the page through the API (Delete this conversation).
    None when it is gone (one the API never saved counts as gone), else why not."""
    if not valid_id(sid):
        return None
    try:
        client.delete(sid)
    except api_client.ApiError as e:
        return e.message
    return None


DELETE_LABEL = "Delete this conversation"
DELETE_ICON = ":material/delete:"      # the bin left of the question box; its tooltip says what
INPUT_PLACEHOLDER = "Ask about a machine, an incident or an SOP"
# The bottom bar: the bin, then the question box. Streamlit pins st.chat_input to the bottom
# only at the top level of the page; inside columns it is drawn where it is, so both go in
# Streamlit's pinned bottom container, st.bottom. (It was st._bottom at first; Streamlit
# warned that st._bottom will be removed after 2026-07-01 and to use st.bottom, 11 Nov 2026.)
# A Streamlit without st.bottom gets the bin in the sidebar instead.
INPUT_MARKER = '<span id="jme-inputbar"></span>'
INPUT_BAR = [1, 14]                   # used as-is only by a browser without :has()
# A shorter question box (11 Nov 2026, 21:12: "make the text input and delete button height
# smaller"). Its height comes from the textarea's padding and line height, so those are
# trimmed; the box still grows with a long question. The bin follows the box's height
# (fit_bin_script), so it shrinks with it. Tune the two numbers here.
INPUT_PAD = "0.3rem"                  # above and below the text (Streamlit's is larger)
INPUT_LINE = "1.35"                   # line height of the text
INPUT_CSS = """<style>
[data-testid="stChatInput"] textarea {
  padding-top: PAD !important; padding-bottom: PAD !important;
  line-height: LINE !important; min-height: 0 !important;
}
[data-testid="stChatInput"] > div, [data-testid="stChatInput"] [data-baseweb="textarea"],
[data-testid="stChatInput"] [data-baseweb="base-input"] {
  min-height: 0 !important;
}
[data-testid="stChatInputSubmitButton"] {
  height: auto !important; min-height: 0 !important; align-self: center;
}
[data-testid="stElementContainer"]:has(#jme-inputbar), .element-container:has(#jme-inputbar) {
  display: none;
}
[data-testid="stVerticalBlock"]:has(#jme-inputbar) [data-testid="stHorizontalBlock"] {
  gap: 0.5rem; align-items: flex-end !important;
}
[data-testid="stVerticalBlock"]:has(#jme-inputbar) [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child,
[data-testid="stVerticalBlock"]:has(#jme-inputbar) [data-testid="stHorizontalBlock"] > [data-testid="column"]:first-child {
  flex: 0 0 auto !important; width: auto !important; min-width: 0 !important;
}
</style>""".replace("PAD", INPUT_PAD).replace("LINE", INPUT_LINE)


DELETE_ICON_SCALE = 0.6               # the bin icon's size, as a share of the button's height


def fit_bin_script(nonce: int) -> str:
    """HTML for an invisible component that makes the bin exactly as tall as the question
    box, and keeps it so when the box grows with a long question.

    CSS alone didn't (11 Nov 2026: "delete button is not the same height as the text
    input"): the popover's button sits inside wrappers whose heights Streamlit sets itself.
    So the script, in a component's iframe that shares the page's origin (as scroll_script),
    finds the question box, the row it is in, and the popover button in that row, and sets
    the button's height to the box's measured height; a ResizeObserver repeats it whenever
    the box changes size. `nonce` makes each run's component new, so Streamlit runs it.

    It also hides the popover's down arrow on the bin (asked for 11 Nov 2026): Streamlit
    draws it as a Material icon ("expand_more", "expand_less" when open) or, in older
    versions, an SVG; the bin itself is a Material icon font, not an SVG, so it stays. A
    MutationObserver hides the arrow again if Streamlit redraws it (on opening the panel).
    Without the arrow the button was thin, so its width is set to its height too: a square,
    the icon centred (asked for 11 Nov 2026, 20:30). On the page the plain width didn't take
    (20:38): something between the button and its column kept it narrow. So the width is set
    with priority ('important'), and every element from the button up to its column is given
    room for it (min-width = the height, no max-width, no clipping, not flexed narrower).
    The bin icon is drawn at DELETE_ICON_SCALE of the button's height (asked for bigger,
    11 Nov 2026, 22:42), so it stays in proportion when the bar's height changes."""
    return (f"<script>/* {nonce} */"
            "const d = window.parent.document;"
            "function fit() {"
            "  const box = d.querySelector('[data-testid=stChatInput]');"
            "  if (!box) return null;"
            "  const row = box.closest('[data-testid=stHorizontalBlock]');"
            "  const btn = row && row.querySelector('[data-testid=stPopover] button');"
            "  if (!btn) return null;"
            "  const h = Math.round(box.getBoundingClientRect().height) + 'px';"
            "  btn.style.height = h; btn.style.minHeight = h;"
            "  btn.style.paddingTop = '0'; btn.style.paddingBottom = '0';"
            "  for (const [k, v] of [['width', h], ['min-width', h], ['max-width', h],"
            "                        ['padding-left', '0'], ['padding-right', '0'],"
            "                        ['justify-content', 'center']])"
            "    btn.style.setProperty(k, v, 'important');"
            "  const col = btn.closest('[data-testid=stColumn], [data-testid=column]');"
            "  for (let e = btn.parentElement; e; e = e.parentElement) {"
            "    for (const [k, v] of [['min-width', h], ['width', 'auto'], ['max-width', 'none'],"
            "                          ['flex', '0 0 auto'], ['overflow', 'visible']])"
            "      e.style.setProperty(k, v, 'important');"
            "    if (e === col) break;"
            "  }"
            "  noArrow(btn);"
            "  if (!btn.dataset.jmeWatched) {"
            "    btn.dataset.jmeWatched = '1';"
            "    new MutationObserver(() => noArrow(btn))"
            "      .observe(btn, {childList: true, subtree: true, characterData: true});"
            "  }"
            "  return box;"
            "}"
            "function noArrow(btn) {"
            "  btn.querySelectorAll('svg').forEach(e => { e.style.display = 'none'; });"
            "  btn.querySelectorAll('span').forEach(e => {"
            "    const t = (e.textContent || '').trim();"
            "    if (t === 'expand_more' || t === 'expand_less') e.style.display = 'none';"
            "    if (t === 'delete') {"
            "      const px = Math.round((parseFloat(btn.style.height) || 40) * "
            f"{DELETE_ICON_SCALE}) + 'px';"
            "      e.style.setProperty('font-size', px, 'important');"
            "      e.style.setProperty('line-height', '1', 'important');"
            "      e.style.setProperty('width', 'auto', 'important');"
            "      e.style.setProperty('height', 'auto', 'important');"
            "    }"
            "  });"
            "}"
            "let tries = 0;"
            "const t = setInterval(() => {"
            "  const box = fit();"
            "  if (box) { clearInterval(t); new ResizeObserver(fit).observe(box); }"
            "  else if (++tries > 50) clearInterval(t);"
            "}, 100);"
            "</script>")


def show_delete(store_on: bool, shown: list[dict]) -> bool:
    """The bin shows once the conversation has an answer and conversations are saved:
    before that there is nothing in the store to delete."""
    return bool(store_on) and any(isinstance(m, dict) and m.get("role") == "assistant"
                                  for m in shown or [])
DELETE_WARNING = ("Deletes this conversation from the saved conversations now, for good, and "
                  "starts a new one. Its link will open a new conversation.")


RECENT = 8                       # saved conversations listed in the sidebar (the API's default)


NO_SPEND = {"cost_usd": 0.0, "questions": 0, "complete": True}


def spend_line(sp: dict) -> str:
    """'This conversation: $0.0031 over 2 questions', as the API words it; built here only for
    a total the page added up itself (spend_after)."""
    if isinstance(sp.get("line"), str):
        return sp["line"]
    q = int(sp.get("questions") or 0)
    return (f"This conversation: {'' if sp.get('complete', True) else 'at least '}"
            f"${float(sp.get('cost_usd') or 0):.4f} over {q} question{'s' if q != 1 else ''}")


def spend_after(prev: dict, body: dict) -> dict:
    """The conversation's spend after an answer. A saved answer carries the conversation's
    total, added up by the API: that is used as it is. An answer that wasn't saved (the API
    keeps no conversations) carries only its own question, so it is added to what the page
    had (those questions are answered one by one anyway)."""
    sp = body.get("spend")
    if body.get("session_id") and isinstance(sp, dict):
        return dict(sp)
    usage = body.get("usage") or {}
    cost = usage.get("cost_usd")
    known = isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
    return {"cost_usd": round(float(prev.get("cost_usd") or 0) + (float(cost) if known else 0), 6),
            "questions": int(prev.get("questions") or 0) + 1,
            "complete": bool(prev.get("complete", True)) and known
            and usage.get("cost_complete", True) is not False}


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


def recent_label(r: dict, current_sid: str | None, now: datetime | None = None) -> str:
    """One line in the sidebar's list (a GET /sessions row): the first question, how many,
    and when (or that it is the conversation on the page)."""
    turns = int(r.get("turns") or 0)
    when = "open now" if r.get("session_id") == current_sid else ago(r.get("updated_at"), now)
    n = f"{turns} question{'s' if turns != 1 else ''}"
    title = r.get("title") or "(untitled)"
    return md_plain(f"{title} ({n}, {when})" if when else f"{title} ({n})")


def save_line(sessions: str | None, save_error: str | None) -> tuple[bool, str]:
    """(ok, sidebar text) for whether this conversation is being kept. `sessions` is the
    API's /health field: "on", or "off (why)"."""
    if sessions != "on":
        why = str(sessions or "the API didn't say")
        if why.startswith("off (") and why.endswith(")"):
            why = why[5:-1]
        return False, (f"Conversations aren't being saved ({why}), so each question is "
                       f"answered on its own, without the earlier ones.")
    if save_error:
        return False, f"This conversation isn't being saved right now: {save_error}."
    return True, "Saved after every answer. Keep this page's link to come back to it."


def password_ok(typed: str, expected: str) -> bool:
    """Constant-time check of the page password."""
    return hmac.compare_digest(typed.encode(), expected.encode())


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title=TITLE, page_icon=None, layout="wide")
    st.markdown(TOP_CSS, unsafe_allow_html=True)
    st.markdown(INPUT_CSS, unsafe_allow_html=True)

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

    ApiError = api_client.ApiError

    @st.cache_resource
    def api():
        return api_client.from_env()               # AGENT_API_URL and AGENT_API_KEY

    try:
        client = api()
    except ValueError as e:
        st.error(str(e))
        st.stop()

    @st.cache_data(ttl=60, show_spinner=False)
    def api_health():
        return client.health()                     # an ApiError isn't cached: tried again

    try:
        health = api_health()
    except ApiError as e:
        st.error(f"The agent API can't be reached right now: {e.message}")
        st.stop()
    if not health.get("guard_ready"):              # fail closed, as the API does
        st.error("The agent API isn't ready: its staff name list isn't loaded, so it refuses "
                 "every question (fail closed). Build the list and restart the API.")
        st.stop()
    store_on = health.get("sessions") == "on"
    model = health.get("model") or "the model"

    @st.cache_data(ttl=30, show_spinner=False)
    def key_budget() -> dict:
        try:
            return client.usage()                  # never fails on the API's side
        except ApiError as e:
            return {"ok": False, "warn": False, "line": f"Spend not available: {e.message}."}

    @st.cache_data(ttl=3600, show_spinner=False, max_entries=200)
    def fetch_photo(sha: str):
        return client.photo(sha)                   # an ApiError isn't cached: tried again

    def start_fresh():
        st.session_state.shown = []   # what to draw: role, text, sources, images, trace, cost
        st.session_state.sid = None   # the API gives the id with the first answer's stream
        st.session_state.spend = dict(NO_SPEND)    # the API's figure, after each answer
        st.session_state.save_error = None
        st.query_params.pop("session", None)

    def open_conversation(sid: str) -> bool:
        """Switch the page to a saved conversation, from the API."""
        try:
            payload = client.session(sid)
            back = restored(payload)
        except ApiError as e:
            payload, back = None, None
            st.session_state.save_error = ("that conversation is no longer saved"
                                           if e.status == 404 else e.message)
        if back is None:
            return False
        st.session_state.shown, st.session_state.sid = back, sid
        st.session_state.spend = dict(payload.get("spend") or NO_SPEND)
        st.session_state.save_error = None
        st.query_params["session"] = sid
        return True

    if "shown" not in st.session_state:            # a new page load, or the page restarted
        sid = st.query_params.get("session")       # read before start_fresh clears it
        start_fresh()
        if store_on and valid_id(sid):
            open_conversation(sid)

    # A question the last run never finished (the page was clicked while it was answered).
    reloaded = {}

    def load_session(sid):
        reloaded["payload"] = client.session(sid)
        return reloaded["payload"]

    st.session_state.shown, cut = settle_cut_off(st.session_state.shown, st.session_state.sid,
                                                 load_session)
    if cut:
        st.session_state.interrupted = cut
    elif reloaded.get("payload"):                  # the API had finished it: its total too
        st.session_state.spend = dict(reloaded["payload"].get("spend") or NO_SPEND)

    # ---- sidebar: conversations only ------------------------------------------------
    with st.sidebar:
        if st.button("New conversation", use_container_width=True, type="primary"):
            start_fresh()
            st.rerun()
        if store_on:
            # The newest saved conversations (#5). Titles come from the API, where a refused
            # question is only its placeholder. One saved by this run's answer shows from
            # the next click on.
            st.caption("Recent")
            try:
                recent = client.recent(RECENT)
            except ApiError:
                recent = None
            if recent is None:
                st.caption("Couldn't list saved conversations right now.")
            elif not recent:
                st.caption("No saved conversations yet.")
            for r in recent or []:
                rid = r.get("session_id")
                if not valid_id(rid):
                    continue
                here = rid == st.session_state.sid
                if st.button(recent_label(r, st.session_state.sid), key=f"recent_{rid}",
                             use_container_width=True, disabled=here):
                    open_conversation(rid)
                    st.rerun()
        st.divider()
        fold = st.toggle("Fold earlier questions", value=True, key=FOLD_KEY,
                         help="On: only the latest question and answer stay open, the rest "
                              "fold into one line. Off: the whole conversation, as a chat.")
        save_box = st.empty()                      # is this conversation being kept (#5)

    def delete_control():
        """The bin icon left of the question box: deleted at once, after a confirm in its
        popover (11 Nov 2026; moved there from the sidebar the same day)."""
        with st.popover(DELETE_ICON, help=DELETE_LABEL):
            st.markdown(f"**{DELETE_LABEL}**")
            st.caption(DELETE_WARNING)
            if st.button("Delete", type="primary", key="delete_confirm",
                         use_container_width=True):
                why = delete_conversation(client, st.session_state.sid)
                if why is None:
                    start_fresh()
                    st.session_state.delete_error = None
                    st.rerun()
                st.session_state.delete_error = why

    def show_saved():
        ok, text = save_line(health.get("sessions"), st.session_state.get("save_error"))
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
    interrupted = st.session_state.pop("interrupted", None)
    if interrupted:
        st.info(interrupted)

    def show_spend():
        # Spend (#7): this conversation's cost, added up from each answer's cost (exact,
        # and the same after a reload); the key's total against its cap from the API, which
        # asks the proxy with its own key (about a minute behind).
        sp = st.session_state.spend
        ks = key_budget()
        with usage_box.container(), st.popover(
                USAGE_ICON, help=usage_help(model, float(sp.get("cost_usd") or 0),
                                            bool(sp.get("complete", True)))):
            st.caption(f"Model: {model}")
            st.markdown(md_plain(spend_line(sp)))
            st.markdown(md_plain(ks.get("line") or "Spend not available."))
        if ks.get("warn") or not ks.get("ok"):
            warn_box.warning(md_plain(ks.get("line") or "Spend not available."))
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
    # The Delete icon (a bin) sits left of the box, in the pinned bottom bar.
    can_delete = show_delete(store_on and valid_id(st.session_state.sid), st.session_state.shown)
    bottom = getattr(st, "bottom", None)           # Streamlit's pinned bottom container
    if can_delete and bottom is not None:
        with bottom:
            st.markdown(INPUT_MARKER, unsafe_allow_html=True)   # for INPUT_CSS; hidden
            del_col, box_col = st.columns(INPUT_BAR)
            with del_col:
                delete_control()
            with box_col:
                typed = st.chat_input(INPUT_PLACEHOLDER)
        import streamlit.components.v1 as components
        components.html(fit_bin_script(len(st.session_state.shown)), height=0)  # bin height
    else:
        typed = st.chat_input(INPUT_PLACEHOLDER)
        if can_delete:
            with st.sidebar:                       # no bottom bar in this Streamlit: fall back
                delete_control()
    if st.session_state.get("delete_error"):
        st.warning(f"Not deleted: {st.session_state.delete_error}.")
    question = typed or picked

    for m in st.session_state.shown:               # photos: fetched once, when first drawn
        attach_photos(m, fetch_photo)
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
        entry, problem, may_finish = None, None, False
        with st.status("Asking the agent...", expanded=False) as status:
            try:
                for evt in client.ask_stream(question, st.session_state.sid):
                    kind, data = evt.get("event"), evt.get("data") or {}
                    if kind == "session" and valid_id(data.get("session_id")):
                        # Known before the answer: a cut-off question can still be found.
                        st.session_state.sid = data["session_id"]
                        st.query_params["session"] = st.session_state.sid
                    elif kind == "step":
                        status.update(label=step_line(data))
                    elif kind == "answer":
                        entry = answer_message(data)
                        st.session_state.save_error = data.get("save_error")
                        st.session_state.spend = spend_after(st.session_state.spend, data)
                    elif kind == "error":
                        problem, may_finish = error_text(data)
            except ApiError as e:
                problem, may_finish = e.message, e.status == 0
            if entry is None and problem is None:
                problem, may_finish = "The answer stopped arriving.", True
            status.update(label="Answered" if entry else "Not answered",
                          state="complete" if entry else "error")
        if entry is None:
            st.session_state.shown.pop()           # the question wasn't answered here
            more = (" It may still be answered: reopen this conversation from the list in a "
                    "moment." if may_finish and valid_id(st.session_state.sid) else "")
            st.error(md_plain(problem + more))
            return
        attach_photos(entry, fetch_photo)
        # On the page before the top bar is redrawn, so the conversation's total includes it.
        st.session_state.shown.append(entry)
        key_budget.clear()                         # re-read the key's spend
        show_spend()                               # and redraw the top bar with this question
        render_answer(st, entry, latest=True)
    show_saved()


if __name__ == "__main__":
    main()