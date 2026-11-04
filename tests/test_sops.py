"""Tests for scripts/ingest_sops.py. No database needed: SOP files are built as bytes,
including the client's three SOPs exactly as delivered (em dash, no final newline).
One test reads the real folder data/sops/ if it is there, and is skipped otherwise."""
from __future__ import annotations

from pathlib import Path

import pytest

import ingest_sops as sp
from ingest_sops import SopFile

ROOT = Path(__file__).resolve().parents[1]
DASH = "—"

LOTO = (f"JME SOP-LOTO-01 {DASH} LOCKOUT/TAGOUT PROCEDURE (rev 4, Jan 2026)\n"
        "1. Identify all energy sources (electrical, hydraulic, pneumatic, gravity).\n"
        "2. Notify affected personnel and shift supervisor.\n"
        "3. Shut down machine using normal stop procedure.\n"
        "4. Isolate at main breaker; apply personal lock + tag. One lock per person working.\n"
        "5. Verify zero energy: attempt restart, test with multimeter (electricians only).\n"
        "6. For CNC machines: also release residual hydraulic pressure via manual valve "
        "(see machine manual).\n"
        "7. After work: ensure all guards restored, all personnel clear, remove locks "
        "(owner only), notify supervisor.\n"
        "VIOLATION = immediate stop-work + incident report. No exceptions, including "
        "'quick jobs'.")
CRANE = (f"JME SOP-CRANE-02 {DASH} OVERHEAD CRANE LIFTING (rev 6, Mar 2026)\n"
         "- Only certified crane operators (list with HR) may operate CRN-01/CRN-02.\n"
         "- Inspect slings/shackles before EVERY lift. Discard slings with >10% wear, kinks, "
         "or broken strands.\n"
         "- Max load: CRN-01 = 10,000 kg, CRN-02 = 2,000 kg. NEVER bypass limit switch.\n"
         "- Tandem lifts require lifting plan signed by PE (Professional Engineer).\n"
         "- No person under suspended load. Use tag lines for long items.\n"
         "- After the Aug 2025 near-miss (sling snapped), ALL slings >2 years old were scrapped. "
         "Check purchase date on sling tag.")
CONFINED = (f"JME SOP-CONFINED-03 {DASH} CONFINED SPACE ENTRY (rev 2, Sep 2025)\n"
            "Applies to: blasting chamber (BLAST-01), ballast tanks on client vessels, "
            "test pump sump.\n"
            "- Entry permit required from WSH officer. Valid 8 hours max.\n"
            "- Gas test before entry AND every 2 hours: O2 19.5-23.5%, LEL <10%, H2S <10ppm, "
            "CO <30ppm.\n"
            "- Attendant stationed outside at all times with communication.\n"
            "- MOM notification required for vessel tank entry (Shipyard regs).")
EMAIL = ("From: kc.tan@jurongmarine.sg\nTo: digital-project@jurongmarine.sg\n"
         "Subject: MOM audit coming\n\nTeam,\nCNC-02 been feeling 'off'.\nKC Tan\n")

MACHINES = {"CNC-01": "CNC Lathe", "CNC-02": "CNC Lathe", "CNC-03": "5-axis Machining Ctr",
            "CRN-01": "Overhead Crane 10T", "CRN-02": "Jib Crane 2T",
            "BLAST-01": "Blasting Chamber", "PMP-T01": "Test Pump Rig"}


def f(name: str, text: str, encoding: str = "utf-8") -> SopFile:
    return SopFile(name, text.encode(encoding))


def client_folder() -> list[SopFile]:
    return [f("0000_gm_brief_email.txt", EMAIL), f("SOP-CONFINED-03.txt", CONFINED),
            f("SOP-CRANE-02.txt", CRANE), f("SOP-LOTO-01.txt", LOTO)]


def doc(out, doc_no):
    return out["sops"].set_index("doc_no").loc[doc_no]


# ------------------------------------------------------------------ reading ---

