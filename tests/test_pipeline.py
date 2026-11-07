"""Tests for scripts/pipeline.py. No database or ingest data needed: a fake runner
stands in for the scripts, with output taken from their documented run summaries."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

import pipeline as pl
from lineage import folder_sha256
from pipeline import Outcome, Planned, Result

INPUTS = dict(pl.DEFAULT_INPUTS)
SGT = timezone(timedelta(hours=8))
DEFAULT_KEYS = ["logs", "spares", "cmms", "images", "sops", "relationships"]

# What ingest_cmms.py --dry-run prints, trimmed: sample rows (with a name), then the summary.
CMMS_DRY = """
employees (first 5 clean rows):
 raw_id  emp_id emp_code               emp_name        trade shift
      1       1   EMP001           Seah Jia Hui qa inspector night
Rejected rows (first 10):
    table  source_line  tuple_no                         raw_tuple reject_reason
employees           40         2 (2, 'EMP001', 'Foo Wei Ming', ...) duplicate_key

Rows read: 17   loaded: 15   rejected: 2
Statements in dump: create_table=1, drop=1, insert=5, lock=1, unlock=1 (total 9)

employees: read 4, loaded 4, rejected 0
  Loaded with gaps: duplicate_emp_code=2, missing_emp_code=1 (total 3)
  trade: fitter=1, machinist=2, welder=1 (total 4)
safety_incidents: read 5, loaded 5, rejected 0
  severity_scale: 1_worst=1, 5_worst=1, ambiguous=1, unknown=2 (total 5)

Severity: before 2025-07-01 the scale was 1 = worst (converted); from then 5 = worst.
          Incidents 2025-07-01 to 2025-07-31 left unconverted (switch-over date unconfirmed).
Dry run - nothing written to the database.
""".splitlines()

# What ingest_images.py --dry-run prints, trimmed.
IMAGES_DRY = """
Reading 2 image(s) in data/images with tesseract
  [1/2] IR-2024-900.jpg: incident_report
incident_reports (first 5 clean rows):
 raw_id  incident_id incident_date machine_id emp_code
      1          900    2024-07-01     LTH-01   EMP001
Rejected images:
source_file doc_type reject_reason duplicate_of_raw_id
  blank.jpg  unknown unknown_document_type  None

Rows read: 3   loaded: 2   rejected: 1
Document types: incident_report=1, nameplate=1, unknown=1 (total 3)
OCR engine: both:tesseract-v3+llm-v3:gpt-4o-mini=3 (total 3)
Rejected rows: unknown_document_type=1 (total 1)
Photos for the app: original=1, redacted=1 (total 2)
Forms with no photo (redaction not possible): labels_not_found=1 (total 1)

nameplates: loaded 1
  Loaded with warnings: placeholder_manufacturer=1 (total 1)

incident_reports: loaded 1
  Loaded with warnings: root_cause_in_attachment=1 (total 1)
  Severity scale on forms: 1_worst=1 (total 1)

Machine ids: not checked here (cmms_machines not loaded, or a dry run); relationships.py checks them after the loads

Severity: taken from the scale printed on each form, not from its date; forms that disagree with the date rule are flagged form_scale_contradicts_date_rule.
Dry run - nothing written to the database.
""".splitlines()

# What ingest_sops.py --dry-run prints on the client's folder, trimmed: the step table,
# the rejected file's name, then the summary.
SOPS_DRY = """
         doc_no  step_no  line_no marker                                                     step_text
SOP-CONFINED-03        1        3      -                Entry permit required from WSH officer. Valid 8 hours max.
    SOP-LOTO-01        1        2     1. Identify all energy sources (electrical, hydraulic, pneumatic, gravity).

Rejected files:
            source_file reject_reason
0000_gm_brief_email.txt    not_an_sop

Rows read: 4   loaded: 3   rejected: 1
One row is one file.
  SOP-CONFINED-03  rev 2, Sep 2025     4 steps  current  machines: BLAST-01, PMP-T01
  SOP-CRANE-02     rev 6, Mar 2026     6 steps  current  machines: CRN-01, CRN-02
  SOP-LOTO-01      rev 4, Jan 2026     8 steps  current  machines: CNC-01, CNC-02, CNC-03
