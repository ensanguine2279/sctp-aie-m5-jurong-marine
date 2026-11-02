"""Tests for scripts/agent_user.py (the app's read-only user). No database needed."""
from __future__ import annotations

import pytest

import agent_user as au
from relationships import Names


def _existing(**extra):
    """information_schema.COLUMNS as {object: columns}, for a fully loaded database."""
    objs = {name: {"machine_id", "raw_id"} for name in au.allowed_objects(Names())}
    objs["cmms_employees"] = {"emp_id", "emp_code", "emp_name"}
    objs["cmms_employees_audit"] = {"emp_id", "emp_code", "emp_name", "raw_id"}
    objs.update(extra)
    return objs


def test_allow_list_never_includes_employees_or_raw_tables():
    for prefix in ("cmms_", "acme_"):
        objs = au.allowed_objects(Names(prefix, "sensors"))
        assert not any("employees" in o for o in objs)
        assert not any(o.endswith("_raw") for o in objs)
        assert "work_order_detail" in objs and "table_relationships" in objs


def test_grants_follow_the_ingest_prefixes():
    objs = au.allowed_objects(Names("acme_", "sensors"))
    assert "acme_work_orders_audit" in objs and "sensors_audit" in objs


def test_everything_allowed_and_existing_is_granted():
    p = au.plan_grants(Names(), _existing())
    assert p.grant == au.allowed_objects(Names())
    assert not p.missing and not p.refused


def test_objects_not_created_yet_are_skipped():
    existing = _existing()
    del existing["work_order_sensor_window"]
    p = au.plan_grants(Names(), existing)
    assert p.missing == ["work_order_sensor_window"]
    assert "work_order_sensor_window" not in p.grant


def test_object_with_a_name_column_is_refused():
    """A view edited later to include emp_name must not be granted."""
    p = au.plan_grants(Names(), _existing(work_order_detail={"wo_id", "EMP_NAME"}))
    assert p.refused == {"work_order_detail": ["emp_name"]}
    assert "work_order_detail" not in p.grant


def test_statements_reset_then_grant_select_only():
    sql = au.statements("jurong", "agent_ro", "%", ["machine_overview"])
    assert sql[0] == "CREATE USER IF NOT EXISTS 'agent_ro'@'%' IDENTIFIED BY :pw"
    assert sql[2] == "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'agent_ro'@'%'"
    assert sql[3:] == ["GRANT SELECT ON `jurong`.`machine_overview` TO 'agent_ro'@'%'"]
    assert all("GRANT" not in s or "GRANT SELECT" in s or "REVOKE" in s for s in sql)


def test_password_is_bound_never_written_into_sql():
    assert all("s3cr3t" not in s for s in au.statements("jurong", "agent_ro", "%", []))


@pytest.mark.parametrize("db,user,host", [
    ("jurong; DROP DATABASE x", "agent_ro", "%"),
    ("jurong", "agent'@'%' --", "%"),
    ("jurong", "agent_ro", "x' OR '1"),
])
def test_bad_identifiers_are_rejected(db, user, host):
    with pytest.raises(ValueError):
        au.statements(db, user, host, [])


def test_error_codes():
    assert au.classify(1142) == "denied"      # SELECT/UPDATE/CREATE command denied
    assert au.classify(1143) == "denied"      # column denied
    assert au.classify(1146) == "missing"     # table doesn't exist
    assert au.classify(2013) == "error"       # lost connection: not a pass
    assert au.classify(None) == "error"


def test_probes_cover_names_raw_writes_and_schema():
    probes = dict(au.forbidden_probes(Names()))
    assert "emp_name" in probes["employee names"]
    assert "cmms_employees_raw" in probes["raw employee rows"]
    assert "WHERE 1 = 0" in probes["writing data"]          # harmless even if allowed


def test_engine_never_puts_parameters_in_errors():
    """The 1227 error once printed AGENT_DB_PASSWORD in the traceback."""
    pytest.importorskip("sqlalchemy")
    from db_config import DbConfig

    engine = au.make_engine(DbConfig(user="u", password="p", host="h", port=3306, name="d"))
    assert engine.hide_parameters is True


def test_admin_errors_are_readable_and_hold_no_secrets():
    assert "DB_ADMIN_USERNAME=root" in au.admin_error(1227, "ingest")   # no CREATE USER
    assert "Login refused for root" in au.admin_error(1045, "root")
    assert "2013" in au.admin_error(2013, "root")
    for code in (1227, 1045, 2013):
        assert ":pw" not in au.admin_error(code, "root")


def test_dry_run_needs_no_database_and_hides_the_password(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DB_PASSWORD", "s3cr3t")
    monkeypatch.setenv("DB_NAME", "jurong")
    monkeypatch.setattr("sys.argv", ["agent_user.py", "--dry-run"])
    au.main()
    out = capsys.readouterr().out
    assert "s3cr3t" not in out and "IDENTIFIED BY ****" in out
    assert "GRANT SELECT ON `jurong`.`incident_detail`" in out
    assert "cmms_employees" not in out.split("Will then check")[0]