def test_header_gives_number_title_revision_and_month():
    d = sp.parse_document(f("SOP-LOTO-01.txt", LOTO))
    assert (d.doc_no, d.title, d.revision, d.rev_month, d.rev_label) == \
        ("SOP-LOTO-01", "LOCKOUT/TAGOUT PROCEDURE", 4, "2026-01", "rev 4, Jan 2026")
    assert d.reject_reason is None


def test_em_dash_survives_where_the_windows_default_would_garble_it():
    """Trap list D2: cp1252 turns the em dash into 'â€”'."""
    data = LOTO.encode("utf-8")
    assert "â€”" in data.decode("cp1252")         # what open() gives on Windows
    assert sp.decode(data).startswith(f"JME SOP-LOTO-01 {DASH} LOCKOUT")


def test_bom_and_windows_line_endings_are_read_like_plain_utf8():
    plain = sp.parse_document(f("SOP-LOTO-01.txt", LOTO))
    crlf = sp.parse_document(SopFile("SOP-LOTO-01.txt",
                                     b"\xef\xbb\xbf" + LOTO.replace("\n", "\r\n").encode()))
    assert [(s.line_no, s.text) for s in crlf.steps] == [(s.line_no, s.text) for s in plain.steps]
    assert crlf.title == plain.title


def test_a_file_that_is_not_utf8_is_rejected_not_guessed():
    out = sp.process([SopFile("SOP-LOTO-01.txt", LOTO.encode("utf-16"))])
    assert out["raw"].iloc[0]["reject_reason"] == "not_utf8"
    assert len(out["sops"]) == 0


def test_hyphen_instead_of_em_dash_is_still_a_header():
    d = sp.parse_document(f("SOP-LOTO-01.txt", LOTO.replace(DASH, "-")))
    assert (d.doc_no, d.title, d.revision) == ("SOP-LOTO-01", "LOCKOUT/TAGOUT PROCEDURE", 4)


def test_header_without_a_revision_loads_with_a_warning():
    d = sp.parse_document(f("SOP-X-01.txt", f"JME SOP-X-01 {DASH} TEST\n- step"))
    assert (d.revision, d.rev_month, d.reject_reason) == (None, None, None)
    assert "no_revision_in_header" in d.warnings


# ---------------------------------------------------------------- rejecting ---

def test_the_gm_email_is_not_an_sop_and_keeps_only_its_raw_row():
    """Trap list D1."""
    out = sp.process(client_folder())
    raw = out["raw"].set_index("source_file")
    assert raw.loc["0000_gm_brief_email.txt", "reject_reason"] == "not_an_sop"
    assert raw.loc["0000_gm_brief_email.txt", "raw_text"].startswith("From:")   # raw only
    assert "kc.tan" not in " ".join(out["sops"]["full_text"])
    assert sorted(out["sops"]["doc_no"]) == ["SOP-CONFINED-03", "SOP-CRANE-02", "SOP-LOTO-01"]


def test_a_rejected_file_carries_no_warnings():
    out = sp.process(client_folder())
    rej = out["raw"][out["raw"]["status"] == "rejected"]
    assert rej["warnings"].isna().all()


def test_same_file_twice_is_a_duplicate_of_the_first():
    out = sp.process([f("SOP-LOTO-01.txt", LOTO), f("SOP-LOTO-01 copy.txt", LOTO)], run_id=7)
    raw = out["raw"]
    assert raw["reject_reason"].isna().tolist() == [True, False]
    assert raw.iloc[1]["reject_reason"] == "duplicate_file"
    assert raw.iloc[1]["duplicate_of_raw_id"] == 7001


def test_same_revision_with_different_text_is_a_conflict():
    edited = LOTO.replace("One lock per person working.", "One lock per crew.")
    out = sp.process([f("SOP-LOTO-01.txt", LOTO), f("SOP-LOTO-01b.txt", edited)])
    assert out["raw"].iloc[1]["reject_reason"] == "duplicate_revision_conflict"