Rejected files: not_an_sop=1 (total 1)
Loaded with warnings: refers_to_document_not_in_data=3 (total 3)
Machine links by kind: alias=4, id=3 (total 7)
Steps referring to documents not in the data: certified operator list (HR)=1, entry permit=1, lifting plan (signed by PE)=1, machine manual=1 (total 4)
Machine names: not matched (CMMS not loaded); ids and aliases only
Dry run - nothing written to the database.
""".splitlines()

SPARES_REAL = """
Rejected rows:
 source_row part_no      description reject_reason
          9 BRG-6310 Bearing 6310 C3 duplicate_part_no
Recorded as run 12 in ingest_runs.

Rev 11: rows_read=10, parts_loaded=10, rejected=0, machine_links=13, below_min_qty=3
Done.
""".splitlines()

RELATIONSHIPS_REAL = """
Relationships (child -> parent)
  wo_machine         cmms_work_orders.machine_id -> cmms_machines.machine_id  [soft]
      rows=1603  matched=1603  null=0  orphans=0
  reading_machine    vibration_temp.machine_id -> cmms_machines.machine_id  [soft]
      rows=6  matched=4  null=0  orphans=2
      orphan keys: CNC2 (2)

Views:
  machine_spares             created
  work_order_sensor_window   skipped: load vibration_temp first
Recorded as run 15 in ingest_runs.
Done. 2 orphan row(s) in total - see orphan_values in table_relationships.
""".splitlines()

# What audit_sensor_csv.py prints on the client's file, trimmed.
AUDIT_OUT = """File: data/logs/vibration_temp_2026H1.csv  bytes=4598467  CRLF lines=106385  BOM=False
Rows: 106384  Columns: ['timestamp', 'machine_id', 'vib_rms_mms', 'temp', 'temp_unit']

[Timestamps] formats: {'iso_offset': 79596, 'epoch_ms': 26788}
  range (SGT): 2026-01-01 00:00:00+08:00 -> 2026-06-30 23:48:00+08:00

[Machine ids] as written: {'CNC-01': 21268, 'CNC-02': 21291}
  not in CMMS after trim/upper: []

[Duplicates] same machine+instant: 0 rows in 0 groups
  exact duplicate lines: 0

[Sampling / gaps] per machine (minutes):
  CMP-01: n=21276 typical=12 min  irregular=436  gaps>36min=0  total_gap_h=0.0
