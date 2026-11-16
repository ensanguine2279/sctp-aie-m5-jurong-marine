"""Tests for frontend/agent_ui.py, the Streamlit page (no Streamlit needed: it is imported
inside main()). Since phase 3 (15 Nov 2026) the page only talks to the agent API, so these
test what it draws and how it reads the API's replies; the end-to-end tests drive the page's
helpers through api_client's ServiceTransport, the real AgentService and the real agent graph.
How an answer is built and kept (the trace table, cost lines, the saved copy) is tested in
tests/test_answer_view.py.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
from datetime import datetime

import agent_ui as ui
import api_client as ac
import conversations as conv
from test_agent_api import KEY, conversations, scripted, service  # noqa: F401


def test_the_page_imports_no_agent_database_or_key_code():
    """The page holds no database login, no LiteLLM key and no agent: only the API's address
    and key (phase 3). Nothing in it may import the server side."""
    tree = ast.parse(inspect.getsource(ui))
    top = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            top |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            top.add(n.module.split(".")[0])
    assert not top & {"agent_app", "agent_tools", "agent_graph", "sessions", "spend",
                      "guardrail", "db_config", "conversations", "sqlalchemy", "openai",
                      "answer_view", "agent_api"}
    assert "api_client" in top


def test_the_page_folder_holds_only_the_page_and_its_client():
    """The page's image is built from frontend/ alone (16 Nov 2026): every module the page
    imports from the repo must be in that folder, so nothing of the agent's can be needed."""
    import pathlib
    folder = pathlib.Path(ui.__file__).resolve().parent
    assert folder.name == "frontend"
    assert pathlib.Path(ac.__file__).resolve().parent == folder
    mine = {p.stem for p in folder.glob("*.py")}
    assert mine == {"agent_ui", "api_client"}
    docker = (folder / "Dockerfile").read_text()
    assert "COPY agent_ui.py api_client.py" in docker and "scripts" not in docker


def test_the_conversations_spend_is_the_apis_when_saved_and_added_up_when_not():
    saved = {"session_id": "a" * 32, "usage": {"cost_usd": 0.002},
             "spend": {"cost_usd": 0.005, "questions": 3, "complete": True,
                       "line": "This conversation: $0.0050 over 3 questions"}}
    prev = {"cost_usd": 0.003, "questions": 2, "complete": True}
    assert ui.spend_after(prev, saved) == saved["spend"]           # the API's figure, as is
    unsaved = {"session_id": None, "usage": {"cost_usd": 0.002, "cost_complete": True},
               "spend": {"cost_usd": 0.002, "questions": 1, "complete": True}}
    got = ui.spend_after(prev, unsaved)
    assert got == {"cost_usd": 0.005, "questions": 3, "complete": True}
    assert ui.spend_line(got) == "This conversation: $0.0050 over 3 questions"
    unknown = ui.spend_after(ui.NO_SPEND, {"usage": {"cost_usd": None}})
    assert unknown["complete"] is False and ui.spend_line(unknown).startswith(
        "This conversation: at least $0.0000 over 1 question")
    assert ui.spend_after(ui.NO_SPEND, {"usage": {"cost_usd": True}})["cost_usd"] == 0.0
    assert ui.spend_line(saved["spend"]) == saved["spend"]["line"]


def test_the_page_password_check():
    assert ui.password_ok("s3cret", "s3cret")
    assert not ui.password_ok("S3cret", "s3cret")
    assert not ui.password_ok("", "s3cret")


# --------------------------------------------------------------------- photos ---

PLATE = {"sha256": "a" * 64, "what": "CRN-02 nameplate photo", "file": "nameplate_CRN-02.jpg"}
FORM = {"sha256": "b" * 64, "what": "Scanned form IR-2025-914", "file": "IR-2025-914.jpg"}


def fake_fetch(sha):
    return {"a" * 64: {"bytes": b"plate", "mime": "image/jpeg", "variant": "as_is"},
            "b" * 64: {"bytes": b"form", "mime": "image/jpeg", "variant": "redacted"}}.get(sha)