def test_every_revision_is_kept_and_only_the_highest_is_current():
    """Trap list D9: earlier revisions, once the client sends them, must not be lost."""
    rev3 = LOTO.replace("rev 4, Jan 2026", "rev 3, Mar 2024")
    out = sp.process([f("SOP-LOTO-01.txt", LOTO), f("SOP-LOTO-01-rev3.txt", rev3)])
    s = out["sops"].set_index("revision")
    assert (s.loc[4, "is_current"], s.loc[3, "is_current"]) == (1, 0)
    assert s.loc[3, "rev_month"] == "2024-03"


# -------------------------------------------------------------------- steps ---

def test_numbered_steps_bullets_and_a_closing_line_keep_their_order_and_lines():
    d = sp.parse_document(f("SOP-LOTO-01.txt", LOTO))
    assert [(s.step_no, s.line_no, s.marker) for s in d.steps][:2] == [(1, 2, "1."), (2, 3, "2.")]
    assert (d.steps[-1].step_no, d.steps[-1].line_no, d.steps[-1].marker) == (8, 9, "")
    assert d.steps[-1].text.startswith("VIOLATION")
    c = sp.parse_document(f("SOP-CRANE-02.txt", CRANE))
    assert {s.marker for s in c.steps} == {"-"} and len(c.steps) == 6


def test_applies_to_is_kept_apart_from_the_steps():
    d = sp.parse_document(f("SOP-CONFINED-03.txt", CONFINED))
    assert d.applies_to.startswith("blasting chamber (BLAST-01)")
    assert [s.line_no for s in d.steps] == [3, 4, 5, 6]
    assert d.steps[1].text == ("Gas test before entry AND every 2 hours: O2 19.5-23.5%, "
                               "LEL <10%, H2S <10ppm, CO <30ppm.")   # limits exactly as written


def test_documents_not_in_the_data_are_flagged_on_their_step():
    """Trap list D8. Writing an incident report is an action, not a missing document."""
    loto = sp.parse_document(f("SOP-LOTO-01.txt", LOTO))
    assert loto.steps[5].refers_to == "machine manual"
    assert loto.steps[7].refers_to is None
    crane = sp.parse_document(f("SOP-CRANE-02.txt", CRANE))
    assert [s.refers_to for s in crane.steps if s.refers_to] == \
        ["certified operator list (HR)", "lifting plan (signed by PE)"]
    assert "refers_to_document_not_in_data" in crane.warnings


def test_doc_number_that_differs_from_the_file_name_is_flagged():
    d = sp.parse_document(f("crane.txt", CRANE))
    assert "doc_no_differs_from_file_name" in d.warnings


def test_personal_data_in_a_step_is_flagged():
    d = sp.parse_document(f("SOP-CRANE-02.txt", CRANE + "\n- Call Halim at 9123 4567 or halim@jme.sg"))
    assert "personal_data_in_text" in d.warnings
    assert "personal_data_in_text" not in sp.parse_document(f("SOP-CRANE-02.txt", CRANE)).warnings


# ----------------------------------------------------------------- machines ---

def test_machine_ids_names_and_aliases_are_linked_with_how():
    out = sp.process(client_folder(), machines=MACHINES)
    links = {(r.doc_no, r.machine_id): r.how for r in out["links"].itertuples()}
    assert links[("SOP-CRANE-02", "CRN-01")] == links[("SOP-CRANE-02", "CRN-02")] == "id"
    assert links[("SOP-CONFINED-03", "BLAST-01")] == "id"
    assert links[("SOP-CONFINED-03", "PMP-T01")] == "alias"          # 'test pump sump'
    assert {m for d, m in links if d == "SOP-LOTO-01"} == {"CNC-01", "CNC-02", "CNC-03"}


def test_a_cmms_machine_name_links_without_the_id():
    text = f"JME SOP-BLAST-04 {DASH} BLASTING (rev 1, Jan 2026)\n- Before entering the blasting chamber, check the door."
    d = sp.parse_document(f("SOP-BLAST-04.txt", text))
    assert sp.machine_links(d, MACHINES) == [("BLAST-01", "name", "blasting chamber")]
    assert sp.machine_links(d, None) == []                     # CMMS not loaded: ids only