""".splitlines()


def fake_runner(outcomes: dict[str, Outcome]):
    """Runner keyed by script name; records the commands it was given."""
    calls = []

    def run(cmd):
        calls.append(cmd)
        script = cmd[1].rsplit("/", 1)[-1]
        return outcomes.get(script, Outcome(0, ["Done."], [], 0.1))
    run.calls = calls
    return run


# --------------------------------------------------------------------- plan ---

def test_default_plan_order_and_flags():
    plan = pl.build_plan(inputs=INPUTS, recreate=True, mermaid="docs/er_diagram.md",
                         env_file="/x/.env")
    assert [p.step.key for p in plan] == DEFAULT_KEYS
    assert plan[0].args == [INPUTS["logs"], "--recreate", "--env-file", "/x/.env"]
    rels = plan[-1].args
    assert "--recreate" not in rels and rels[-2:] == ["--mermaid", "docs/er_diagram.md"]
    assert plan[2].display() == ("python scripts/ingest_cmms.py data/sql/jurongmarine_cmms.sql "
                                 "--recreate --env-file /x/.env")
    assert plan[3].display() == ("python scripts/ingest_images.py data/images "
                                 "--recreate --env-file /x/.env")
    assert plan[4].display() == ("python scripts/ingest_sops.py data/sops "
                                 "--recreate --env-file /x/.env")


def test_images_load_after_the_cmms_so_machine_ids_can_be_checked():
    keys = [p.step.key for p in pl.build_plan(inputs=INPUTS)]
    assert keys.index("images") == keys.index("cmms") + 1


def test_sops_load_after_the_cmms_and_before_the_linking():
    """ingest_sops.py matches CMMS machine names (cmms_machines must be loaded), and
    relationships.py must see the new sop_machines rows and rebuild machine_sops."""
    keys = [p.step.key for p in pl.build_plan(inputs=INPUTS)]
    assert keys.index("cmms") < keys.index("sops") < keys.index("relationships")


def test_foreign_keys_are_dropped_first_and_added_last():
    plan = pl.build_plan(inputs=INPUTS, foreign_keys=True)
    assert plan[0].step is pl.DROP_FKS and plan[0].args[0] == "--drop-foreign-keys"
    assert plan[-1].step.key == "relationships" and plan[-1].args[0] == "--foreign-keys"
    only_images = pl.build_plan(inputs=INPUTS, only=["images"], foreign_keys=True)
    assert [p.step.key for p in only_images] == ["drop_fks", "images"]   # a reload too
    only_sops = pl.build_plan(inputs=INPUTS, only=["sops", "relationships"], foreign_keys=True)
    assert [p.step.key for p in only_sops] == ["drop_fks", "sops", "relationships"]


def test_dry_run_never_touches_foreign_keys():
    plan = pl.build_plan(inputs=INPUTS, dry_run=True, foreign_keys=True)
    assert plan[0].step.key == "logs"
    assert all("--dry-run" in p.args for p in plan)
    assert not any("foreign-keys" in a for p in plan for a in p.args)


def test_only_runs_some_steps_in_pipeline_order():
    plan = pl.build_plan(inputs=INPUTS, only=["relationships", "cmms"], foreign_keys=True)
    assert [p.step.key for p in plan] == ["drop_fks", "cmms", "relationships"]
    assert [p.step.key for p in pl.build_plan(inputs=INPUTS, only=["relationships"],
                                              foreign_keys=True)] == ["relationships"]
    with pytest.raises(ValueError):
        pl.parse_only("cmms,nope")


def test_agent_step_modes():
    assert pl.build_plan(inputs=INPUTS, agent="check")[-1].args == ["--check"]
    assert pl.build_plan(inputs=INPUTS, agent="setup")[-1].args == []
    assert pl.build_plan(inputs=INPUTS, agent="setup", dry_run=True)[-1].args == ["--dry-run"]
    assert pl.build_plan(inputs=INPUTS, only=["agent_user"])[0].args == ["--check"]
    assert "agent_user" not in [p.step.key for p in pl.build_plan(inputs=INPUTS)]


def test_paths_relative_to_repo_root(tmp_path):
    assert pl.rel("data/x.csv") == "data/x.csv"
    assert pl.rel(pl.ROOT / "data" / "x.csv") == "data/x.csv"
    assert pl.rel("x.csv", tmp_path) == str((tmp_path / "x.csv").resolve())


def test_image_folder_input_is_hashed_like_the_load(tmp_path):
    """The report's 'Same file as loaded' compares with what ingest_images.py recorded."""
    (tmp_path / "a.jpg").write_bytes(b"one")
    (tmp_path / "b.PNG").write_bytes(b"two")
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")
    info = pl.describe_inputs([Planned(pl.BY_KEY["images"], [str(tmp_path)], str(tmp_path))])
    assert info[0].sha256 == folder_sha256(tmp_path, (".jpg", ".png")) and info[0].size == 6
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pl.describe_inputs([Planned(pl.BY_KEY["images"], [], str(empty))])[0].sha256 is None


def test_sop_folder_input_is_hashed_like_the_load(tmp_path):
    """Only .txt and .md count, as in ingest_sops.py: a stray photo in the folder must not
    make 'Same file as loaded' say NO."""
    (tmp_path / "SOP-LOTO-01.txt").write_bytes(b"JME SOP-LOTO-01")
    (tmp_path / "notes.md").write_bytes(b"md")
    (tmp_path / "photo.jpg").write_bytes(b"not an SOP")
    info = pl.describe_inputs([Planned(pl.BY_KEY["sops"], [str(tmp_path)], str(tmp_path))])
    assert info[0].sha256 == folder_sha256(tmp_path, (".txt", ".md")) and info[0].size == 17
    assert pl.folder_extensions("sops") == {".txt", ".md"}