def test_each_cited_scan_gets_its_photo_from_the_api():
    items = ui.photo_items(fake_fetch, [PLATE, FORM])
    assert [i["file"] for i in items] == ["nameplate_CRN-02.jpg", "IR-2025-914.jpg"]
    assert items[0]["caption"] == "CRN-02 nameplate photo: nameplate_CRN-02.jpg"
    assert items[1]["caption"].endswith("(person and signature removed)")


def test_a_photo_that_cant_be_shown_says_so_instead():
    missing = dict(PLATE, sha256="e" * 64, file="nameplate_X.jpg")
    assert ui.photo_items(fake_fetch, [missing]) == [
        {"note": "CRN-02 nameplate photo: nameplate_X.jpg - photo not available here."}]

    def down(sha):
        raise ac.ApiError(0, "unreachable", "Can't reach the agent API")
    assert ui.photo_items(down, [PLATE]) == [
        {"note": "CRN-02 nameplate photo: nameplate_CRN-02.jpg - photo can't be fetched "
                 "right now."}]


def test_photos_are_fetched_once_per_answer_and_only_for_answers():
    calls = []

    def fetch(sha):
        calls.append(sha)
        return fake_fetch(sha)
    m = {"role": "assistant", "text": "a", "images": [PLATE]}
    ui.attach_photos(m, fetch)
    ui.attach_photos(m, fetch)                                 # redrawn: not fetched again
    assert calls == ["a" * 64] and m["photos"][0]["bytes"] == b"plate"
    q = {"role": "user", "text": "q", "images": [PLATE]}
    assert "photos" not in ui.attach_photos(q, fetch)
    assert "photos" not in ui.attach_photos({"role": "assistant", "text": "a"}, fetch)


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


class FakeSt:
    def __init__(self):
        self.md, self.expanders = [], []

    def markdown(self, text, **kw):
        self.md.append(text)

    def expander(self, label, expanded=False):
        self.expanders.append((label, expanded))
        return contextlib.nullcontext()


def test_the_tools_notes_are_a_plain_list_inside_their_tab():
    """10 Nov 2026: the notes moved from a closed expander into the answer's Caveats tab,
    which is just as closed until clicked; an expander inside a tab is clutter."""
    st = FakeSt()
    ui.show_notes(st, ["read off the scan by a person", "trap list C6"])
    assert st.md == ["- read off the scan by a person\n- trap list C6"]
    assert st.expanders == []
    st2 = FakeSt()
    ui.show_notes(st2, [])
    assert st2.md == []


# ------------------------------------------------------- answers from the API ---

def test_each_streamed_step_becomes_a_status_line_with_no_question_text():
    assert ui.step_line({"node": "guard_in", "passed": True}) == \
        "Question checked by the guardrail"
    assert "refused" in ui.step_line({"node": "guard_in", "passed": False})
    assert ui.step_line({"node": "model", "round": 1, "tool_calls": ["find_sop", "get_sop"]}) == \
        "Model (round 1) asked for: find_sop, get_sop"
    assert ui.step_line({"node": "model", "round": 2, "tool_calls": []}) == \
        "Model (round 2) wrote the answer"
    assert ui.step_line({"node": "model", "round": 3, "failed": True}) == \
        "Model call failed (round 3)"
    assert "too many" in ui.step_line({"node": "model", "round": 9, "stopped": "max_rounds"})
    assert ui.step_line({"node": "tools", "round": 1, "tools": ["list_machines"]}) == \
        "Tools (round 1) ran: list_machines"
    assert ui.step_line({"node": "guard_out", "answer_replaced": True}).startswith(
        "Answer replaced")
    assert ui.step_line({"node": "guard_out", "answer_replaced": False}) == \
        "Answer checked by the guardrail"


