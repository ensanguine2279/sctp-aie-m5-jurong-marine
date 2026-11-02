"""Tests for scripts/relationships.py. No database needed."""
from __future__ import annotations

import re
from datetime import date

import pytest

import relationships as rel
from relationships import (ENTITIES, RELATIONSHIPS, VIEWS, Check, Names, check_relationship,
                           enforced_names, fingerprint, mermaid_doc, plan_foreign_key,
                           render_view, run_totals, to_mermaid)

BY_NAME = {r.name: r for r in RELATIONSHIPS}


# ------------------------------------------------------------------- checks ---

def test_check_counts_matched_null_and_orphans():
    child = {"CNC-01": 10, "CNC-02": 5, "CNC-09": 3, None: 2, "": 1}
    parent = {"CNC-01": 1, "CNC-02": 1, "MILL-01": 1}
    c = check_relationship(child, parent)
    assert (c.rows, c.matched, c.null_rows) == (21, 15, 3)
    assert c.orphans == {"CNC-09": 3}
    assert c.orphan_rows == 3
    assert c.ambiguous == []


def test_wildcard_all_matches_every_machine_not_an_orphan():
    """Spares sheet: 'ALL' means used on every machine."""
    c = check_relationship({"ALL": 1, "CNC-01": 2, "XYZ-01": 1}, {"CNC-01": 1}, wildcard="ALL")
    assert c.wildcard_rows == 1
    assert c.matched == 3
    assert c.orphans == {"XYZ-01": 1}


def test_keys_compare_like_mysql_collation():
    """MySQL's default collation ignores case and trailing spaces, so the check does too."""
    c = check_relationship({"cnc-01 ": 4}, {"CNC-01": 1})
    assert c.matched == 4 and not c.orphans


def test_duplicate_parent_keys_reported_as_ambiguous():
    """Two employees sharing a code: a join would duplicate rows."""
    c = check_relationship({"EMP001": 3}, {"EMP001": 2, "EMP002": 1})
    assert c.ambiguous == ["EMP001"]
    assert c.matched == 3


def test_empty_child_table():
    c = check_relationship({}, {"CNC-01": 1})
    assert (c.rows, c.matched, c.orphan_rows) == (0, 0, 0)


def test_orphan_text_sorted_by_rows_and_truncated():
    c = Check(orphans={"A": 1, "B": 9})
    assert c.orphan_text() == "B (9), A (1)"
    c = Check(orphans={f"MACHINE-{i:03}": 1 for i in range(200)})
    assert len(c.orphan_text(100)) == 100 and c.orphan_text(100).endswith("...")


# ------------------------------------------------------------- foreign keys ---

def test_fk_added_only_when_clean():
    r = BY_NAME["reading_machine"]
    assert plan_foreign_key(r, Check(rows=5, matched=5), enforced=False) == ("add", "")
    action, reason = plan_foreign_key(r, Check(rows=5, matched=4, orphans={"X": 1}), False)
    assert action == "skip" and "orphan" in reason
    action, reason = plan_foreign_key(r, Check(ambiguous=["CNC-01"]), False)
    assert action == "skip" and "not unique" in reason
    assert plan_foreign_key(r, None, False)[0] == "skip"           # table not loaded
    assert plan_foreign_key(r, Check(), enforced=True)[0] == "exists"


def test_soft_relationships_never_get_a_fk():
    for name in ("spare_machine", "wo_employee", "incident_employee"):
        action, reason = plan_foreign_key(BY_NAME[name], Check(rows=1, matched=1), False)
        assert action == "skip" and reason


def test_enforced_detects_fk_made_by_ingest_script():
    fks = [("fk_csm_part", "critical_spare_machines", "part_no", "critical_spares", "part_no"),
           ("fk_rel_wo_machine", "cmms_work_orders", "machine_id", "cmms_machines",
            "machine_id"),
           ("fk_cmms_machines_raw", "cmms_machines", "raw_id", "cmms_machines_raw", "raw_id")]
    assert enforced_names(fks, Names()) == {"spare_part", "wo_machine"}


# ------------------------------------------------------------------- schema ---

def test_every_relationship_uses_known_tables_and_columns():
    for r in RELATIONSHIPS:
        assert r.child_col in {c for _, c, _, _ in ENTITIES[r.child]}, r.name
        assert r.parent_col in {c for _, c, _, _ in ENTITIES[r.parent]}, r.name
        assert r.parent_end in ("||", "|o")
    assert len({r.name for r in RELATIONSHIPS}) == len(RELATIONSHIPS)


def test_names_follow_ingest_prefix_and_reject_bad_identifiers():
    n = Names("acme_", "sensors_2026")
    assert n["machines"] == "acme_machines" and n["readings"] == "sensors_2026"
    with pytest.raises(ValueError):
        Names("x; DROP TABLE y; --", "vibration_temp").mapping()


# -------------------------------------------------------------------- views ---

@pytest.mark.parametrize("view", list(VIEWS))
def test_views_use_the_real_table_names(view):
    sql = render_view(view, Names("acme_", "sensors"), 7)
    assert f"CREATE OR REPLACE VIEW {view} AS" in sql
    assert "cmms_" not in sql and "vibration_temp" not in sql
    assert "{" not in sql                                    # every placeholder filled


@pytest.mark.parametrize("view", list(VIEWS))
def test_no_view_exposes_employee_names(view):
    """PDPA: emp_name stays in cmms_employees; views give trade/shift only."""
    assert "emp_name" not in render_view(view, Names(), 7)