# ------------------------------------------------------------------ parsing ---

def test_summary_keeps_counts_and_drops_sample_rows():
    """PDPA: dry-run sample rows hold names; only summary lines reach the report."""
    s = pl.summary_lines(CMMS_DRY)
    text = "\n".join(s)
    assert "Seah Jia Hui" not in text and "Foo Wei Ming" not in text
    assert "Rows read: 17   loaded: 15   rejected: 2" in s
    assert "employees: read 4, loaded 4, rejected 0" in s
    assert "trade: fitter=1, machinist=2, welder=1 (total 4)" in s
    assert any(line.startswith("Incidents 2025-07-01") for line in s)
    assert "Rejected rows (first 10):" not in s and "Dry run - nothing written" not in text


def test_images_summary_lines_are_kept_and_sample_rows_dropped():
    s = pl.summary_lines(IMAGES_DRY)
    text = "\n".join(s)
    assert "Document types: incident_report=1, nameplate=1, unknown=1 (total 3)" in s
    assert "Severity scale on forms: 1_worst=1 (total 1)" in s
    assert "nameplates: loaded 1" in s and "incident_reports: loaded 1" in s
    assert any(x.startswith("Machine ids: not checked") for x in s)
    assert "EMP001" not in text and "blank.jpg" not in text and "[1/2]" not in text
    assert pl.parse_counts(IMAGES_DRY) == {"rows_read": 3, "rows_loaded": 2, "rows_rejected": 1}


def test_sop_summary_lines_are_kept_and_the_steps_and_email_dropped():
    """The report gets one line per SOP and the counts. Not the step table (the report
    isn't where an SOP is quoted), and not the rejected email's file name."""
    s = pl.summary_lines(SOPS_DRY)
    text = "\n".join(s)
    assert "Rows read: 4   loaded: 3   rejected: 1" in s
    assert ("SOP-LOTO-01      rev 4, Jan 2026     8 steps  current  machines: CNC-01, CNC-02, "
            "CNC-03") in s
    assert sum(x.startswith("SOP-") for x in s) == 3
    assert "Rejected files: not_an_sop=1 (total 1)" in s
    assert "Machine links by kind: alias=4, id=3 (total 7)" in s
    assert any(x.startswith("Steps referring to documents not in the data:") for x in s)
    assert any(x.startswith("Machine names:") for x in s)
    assert "Entry permit required" not in text and "Identify all energy" not in text
    assert "0000_gm_brief_email" not in text and "Rejected files:" not in s   # bare heading
    assert pl.parse_counts(SOPS_DRY) == {"rows_read": 4, "rows_loaded": 3, "rows_rejected": 1}


def test_sop_real_load_lines_are_kept():
    s = pl.summary_lines(["Recorded as run 21 in ingest_runs.",
                          "FULLTEXT indexes rebuilt with 60 stop words (sop_ft_stopwords).",
                          "Done."])
    assert len(s) == 3


def test_summary_skips_a_bare_heading_but_keeps_its_counts():
    s = pl.summary_lines(SPARES_REAL)
    assert "Rejected rows:" not in s and not any("BRG-6310" in x for x in s)
    assert s[0] == "Recorded as run 12 in ingest_runs."


def test_relationship_check_and_view_lines_are_kept():
    s = pl.summary_lines(RELATIONSHIPS_REAL)
    assert "orphan keys: CNC2 (2)" in s
    assert "work_order_sensor_window   skipped: load vibration_temp first" in s
    assert any(x.startswith("rows=1603  matched=1603") for x in s)


def test_agent_check_lines_are_kept():
    s = pl.summary_lines(["  ok      machine_spares", "  FAIL    employee names: the app user "
                          "CAN do this", "  skip    vibration_temp_audit: not created yet",
                          "Passed: the app user reads only the allowed views"])
    assert len(s) == 4