def test_an_answer_from_the_api_is_drawn_with_everything_it_carries():
    body = {"answer": "Twelve.", "sources": ["CMMS machines"], "notes": ["n"],
            "images": [PLATE], "trace": [{"step": "model"}], "cost_line": "1 model call",
            "usage": {"cost_usd": 0.0004, "cost_complete": False}}
    m = ui.answer_message(body)
    assert m == {"role": "assistant", "text": "Twelve.", "sources": ["CMMS machines"],
                 "notes": ["n"], "images": [PLATE], "trace": [{"step": "model"}],
                 "cost": "1 model call", "cost_usd": 0.0004, "cost_complete": False}
    bare = ui.answer_message({"answer": "x", "usage": {"cost_usd": None}})
    assert bare["cost_usd"] == 0.0 and bare["sources"] == [] and bare["cost_complete"] is True


def test_an_error_event_says_whether_the_question_may_still_finish():
    late = {"status": 504, "error": {"code": "timeout", "message": "No progress for 300 s"}}
    assert ui.error_text(late) == ("No progress for 300 s", True)
    bad = {"status": 422, "error": {"code": "bad_question", "message": "question must be..."}}
    assert ui.error_text(bad) == ("question must be...", False)
    assert ui.error_text({})[1] is False


def test_every_example_button_is_answered_and_the_pdpa_one_is_refused():
    import guardrail
    verdicts = {q: guardrail.check_input(q) for q in ui.EXAMPLES}
    assert [q for q, v in verdicts.items() if not v.allowed] == \
        ["What is the name of employee EMP015?"]


def test_only_the_guardrail_example_has_a_tooltip():
    helps = {q: ui.example_help(q) for q in ui.EXAMPLES}
    assert [q for q, h in helps.items() if h] == ["What is the name of employee EMP015?"]


# ----------------------------------------------- the page layout (10 Nov 2026) ---


def test_each_answer_gets_a_tab_only_for_what_it_has_with_counts():
    m = {"role": "assistant", "text": "a", "sources": ["CMMS", "sensor log"],
         "notes": ["trap list C6"], "photos": [], "trace": [{"step": "model"}]}
    assert ui.evidence_tabs(m) == [("sources", "Sources (2)"), ("notes", "Caveats (1)"),
                                   ("trace", "What happened (1 step)")]
    assert ui.evidence_tabs({"role": "assistant", "text": "a"}) == []
    reloaded = {"role": "assistant", "text": "a", "sources": ["CMMS"]}   # no trace once saved
    assert ui.evidence_tabs(reloaded, latest=True) == [("sources", "Sources (1)")]


def test_on_the_newest_answer_what_happened_is_the_first_tab():
    """Streamlit opens the first tab: the demo shows the path of the newest question."""
    m = {"sources": ["CMMS"], "photos": [{"note": "x"}], "trace": [{}, {}, {}]}
    assert [k for k, _ in ui.evidence_tabs(m, latest=True)] == ["trace", "sources", "photos"]
    assert [k for k, _ in ui.evidence_tabs(m)] == ["sources", "photos", "trace"]
    assert ui.evidence_tabs(m, latest=True)[0][1] == "What happened (3 steps)"


def test_the_newest_answer_is_found():
    shown = [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"},
             {"role": "user", "text": "q2"}]
    assert ui.last_answer(shown) == 1
    assert ui.last_answer([]) is None and ui.last_answer([{"role": "user"}]) is None


def test_the_usage_button_is_an_icon_with_model_and_cost_in_its_tooltip():
    assert ui.USAGE_ICON.startswith(":material/")                    # no words on the button
    assert ui.EXAMPLES_ICON.startswith(":material/") and ui.EXAMPLES_HELP   # nor on Examples
    assert ui.usage_help("gpt-4o-mini", 0.0031) == r"gpt\-4o\-mini · \$0\.0031"
    assert ui.usage_help("m", 0.0004, complete=False) == r"m · at least \$0\.0004"


def test_a_new_question_is_scrolled_into_view():
    """10 Nov 2026: a question picked from the examples was added below the visible page
    and the user didn't see it; the page now scrolls to the newest message."""
    a, b = ui.scroll_script(3), ui.scroll_script(5)
    assert a != b                                  # a fresh component per question
    assert "stChatMessage" in a and "scrollIntoView" in a and "window.parent" in a