def test_short_id_lists_are_expanded():
    text = f"JME SOP-X-01 {DASH} X (rev 1, Jan 2026)\n- Applies to CNC-01/02/03 only."
    d = sp.parse_document(f("SOP-X-01.txt", text))
    assert [m for m, _, _ in sp.machine_links(d)] == ["CNC-01", "CNC-02", "CNC-03"]


def test_an_id_the_cmms_does_not_have_is_flagged():
    text = f"JME SOP-X-01 {DASH} X (rev 1, Jan 2026)\n- Isolate CRN-03 first."
    out = sp.process([f("SOP-X-01.txt", text)], machines=MACHINES)
    assert "unknown_machine" in out["raw"].iloc[0]["warnings"]


# ------------------------------------------------------------------- search ---

@pytest.mark.parametrize("question,first", [
    ("What's the SOP before entering the blasting chamber?", "SOP-CONFINED-03"),
    ("What is the max load of CRN-01?", "SOP-CRANE-02"),
    ("How do I lock out CNC-02 before maintenance?", "SOP-LOTO-01"),
    ("gas test limits for the ballast tank", "SOP-CONFINED-03"),
])
def test_search_finds_the_right_sop(question, first):
    out = sp.process(client_folder(), machines=MACHINES)
    assert sp.search_sops(question, out["sops"], out["links"])[0][0] == first


PARAPHRASES = [
    ("Going inside the sandblasting booth, what do I need?", "SOP-CONFINED-03"),
    ("Isolating power on the lathe before changing the tool", "SOP-LOTO-01"),
]


@pytest.mark.parametrize("question,first", PARAPHRASES)
def test_search_finds_the_sop_for_a_paraphrase(question, first):
    """Questions that share no word with their SOP ('sandblasting booth' vs 'blasting
    chamber', 'isolating power' vs 'LOCKOUT'). Found through SYNONYMS."""
    out = sp.process(client_folder(), machines=MACHINES)
    assert sp.search_sops(question, out["sops"], out["links"])[0][0] == first


@pytest.mark.parametrize("question,_", PARAPHRASES)
def test_the_paraphrases_are_found_only_through_synonyms(question, _, monkeypatch):
    """Proves the paraphrase tests test SYNONYMS: without it, keyword search misses them."""
    out = sp.process(client_folder(), machines=MACHINES)
    monkeypatch.setattr(sp, "SYNONYMS", {})
    assert sp.search_sops(question, out["sops"], out["links"]) == []


def test_synonyms_match_whole_words_only():
    assert sp.expand_question("Isolate the pump") == "Isolate the pump lockout"
    assert sp.expand_question("an isolated reading") == "an isolated reading"
    assert sp.expand_question("lock out, tag out") == "lock out, tag out lockout tagout"


def test_search_returns_nothing_for_a_topic_no_sop_covers():
    """Trap list D3: the app must say 'no SOP in the data', not stretch another one."""
    out = sp.process(client_folder(), machines=MACHINES)
    assert sp.search_sops("welding hot work PPE for grinding", out["sops"], out["links"]) == []


def test_search_ignores_superseded_revisions():
    rev3 = LOTO.replace("rev 4, Jan 2026", "rev 3, Mar 2024")
    out = sp.process([f("SOP-LOTO-01.txt", LOTO), f("SOP-LOTO-01-rev3.txt", rev3)])
    assert [(d, r) for d, r, _ in sp.search_sops("lockout", out["sops"])] == [("SOP-LOTO-01", 4)]


def test_an_sop_is_quoted_whole_with_its_citation():
    out = sp.process(client_folder())
    q = sp.quote_sop("SOP-CONFINED-03", 2, out["sops"], out["steps"])
    assert q.splitlines()[0] == f"SOP-CONFINED-03 {DASH} CONFINED SPACE ENTRY (rev 2, Sep 2025)"
    assert q.count("\n- ") == 4                                         # every step, not one
    assert q.endswith("[Source: SOP-CONFINED-03, lines 3-6]")


# ---------------------------------------------------------- MySQL FULLTEXT ---