def test_counts_for_each_script_format():
    assert pl.parse_counts(CMMS_DRY) == {"rows_read": 17, "rows_loaded": 15, "rows_rejected": 2}
    assert pl.parse_counts(SPARES_REAL) == {"rows_read": 10, "rows_loaded": 10,
                                            "rows_rejected": 0}
    assert pl.parse_counts(["Done."]) is None


def test_run_id_also_found_in_error_output():
    assert pl.parse_run_id(SPARES_REAL) == 12
    assert pl.parse_run_id(["Load failed - nothing changed. Recorded as run 9 in ingest_runs."]) == 9
    assert pl.parse_run_id(["Done."]) is None


# ------------------------------------------------------------------ running ---

def test_a_failure_stops_the_rest():
    run = fake_runner({"ingest_spares.py": Outcome(1, [], ["Traceback ...",
                                                           "FileNotFoundError: x.xlsx"], 0.2)})
    results = pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, echo=lambda *a, **k: None)
    assert [r.status for r in results] == ["ok", "failed"] + ["not run"] * 4
    assert results[1].error_tail[-1] == "FileNotFoundError: x.xlsx"
    assert len(run.calls) == 2                       # relationships never linked half a load


def test_a_failed_sop_load_stops_before_the_linking():
    run = fake_runner({"ingest_sops.py": Outcome(1, [], ["sops table is from an older version"],
                                                 0.2)})
    results = pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, echo=lambda *a, **k: None)
    assert [r.status for r in results][-2:] == ["failed", "not run"]


def test_keep_going_runs_everything():
    run = fake_runner({"ingest_spares.py": Outcome(1, [], ["boom"], 0.2)})
    results = pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, keep_going=True,
                              echo=lambda *a, **k: None)
    assert [r.status for r in results] == ["ok", "failed"] + ["ok"] * 4


def test_interrupt_stops_even_with_keep_going():
    run = fake_runner({"ingest_logs.py": Outcome(None, [], [], 3.0, interrupted=True)})
    results = pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, keep_going=True,
                              echo=lambda *a, **k: None)
    assert [r.status for r in results] == ["interrupted"] + ["not run"] * 5


def test_run_command_streams_and_keeps_both_outputs():
    code = "import sys; print('Rows read: 3   loaded: 3   rejected: 0'); " \
           "print('oops', file=sys.stderr); sys.exit(2)"
    o = pl.run_command([sys.executable, "-c", code], echo=False)
    assert o.exit_code == 2 and o.stderr == ["oops"]
    assert pl.parse_counts(o.stdout)["rows_read"] == 3


# ------------------------------------------------------------------- report ---

def _meta(**kw):
    t = datetime(2026, 11, 3, 7, 0, tzinfo=SGT)
    return {"started": t, "finished": t + timedelta(seconds=75), "dry_run": False,
            "options": ["--recreate"], "database": "ingest@localhost:3307/jurong (password hidden)",
            "git_commit": "abc1234", "git_dirty": False, **kw}


def _results():
    plan = pl.build_plan(inputs=INPUTS)
    return [Result(plan[0], "ok", 0, 12.3, ["Rows read: 9   loaded: 5   rejected: 4"],
                   {"rows_read": 9, "rows_loaded": 5, "rows_rejected": 4}, 11),
            Result(plan[1], "failed", 1, 0.4, [], None, 12, ["FileNotFoundError: x.xlsx"]),
            *[Result(p, "not run") for p in plan[2:]]]


def test_report_tables():
    inputs = [pl.InputFile("logs", INPUTS["logs"], 2048, "a" * 64),
              pl.InputFile("spares", INPUTS["spares"], 1024, "b" * 64),
              pl.InputFile("cmms", INPUTS["cmms"])]
    runs = {11: {"file_sha256": "a" * 64, "rows_read": 106000, "rows_loaded": 105990,
                 "rows_rejected": 10},
            12: {"file_sha256": "c" * 64, "rows_read": None, "rows_loaded": None,
                 "rows_rejected": None}}
    md = pl.render_report(_meta(), _results(), inputs, runs)
    assert "| Result | **FAILED** at step 2, Critical spares |" in md
    assert "| 1 | Sensor logs | `ingest_logs.py` | ok | 12.3s | 11 | 106,000 | 105,990 | 10 |" in md
    assert "| 3 | CMMS dump | `ingest_cmms.py` | not run | – |" in md
    assert "| 4 | Scanned images | `ingest_images.py` | not run | – |" in md
    assert "| 5 | SOP documents | `ingest_sops.py` | not run | – |" in md
    assert f"| logs | `{INPUTS['logs']}` | 2.0 KB | `{'a' * 64}` | yes |" in md
    assert "| spares |" in md and "| **NO** |" in md             # file changed since the load
    assert "**file not found**" in md
    assert "FileNotFoundError: x.xlsx" in md and "1m 15s" in md


