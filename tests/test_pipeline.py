"""Tests for scripts/pipeline.py. No database or ingest data needed: a fake runner
stands in for the scripts, with output taken from their documented run summaries."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

import pipeline as pl
from pipeline import Outcome, Planned, Result

INPUTS = dict(pl.DEFAULT_INPUTS)
SGT = timezone(timedelta(hours=8))

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
    assert [p.step.key for p in plan] == ["logs", "spares", "cmms", "relationships"]
    assert plan[0].args == [INPUTS["logs"], "--recreate", "--env-file", "/x/.env"]
    rels = plan[-1].args
    assert "--recreate" not in rels and rels[-2:] == ["--mermaid", "docs/er_diagram.md"]
    assert plan[2].display() == ("python scripts/ingest_cmms.py data/sql/jurongmarine_cmms.sql "
                                 "--recreate --env-file /x/.env")


def test_foreign_keys_are_dropped_first_and_added_last():
    plan = pl.build_plan(inputs=INPUTS, foreign_keys=True)
    assert plan[0].step is pl.DROP_FKS and plan[0].args[0] == "--drop-foreign-keys"
    assert plan[-1].step.key == "relationships" and plan[-1].args[0] == "--foreign-keys"


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
    assert [r.status for r in results] == ["ok", "failed", "not run", "not run"]
    assert results[1].error_tail[-1] == "FileNotFoundError: x.xlsx"
    assert len(run.calls) == 2                       # relationships never linked half a load


def test_keep_going_runs_everything():
    run = fake_runner({"ingest_spares.py": Outcome(1, [], ["boom"], 0.2)})
    results = pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, keep_going=True,
                              echo=lambda *a, **k: None)
    assert [r.status for r in results] == ["ok", "failed", "ok", "ok"]


def test_interrupt_stops_even_with_keep_going():
    run = fake_runner({"ingest_logs.py": Outcome(None, [], [], 3.0, interrupted=True)})
    results = pl.run_pipeline(pl.build_plan(inputs=INPUTS), run, keep_going=True,
                              echo=lambda *a, **k: None)
    assert [r.status for r in results] == ["interrupted", "not run", "not run", "not run"]


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
            Result(plan[2], "not run"), Result(plan[3], "not run")]


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
    assert "=== [1/4] Sensor logs: python scripts/ingest_logs.py" in text
    assert "--- failed, exit code 1, 0.2s" in text
    assert "=== [4/4] Links between sources" in text and "--- not run" in text


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