def test_fulltext_ignores_the_same_words_as_search_sops():
    """MySQL's own stopword list has no 'work', so MATCH found SOP-LOTO-01 ('After work:')
    for a welding question that search_sops rightly answers with nothing (D3)."""
    words = set(sp.fulltext_stopwords())
    assert sp._STOP <= words
    assert sp.MYSQL_DEFAULT_STOPWORDS <= words      # a user list replaces MySQL's defaults
    assert {"work", "working", "machine"} <= words
    assert max(len(w) for w in words) <= 30          # fits sop_ft_stopwords.value


class RecordingConn:
    """Stands in for a SQLAlchemy connection: records each statement and its parameters."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))
        return self

    def scalar(self):
        return "jme"


def test_fulltext_indexes_are_rebuilt_after_the_stopword_list_is_set():
    """MySQL reads the stopword list only when an index is built, so the SET must come
    first, on the same connection, and both indexes must be rebuilt."""
    conn = RecordingConn()
    sp.rebuild_fulltext(conn)
    sql = [s for s, _ in conn.calls]
    set_at = next(i for i, s in enumerate(sql) if "innodb_ft_user_stopword_table" in s)
    assert conn.calls[set_at][1] == {"t": "jme/sop_ft_stopwords"}
    adds = [s for s in sql[set_at:] if "ADD FULLTEXT" in s]
    assert adds == ["ALTER TABLE sops ADD FULLTEXT INDEX ft_sops (title, full_text)",
                    "ALTER TABLE sop_steps ADD FULLTEXT INDEX ft_sop_steps (step_text)"]
    assert not any("ADD FULLTEXT" in s for s in sql[:set_at])
    inserted = next(p for s, p in conn.calls if s.startswith("INSERT INTO sop_ft_stopwords"))
    assert [r["value"] for r in inserted] == sp.fulltext_stopwords()


def test_stopword_table_has_a_primary_key():
    """Hosted MySQL sets sql_require_primary_key: a table without one fails (error 3750)."""
    conn = RecordingConn()
    sp.rebuild_fulltext(conn)
    create = next(s for s, _ in conn.calls if s.startswith("CREATE TABLE"))
    assert "value VARCHAR(30) NOT NULL PRIMARY KEY" in create
    assert len(set(sp.fulltext_stopwords())) == len(sp.fulltext_stopwords())   # no duplicates


# ------------------------------------------------------------ whole folder ---

def test_client_folder_end_to_end():
    out = sp.process(client_folder(), run_id=12, machines=MACHINES)
    stats = sp.summary(out)
    assert (stats["rows_read"], stats["loaded"], stats["rejected"]) == (4, 3, 1)
    assert stats["steps"] == 18
    assert list(out["raw"]["raw_id"]) == [12001, 12002, 12003, 12004]
    assert set(out["sops"]["raw_id"]) == {12002, 12003, 12004}
    assert doc(out, "SOP-CRANE-02")["rev_month"] == "2026-03"
    assert out["sops"]["is_current"].tolist() == [1, 1, 1]


def test_dry_run_prints_a_summary_the_pipeline_can_read(tmp_path, monkeypatch, capsys):
    for sf in client_folder():
        (tmp_path / sf.source_file).write_bytes(sf.data)
    monkeypatch.setattr("sys.argv", ["ingest_sops.py", str(tmp_path), "--dry-run"])
    sp.main()
    out = capsys.readouterr().out
    assert "Rows read: 4   loaded: 3   rejected: 1" in out
    assert "Rejected files: not_an_sop=1 (total 1)" in out
    assert "@" not in out                    # the email's addresses are never printed


SOPS = ROOT / "data" / "sops"


@pytest.mark.skipif(not (SOPS / "SOP-LOTO-01.txt").is_file(), reason="needs data/sops/")
def test_the_real_sop_folder():
    out = sp.process(sp.read_folder(SOPS))
    assert sorted(out["sops"]["doc_no"]) == ["SOP-CONFINED-03", "SOP-CRANE-02", "SOP-LOTO-01"]
    assert out["sops"].set_index("doc_no").loc["SOP-LOTO-01", "title"] == "LOCKOUT/TAGOUT PROCEDURE"
    assert len(out["steps"]) == 18