def test_report_warns_about_uncommitted_code():
    md = pl.render_report(_meta(git_dirty=True), _results()[:1], [])
    assert "**with uncommitted changes**" in md
    assert "| Result | **Succeeded** |" in md


def test_report_dry_run_and_links():
    links = [{"name": "reading_machine", "child_table": "vibration_temp",
              "child_column": "machine_id", "parent_table": "cmms_machines",
              "parent_column": "machine_id", "enforced": "soft", "child_rows": 6,
              "matched_rows": 4, "orphan_rows": 2, "orphan_values": "CNC2 (2) | x"}]
    md = pl.render_report(_meta(dry_run=True), _results()[:1],
                          [pl.InputFile("logs", INPUTS["logs"], 10, "a" * 64)], links=links)
    assert "dry run: nothing written" in md and "n/a (dry run)" in md
    assert "| reading_machine | `vibration_temp.machine_id` -> `cmms_machines.machine_id` | " \
           "soft | 6 | 4 | 2 | CNC2 (2) \\| x |" in md                     # pipes escaped
    assert "Couldn't read" in pl.render_report(_meta(), _results()[:1], [], links="OperationalError")


def test_main_writes_the_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    run = fake_runner({"ingest_cmms.py": Outcome(0, CMMS_DRY, [], 1.0)})
    report = tmp_path / "r.md"
    code = pl.main(["--dry-run", "--only", "cmms", "--report", str(report)], runner=run)
    md = report.read_text(encoding="utf-8")
    assert code == 0 and "| Result | **Succeeded** |" in md
    assert "employees: read 4, loaded 4, rejected 0" in md and "Seah Jia Hui" not in md
    assert run.calls[0][2:] == [INPUTS["cmms"], "--dry-run"]
    assert "Report written to" in capsys.readouterr().out


def test_main_passes_an_images_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    (tmp_path / "scans").mkdir()
    (tmp_path / "scans" / "x.jpg").write_bytes(b"img")
    run = fake_runner({"ingest_images.py": Outcome(0, IMAGES_DRY, [], 1.0)})
    report = tmp_path / "r.md"
    assert pl.main(["--dry-run", "--only", "images", "--images", str(tmp_path / "scans"),
                    "--report", str(report)], runner=run) == 0
    assert run.calls[0][2] == str((tmp_path / "scans").resolve())
    md = report.read_text(encoding="utf-8")
    assert "Severity scale on forms: 1_worst=1 (total 1)" in md
    assert folder_sha256(tmp_path / "scans", (".jpg",)) in md


def test_main_passes_a_sop_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    (tmp_path / "sops").mkdir()
    (tmp_path / "sops" / "SOP-LOTO-01.txt").write_bytes(b"JME SOP-LOTO-01")
    run = fake_runner({"ingest_sops.py": Outcome(0, SOPS_DRY, [], 0.4)})
    report = tmp_path / "r.md"
    assert pl.main(["--dry-run", "--only", "sops", "--sops", str(tmp_path / "sops"),
                    "--report", str(report)], runner=run) == 0
    assert run.calls[0][2:] == [str((tmp_path / "sops").resolve()), "--dry-run"]
    md = report.read_text(encoding="utf-8")
    assert "| 1 | SOP documents | `ingest_sops.py` | ok | 0.4s | – | 4 | 3 | 1 |" in md
    assert "Machine links by kind: alias=4, id=3 (total 7)" in md
    assert folder_sha256(tmp_path / "sops", (".txt", ".md")) in md
    assert "0000_gm_brief_email" not in md