def test_only_the_latest_exchange_stays_open():
    """10 Nov 2026: earlier questions fold into one expander, the latest stays open."""
    q1, a1 = {"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}
    q2, a2 = {"role": "user", "text": "q2"}, {"role": "assistant", "text": "a2"}
    assert ui.split_latest([q1, a1, q2, a2]) == ([q1, a1], [q2, a2])
    assert ui.split_latest([q1, a1]) == ([], [q1, a1])               # nothing to fold
    assert ui.split_latest([q1, a1, q2]) == ([q1, a1], [q2])          # answer still coming
    assert ui.split_latest([]) == ([], [])
    assert ui.earlier_label([q1, a1]) == "Earlier in this conversation (1 question)"
    assert ui.earlier_label([q1, a1, q2, a2]) == "Earlier in this conversation (2 questions)"


def test_folding_earlier_questions_can_be_switched_off():
    """10 Nov 2026: a sidebar toggle between folding earlier questions and a plain chat."""
    q1, a1 = {"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}
    q2, a2 = {"role": "user", "text": "q2"}, {"role": "assistant", "text": "a2"}
    shown = [q1, a1, q2, a2]
    assert ui.history_parts(shown, new_question=False, fold=True) == ([q1, a1], [q2, a2])
    assert ui.history_parts(shown, new_question=True, fold=True) == (shown, [])
    assert ui.history_parts(shown, new_question=False, fold=False) == ([], shown)
    assert ui.history_parts(shown, new_question=True, fold=False) == ([], shown)
    assert ui.history_parts(shown, False, fold=False)[1] is not shown   # a copy, not the list


def test_the_space_above_the_title_is_reduced_and_the_usage_panel_is_wide_enough():
    assert "padding-top" in ui.TOP_CSS and "stMainBlockContainer" in ui.TOP_CSS
    assert "stPopoverBody" in ui.TOP_CSS and "min-width" in ui.TOP_CSS
    # 10 Nov 2026: the icon buttons were cut off in columns of 0.7 against 10.
    assert '[data-testid="stPopover"] button' in ui.TOP_CSS
    # 10 Nov 2026: the two icons sat far apart; their columns now shrink to the buttons.
    assert 'id="jme-topbar"' in ui.TOP_MARKER and ":has(#jme-topbar)" in ui.TOP_CSS
    assert "flex: 0 0 auto" in ui.TOP_CSS
    title, ex, usage = ui.TOP_BAR
    assert ex / sum(ui.TOP_BAR) >= 0.1 and usage / sum(ui.TOP_BAR) >= 0.1


# ------------------------------------------- the conversation kept (#5) ---

def test_a_reopened_conversation_draws_only_its_questions_and_answers():
    payload = {"messages": [{"role": "user", "text": "q"}, {"role": "system", "text": "x"},
                            {"role": "assistant"}, "junk",
                            {"role": "assistant", "text": "a", "images": [PLATE]}]}
    assert ui.restored(payload) == [{"role": "user", "text": "q"},
                                    {"role": "assistant", "text": "a", "images": [PLATE]}]
    assert ui.restored(None) is None and ui.restored({"error": {}}) is None
    back = ui.restored(payload)
    back[0]["text"] = "changed"
    assert payload["messages"][0]["text"] == "q"                   # a copy, not the reply


def test_the_sidebar_says_honestly_whether_the_conversation_is_kept():
    assert ui.save_line("on", None)[0]
    ok, text = ui.save_line("off (CONV_DB_URL isn't set)", None)
    assert not ok and "aren't being saved" in text and "CONV_DB_URL isn't set" in text
    assert "without the earlier ones" in text                    # follow-ups have no context
    ok, text = ui.save_line("on", "couldn't save the conversation (OperationalError)")
    assert not ok and "isn't being saved right now" in text
    assert not ui.save_line(None, None)[0]


def test_how_long_ago_a_conversation_was_saved():
    from datetime import datetime
    now = datetime(2026, 11, 9, 12, 0)
    assert ui.ago(datetime(2026, 11, 9, 11, 59, 30), now) == "just now"
    assert ui.ago("2026-11-09 11:55:00", now) == "5 min ago"           # sqlite gives text
    assert ui.ago(datetime(2026, 11, 9, 9, 0), now) == "3 h ago"
    assert ui.ago(datetime(2026, 11, 7, 12, 0), now) == "2 d ago"
    assert ui.ago(datetime(2026, 11, 9, 12, 5), now) == "just now"     # a clock ahead
    assert ui.ago(None, now) == "" and ui.ago("yesterday", now) == ""


def test_each_recent_conversation_line_and_the_open_one():
    now = datetime(2026, 11, 9, 12, 0)
    r = {"session_id": "a" * 32, "title": "Is CNC-02 running differently now?", "turns": 2,
         "updated_at": "2026-11-09T09:00:00+00:00"}                   # as GET /sessions sends it
    assert ui.recent_label(r, "b" * 32, now) == \
        r"Is CNC\-02 running differently now? \(2 questions, 3 h ago\)"
    assert ui.recent_label(r, "a" * 32, now).endswith(r"\(2 questions, open now\)")
    one = {"session_id": "c" * 32, "title": "cost *was* $5 [x]", "turns": 1, "updated_at": None}
    assert ui.recent_label(one, None, now) == r"cost \*was\* \$5 \[x\] \(1 question\)"


def test_listing_and_reopening_through_the_api(scripted):  # noqa: F811
    c = ac.ApiClient(None, KEY, transport=ac.ServiceTransport(service(conversations())))
    scripted("Twelve.")
    sid = c.ask("how many machines?")["session_id"]
    rows = c.recent(ui.RECENT)
    assert [r["session_id"] for r in rows] == [sid]
    assert ui.recent_label(rows[0], sid).endswith(r"open now\)")
    shown = ui.restored(c.session(sid))
    assert [m["text"] for m in shown] == ["how many machines?", "Twelve."]


# ------------------- a question cut off by a click, and deleting (11 Nov 2026) ---

def test_a_cut_off_question_is_shown_answered_if_the_api_finished_it(scripted):  # noqa: F811
    """The click stopped the page, not the API: it answered and saved the question in its own
    thread, so reloading the conversation draws the answer and says nothing (15 Nov 2026)."""
    c = ac.ApiClient(None, KEY, transport=ac.ServiceTransport(service(conversations())))
    scripted("One.")
    sid = c.ask("first")["session_id"]
    scripted("Two.")
    c.ask("second", sid)                                     # finished while the page was gone
    page = [{"role": "user", "text": "first"}, {"role": "assistant", "text": "One."},
            {"role": "user", "text": "second"}]
    shown, note = ui.settle_cut_off(page, sid, c.session)
    assert note is None and [m["text"] for m in shown][-2:] == ["second", "Two."]


def test_a_cut_off_question_not_yet_answered_is_taken_off_and_noted(scripted):  # noqa: F811
    c = ac.ApiClient(None, KEY, transport=ac.ServiceTransport(service(conversations())))
    scripted("One.")
    sid = c.ask("first")["session_id"]
    page = [{"role": "user", "text": "first"}, {"role": "assistant", "text": "One."},
            {"role": "user", "text": "What is the name of employee EMP015?"}]
    shown, note = ui.settle_cut_off(page, sid, c.session)
    assert [m["text"] for m in shown] == ["first", "One."]
    assert "wasn't answered" in note and "reopen this conversation" in note
    assert "EMP015" not in note                              # the question isn't repeated
    # A new conversation cut off before the API sent its id: nothing to reload.
    shown, note = ui.settle_cut_off([{"role": "user", "text": "q"}], None, c.session)
    assert shown == [] and "Ask it again" in note


def test_an_answered_page_or_an_unreachable_api_is_left_as_is():
    page = [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}]

    def never(sid):
        raise AssertionError("nothing to reload")
    assert ui.settle_cut_off(page, "a" * 32, never) == (page, None)
    assert ui.settle_cut_off([], None, never) == ([], None)

    def down(sid):
        raise ac.ApiError(0, "unreachable", "Can't reach the agent API")
    shown, note = ui.settle_cut_off(page + [{"role": "user", "text": "q2"}], "a" * 32, down)
    assert shown == page and "wasn't answered" in note


def test_delete_this_conversation_goes_through_the_api(scripted):  # noqa: F811
    convs = conversations()
    c = ac.ApiClient(None, KEY, transport=ac.ServiceTransport(service(convs)))
    scripted("Twelve.")
    sid = c.ask("how many machines?")["session_id"]
    assert ui.delete_conversation(c, sid) is None
    assert c.recent(10) == [] and not convs.threads.get(sid).found
    assert ui.delete_conversation(c, None) is None              # never saved: nothing to do
    down = ac.ApiClient(None, KEY, transport=ac.ServiceTransport(service(
        conversations(down=True))))
    why = ui.delete_conversation(down, conv.new_id())
    assert why and "db.internal" not in why                     # no connection details shown
    assert "for good" in ui.DELETE_WARNING and ui.DELETE_LABEL == "Delete this conversation"


def test_the_bin_sits_left_of_the_question_box_once_there_is_something_to_delete():
    """11 Nov 2026: Delete moved from the sidebar to an icon left of the chat box."""
    q, a = {"role": "user", "text": "q"}, {"role": "assistant", "text": "a"}
    assert ui.show_delete(True, [q, a])
    assert not ui.show_delete(True, [])                  # a new conversation: nothing saved
    assert not ui.show_delete(True, [q])                 # no answer yet, so not saved yet
    assert not ui.show_delete(False, [q, a])             # conversations aren't being saved
    assert ui.DELETE_ICON.startswith(":material/")       # an icon; the tooltip says what
    assert 'id="jme-inputbar"' in ui.INPUT_MARKER and ":has(#jme-inputbar)" in ui.INPUT_CSS
    assert "display: none" in ui.INPUT_CSS               # the marker itself takes no room
    assert "flex: 0 0 auto" in ui.INPUT_CSS              # the bin's column shrinks to the icon
    # The bin as tall as the question box, set from the box's measured height (CSS alone
    # didn't, 11 Nov 2026), and kept so as the box grows; a fresh component per run.
    js = ui.fit_bin_script(4)
    assert "stChatInput" in js and "stPopover" in js and "window.parent" in js
    assert "getBoundingClientRect().height" in js and "ResizeObserver" in js
    assert js != ui.fit_bin_script(5)
    # 11 Nov 2026: no down arrow on the bin, also when Streamlit redraws it.
    assert "expand_more" in js and "expand_less" in js and "MutationObserver" in js
    # 20:30-20:38: a square, not a thin bar: the width set with priority, and the wrappers up
    # to the bin's column given room (a narrow, clipping column had kept it thin).
    assert "['width', h]" in js and "'important'" in js and "['min-width', h]" in js
    assert "['overflow', 'visible']" in js and "e === col" in js
    # 21:12: a shorter question box (the bin follows it): its padding and line height trimmed.
    assert f"padding-top: {ui.INPUT_PAD} !important" in ui.INPUT_CSS
    assert f"line-height: {ui.INPUT_LINE} !important" in ui.INPUT_CSS
    assert "PAD" not in ui.INPUT_CSS and "LINE" not in ui.INPUT_CSS   # both filled in
    # 22:42: a bigger bin icon, sized from the button's height.
    assert "t === 'delete'" in js and f"* {ui.DELETE_ICON_SCALE})" in js
    assert 0.4 <= ui.DELETE_ICON_SCALE <= 0.9
    bin_col, box = ui.INPUT_BAR
    assert box > 10 * bin_col                            # without :has(), the box still wide