def test_incident_view_only_offers_the_converted_severity():
    sql = render_view("incident_detail", Names(), 7)
    assert "severity_5_worst" in sql and "severity_as_logged" not in sql


def test_employee_join_cannot_duplicate_rows():
    sql = render_view("work_order_detail", Names(), 7)
    assert "HAVING COUNT(*) = 1" in sql
    assert "LEFT JOIN" in sql                                   # orphans kept, not dropped


def test_sensor_window_days():
    assert "INTERVAL 14 DAY" in render_view("work_order_sensor_window", Names(), 14)
    for bad in (0, 366, "7; DROP TABLE x"):
        with pytest.raises(ValueError):
            render_view("work_order_sensor_window", Names(), bad)


# --------------------------------------------------------------- ER diagram ---

def test_mermaid_solid_for_fk_dotted_for_soft():
    m = to_mermaid(Names(), enforced={"spare_part"})
    assert 'critical_spares ||--o{ critical_spare_machines : "is listed for"' in m
    assert 'cmms_machines ||..o{ vibration_temp : "has sensor readings"' in m
    assert 'cmms_employees |o..o{ cmms_work_orders : "did work orders"' in m


def test_mermaid_lists_every_entity_once():
    m = to_mermaid(Names(), enforced=set())
    blocks = re.findall(r"^    (\w+) \{$", m, flags=re.M)
    assert sorted(blocks) == sorted(Names()[t] for t in ENTITIES)


def test_mermaid_doc_is_markdown_with_a_mermaid_block():
    doc = mermaid_doc(Names(), {"spare_part"}, today=date(2026, 11, 2))
    assert doc.startswith("# ER diagram") and "2026-11-02" in doc
    assert doc.count("```mermaid") == 1 and doc.count("```") == 2
    assert "raw_id" in doc                                      # lineage explained


# ------------------------------------------------------------------ run log ---

def _checks(orphans=None):
    return {"wo_machine": Check(rows=10, matched=9, null_rows=1),
            "reading_machine": Check(rows=5, matched=3, orphans=orphans or {"CNC2": 2}),
            "spare_machine": None}                                  # table not loaded


def test_fingerprint_is_a_stable_sha256():
    h = fingerprint(Names(), _checks())
    assert len(h) == 64 and int(h, 16) >= 0
    assert h == fingerprint(Names(), dict(reversed(list(_checks().items()))))  # order-free


def test_fingerprint_changes_when_the_data_does():
    base = fingerprint(Names(), _checks())
    assert fingerprint(Names(), _checks({"CNC2": 3})) != base             # another orphan
    assert fingerprint(Names("acme_"), _checks()) != base                 # other tables


def test_run_totals_skip_unloaded_tables():
    assert run_totals(_checks()) == {"rows_read": 15, "rows_loaded": 12, "rows_rejected": 2}


class _FakeDb:
    """Stands in for the database in main(): records what lineage was told."""

    def __init__(self, monkeypatch, fail=False):
        self.finished = []
        self.catalog_run_id = None
        cfg = type("Cfg", (), {"engine": lambda s: self, "describe": lambda s: "fake"})()
        monkeypatch.setattr(rel, "load_db_config", lambda env_file=None: cfg)
        monkeypatch.setattr(rel, "existing_tables", lambda conn: set())
        monkeypatch.setattr(rel, "run_checks", lambda conn, n, t: _checks())
        monkeypatch.setattr(rel, "existing_foreign_keys", lambda conn: [])
        monkeypatch.setattr(rel, "create_views", lambda *a: [])
        monkeypatch.setattr(rel, "start_run", self.start_run)
        monkeypatch.setattr(rel, "finish_run", lambda e, run_id, **kw: self.finished.append(kw))

        def write_catalog(engine, names, checks, enforced, run_id=None):
            if fail:
                raise RuntimeError("disk full")
            self.catalog_run_id = run_id
        monkeypatch.setattr(rel, "write_catalog", write_catalog)

    def start_run(self, engine, script, source, sha256=None):
        self.started = (script, source, sha256)
        return 7

    def connect(self):
        class Ctx:
            def __enter__(self):
                return None

            def __exit__(self, *a):
                return False
        return Ctx()


def test_real_run_is_recorded_in_ingest_runs(monkeypatch, capsys):
    db = _FakeDb(monkeypatch)
    monkeypatch.setattr("sys.argv", ["relationships.py"])
    rel.main()
    script, source, sha = db.started
    assert (script, source) == ("relationships.py", "(database)") and len(sha) == 64
    assert db.catalog_run_id == 7                                  # catalog points at the run
    assert db.finished == [{"ok": True, "rows_read": 15, "rows_loaded": 12,
                            "rows_rejected": 2}]
    assert "Recorded as run 7" in capsys.readouterr().out


def test_failed_run_is_recorded_with_its_error(monkeypatch):
    db = _FakeDb(monkeypatch, fail=True)
    monkeypatch.setattr("sys.argv", ["relationships.py"])
    with pytest.raises(RuntimeError):
        rel.main()
    assert db.finished[0]["ok"] is False
    assert db.finished[0]["error"] == "RuntimeError: disk full"


def test_dry_run_needs_no_database(tmp_path, monkeypatch, capsys):
    out = tmp_path / "docs" / "er.md"
    monkeypatch.setattr("sys.argv", ["relationships.py", "--dry-run", "--mermaid", str(out)])
    rel.main()
    assert out.read_text(encoding="utf-8").startswith("# ER diagram")
    assert "Dry run" in capsys.readouterr().out