# ------------------------------------------------------------------ --save-log ---

def logging_runner(outcomes: dict[str, Outcome]):
    """Fake runner that also writes its output to the log, like run_command does."""
    def run(cmd, log=None):
        o = outcomes.get(cmd[1].rsplit("/", 1)[-1], Outcome(0, ["Done."], [], 0.1))
        if log is not None:
            for line in o.stdout:
                log.write(line + "\n")
            for line in o.stderr:
                log.write(pl.STDERR_PREFIX + line + "\n")
        return o
    return run


def test_run_command_logs_both_streams_as_they_arrive(tmp_path):
    code = "import sys; print('out 1'); print('err 1', file=sys.stderr)"
    path = tmp_path / "x.log"
    with open(path, "w", encoding="utf-8") as log:
        pl.run_command([sys.executable, "-c", code], echo=False, log=log)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "out 1" in lines and "[stderr] err 1" in lines


def test_run_pipeline_logs_each_step_and_its_outcome(tmp_path):
    path = tmp_path / "x.log"
    run = fake_runner({"ingest_spares.py": Outcome(1, [], ["boom"], 0.2)})
    with open(path, "w", encoding="utf-8") as log:
        pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, log=log, echo=lambda *a, **k: None)
    text = path.read_text(encoding="utf-8")
    assert "=== [1/6] Sensor logs: python scripts/ingest_logs.py" in text
    assert "--- failed, exit code 1, 0.2s" in text
    assert "=== [5/6] SOP documents" in text
    assert "=== [6/6] Links between sources" in text and "--- not run" in text


def test_save_log_keeps_full_output_out_of_git_and_out_of_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    report = tmp_path / "pipeline_x.md"
    run = logging_runner({"ingest_cmms.py": Outcome(0, CMMS_DRY, ["a warning"], 1.0)})
    assert pl.main(["--dry-run", "--only", "cmms", "--report", str(report), "--save-log"],
                   runner=run) == 0
    log = tmp_path / "logs" / "pipeline_x.log"
    text = log.read_text(encoding="utf-8")
    assert "Seah Jia Hui" in text and "[stderr] a warning" in text   # really everything
    assert text.startswith("# Pipeline log") and "# Finished" in text
    assert (tmp_path / "logs" / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"
    md = report.read_text(encoding="utf-8")
    assert "[`" in md and "](logs/pipeline_x.log)" in md and "Seah Jia Hui" not in md
    if sys.platform != "win32":
        assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_no_log_without_the_option(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    report = tmp_path / "r.md"
    pl.main(["--dry-run", "--only", "cmms", "--report", str(report)], runner=fake_runner({}))
    assert not (tmp_path / "logs").exists()
    assert "`--save-log` keeps it" in report.read_text(encoding="utf-8")


def test_main_returns_1_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: None)
    run = fake_runner({"relationships.py": Outcome(1, [], ["boom"], 0.1)})
    assert pl.main(["--dry-run", "--only", "relationships", "--report",
                    str(tmp_path / "r.md")], runner=run) == 1


# ------------------------------------------------------------------ --audit ---

def test_audit_runs_first_on_the_sensor_file_and_writes_nothing():
    plan = pl.build_plan(inputs=INPUTS, audit=True, foreign_keys=True, env_file="/x/.env")
    assert [p.step.key for p in plan][:3] == ["audit", "drop_fks", "logs"]   # before any write
    assert plan[0].args == [INPUTS["logs"]]                    # read-only: no --env-file
    dry = pl.build_plan(inputs=INPUTS, audit=True, dry_run=True)
    assert dry[0].args == [INPUTS["logs"]]                     # the script has no --dry-run
    assert dry[0].display() == f"python scripts/audit_sensor_csv.py {INPUTS['logs']}"


def test_audit_is_off_by_default_and_can_be_picked_with_only():
    assert "audit" not in [p.step.key for p in pl.build_plan(inputs=INPUTS)]
    assert [p.step.key for p in pl.build_plan(inputs=INPUTS, only=["audit"])] == ["audit"]
    assert [p.step.key for p in pl.build_plan(inputs=INPUTS, only=["logs", "audit"])] == \
        ["audit", "logs"]


def test_audit_findings_go_into_the_report_in_full():
    """The sensor file holds no personal data, so all of the audit's findings are kept."""
    run = fake_runner({"audit_sensor_csv.py": Outcome(0, AUDIT_OUT, [], 19.2)})
    r = pl.run_pipeline(pl.build_plan(inputs=INPUTS, only=["audit"]), run,
                        echo=lambda *a, **k: None)[0]
    assert r.status == "ok" and r.counts == {"rows_read": 106384}
    assert "[Duplicates] same machine+instant: 0 rows in 0 groups" in r.summary
    assert "  not in CMMS after trim/upper: []" in r.summary          # indentation kept
    assert "" not in r.summary and len(r.summary) == 10


def test_a_file_read_twice_is_listed_once_under_the_loading_step(tmp_path):
    csv = tmp_path / "x.csv"
    csv.write_text("a\n", encoding="utf-8")
    both = pl.build_plan(inputs={**INPUTS, "logs": str(csv)}, only=["audit", "logs"])
    assert [(f.step, f.path) for f in pl.describe_inputs(both)] == [("logs", str(csv))]
    alone = pl.build_plan(inputs={**INPUTS, "logs": str(csv)}, only=["audit"])
    assert [f.step for f in pl.describe_inputs(alone)] == ["audit"]


def test_main_with_audit_reports_its_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    run = fake_runner({"audit_sensor_csv.py": Outcome(0, AUDIT_OUT, [], 19.2)})
    report = tmp_path / "r.md"
    assert pl.main(["--dry-run", "--audit", "--only", "logs", "--report", str(report)],
                   runner=run) == 0
    assert [c[1].rsplit("/", 1)[-1] for c in run.calls] == ["audit_sensor_csv.py",
                                                             "ingest_logs.py"]
    md = report.read_text(encoding="utf-8")
    assert "| 1 | Sensor CSV audit | `audit_sensor_csv.py` | ok | 19.2s | – | 106,384 | – | – |" \
        in md
    assert "[Sampling / gaps] per machine (minutes):" in md and "--audit" in md



# ----------------------------------------------- images: engine and photos ---

def test_the_image_engine_is_passed_to_the_images_step_only():
    plan = pl.build_plan(inputs=INPUTS, image_engine="both", dry_run=True)
    images = next(p for p in plan if p.step.key == "images")
    assert images.args == [INPUTS["images"], "--engine", "both", "--dry-run"]
    assert all("--engine" not in p.args for p in plan if p.step.key != "images")
    default = next(p for p in pl.build_plan(inputs=INPUTS) if p.step.key == "images")
    assert "--engine" not in default.args          # the script decides from IMAGE_ENGINE


def test_the_photo_lines_reach_the_report():
    s = pl.summary_lines(IMAGES_DRY)
    assert "Photos for the app: original=1, redacted=1 (total 2)" in s
    assert "Forms with no photo (redaction not possible): labels_not_found=1 (total 1)" in s


def test_the_report_header_says_which_engine_read_the_scans(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    run = fake_runner({"ingest_images.py": Outcome(0, IMAGES_DRY, [], 1.0)})
    report = tmp_path / "r.md"
    assert pl.main(["--dry-run", "--only", "images", "--image-engine", "both",
                    "--report", str(report)], runner=run) == 0
    assert run.calls[0][3:5] == ["--engine", "both"]
    md = report.read_text(encoding="utf-8")
    assert "| Image engine | both:tesseract-v3+llm-v3:gpt-4o-mini=3 (total 3) |" in md
    assert "--image-engine both" in md                                # in Options
    assert "Photos for the app: original=1, redacted=1 (total 2)" in md


def test_no_engine_row_without_an_images_step(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "git_dirty", lambda: False)
    report = tmp_path / "r.md"
    pl.main(["--dry-run", "--only", "cmms", "--report", str(report)], runner=fake_runner({}))
    assert "Image engine" not in report.read_text(encoding="utf-8")