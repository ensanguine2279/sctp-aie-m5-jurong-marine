"""Tests for ingest/agent_user.py (the app's read-only user). No database needed."""
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
    objs = au.allowed_objects(Names("acme_", "sensors", "scan_"))
    assert "acme_work_orders_audit" in objs and "sensors_audit" in objs
    assert "scan_nameplates_audit" in objs and "scan_incident_reports_audit" in objs


def test_scanned_images_are_cited_but_raw_ocr_is_never_granted():
    """img_documents_raw keeps a form's 'person involved' exactly as read (maybe a name)."""
    objs = au.allowed_objects(Names())
    assert {"img_nameplates_audit", "img_incident_reports_audit", "machine_nameplate",
            "incident_report_vs_cmms"} <= set(objs)
    assert not any("documents" in o for o in objs)


def test_the_sops_are_granted_but_the_raw_sop_files_never_are():
    """sops for MATCH ... AGAINST (the FULLTEXT index is on the table), the audit views to
    quote an SOP whole with its citation, sop_machines to find SOPs by machine.
    sop_documents_raw keeps the GM's email (names, addresses): never granted."""
    objs = au.allowed_objects(Names())
    assert {"sops", "sops_audit", "sop_steps_audit", "sop_machines"} <= set(objs)
    assert "sop_documents_raw" not in objs
    assert "sop_ft_stopwords" not in objs                 # the index's, not the app's


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
    assert sql[0] == "DROP USER IF EXISTS 'agent_ro'@'%'"        # nothing granted before survives
    assert sql[1] == "CREATE USER 'agent_ro'@'%' IDENTIFIED BY :pw"
    assert sql[2:] == ["GRANT SELECT ON `jurong`.`machine_overview` TO 'agent_ro'@'%'"]
    assert all("GRANT" not in s or s.startswith("GRANT SELECT ON") for s in sql)


def test_no_statement_needs_rights_over_every_database():
    """Aiven keeps avnadmin out of system databases: REVOKE ALL / ON *.* fail there (3879)."""
    for s in au.statements("defaultdb", "agent_ro", "%", ["machine_overview"]):
        assert "REVOKE" not in s and "*.*" not in s and "`mysql`" not in s


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
    assert au.classify(3879) == "denied"      # database denied (hosted MySQL)
    assert au.classify(1146) == "missing"     # table doesn't exist
    assert au.classify(2013) == "error"       # lost connection: not a pass
    assert au.classify(None) == "error"


def test_access_check_reads_no_rows():
    """LIMIT 1 hung on work_order_sensor_window: a grouped view is computed in full first."""
    assert au.access_probe("work_order_sensor_window") == \
        "SELECT * FROM `work_order_sensor_window` LIMIT 0"


def test_a_denied_object_that_exists_fails_and_says_to_grant_it():
    """MySQL says 'denied' (1142) for an object with no grant, whether or not it exists.
    On 4 Nov 2026 the image and SOP objects existed but weren't granted yet."""
    ok, line = au.denied_verdict("sops", missing=set())
    assert not ok and "exists but not granted" in line and "without --check" in line


def test_a_denied_object_not_created_yet_is_skipped():
    ok, line = au.denied_verdict("sops", missing={"sops"})
    assert ok and line == "  skip    sops: not created yet"


def test_a_denied_object_fails_when_nobody_can_tell_whether_it_exists():
    """No admin connection: never pass it, but say both causes are possible."""
    ok, line = au.denied_verdict("sops", missing=None)
    assert not ok and "not granted, or not created yet" in line
    assert "CMMS_DB_ADMIN_PASSWORD" in line


def test_missing_objects_come_from_what_the_admin_sees():
    class Conn:
        def execute(self, stmt, params=None):   # information_schema.COLUMNS rows
            return [(o, "raw_id") for o in au.allowed_objects(Names()) if o != "sop_machines"]

    class Ctx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    class Engine:
        def connect(self):
            return Ctx()

    assert au.missing_objects(Engine(), Names()) == {"sop_machines"}


def test_probes_cover_names_raw_writes_and_schema():
    probes = dict(au.forbidden_probes(Names()))
    assert "emp_name" in probes["employee names"]
    assert "cmms_employees_raw" in probes["raw employee rows"]
    assert "sop_documents_raw" in probes["raw SOP files (the GM's email)"]
    assert "WHERE 1 = 0" in probes["writing data"]          # harmless even if allowed


def test_engine_never_puts_parameters_in_errors():
    """The 1227 error once printed the app user's password in the traceback."""
    pytest.importorskip("sqlalchemy")
    from db_config import DbConfig

    engine = au.make_engine(DbConfig(user="u", password="p", host="h", port=3306, name="d"))
    assert engine.hide_parameters is True


def test_admin_errors_are_readable_and_hold_no_secrets():
    assert "avnadmin on Aiven" in au.admin_error(1227, "ingest")         # no CREATE USER
    assert "Login refused for root" in au.admin_error(1045, "root")
    assert "CMMS_DB_ADMIN_PASSWORD" in au.admin_error(1045, "root")
    assert "Hosted MySQL" in au.admin_error(3879, "avnadmin")
    assert "2013" in au.admin_error(2013, "root")
    for code in (1227, 1045, 3879, 2013):
        assert ":pw" not in au.admin_error(code, "root")


def test_admin_error_names_the_step_and_mysqls_message():
    msg = au.admin_error(3879, "avnadmin", "CREATE USER 'agent_ro'@'%'",
                         "Access denied for AuthId `avnadmin`@`%` to database 'mysql'.")
    assert "Failed at: CREATE USER 'agent_ro'@'%'" in msg
    assert "MySQL said: Access denied" in msg


def test_step_label_stops_before_the_password():
    sql = au.statements("d", "agent_ro", "%", [])[1]
    assert au.step_label(sql) == "CREATE USER 'agent_ro'@'%'"


def test_failed_step_is_reported_without_the_password():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import exc

    class Orig(Exception):
        pass

    class Conn:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            if sql.startswith("SELECT"):
                return []                                         # existing_objects: nothing
            if sql.startswith("CREATE USER"):
                raise exc.OperationalError(sql, params, Orig(3879, "Access denied ... 'mysql'"),
                                           hide_parameters=True)

    class Ctx:
        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    class Engine:
        connect = begin = lambda self: Ctx()

    from db_config import DbConfig
    cfg = DbConfig(user="u", password="p", host="h", port=3306, name="defaultdb")
    with pytest.raises(au.SetupError) as e:
        au.apply(Engine(), cfg, Names(), "agent_ro", "%", "s3cr3t")
    assert (e.value.step, e.value.code) == ("CREATE USER 'agent_ro'@'%'", 3879)
    assert "s3cr3t" not in au.admin_error(e.value.code, "avnadmin", e.value.step, e.value.message)


@pytest.fixture
def clean_env(tmp_path):
    """An empty .env, so the repo's own stays out. conftest.py has already removed every
    database setting from the shell's environment: without that, a
    CRLF .env loaded with `source .env` made the app user 'agent_ro\\r' and failed the test."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    return env_file


def _dry_run(monkeypatch, capsys, env_file):
    monkeypatch.setattr("sys.argv", ["agent_user.py", "--dry-run", "--env-file", str(env_file)])
    au.main()
    return capsys.readouterr().out


def test_dry_run_needs_no_database_and_hides_the_password(monkeypatch, capsys, clean_env):
    monkeypatch.setenv("CMMS_DB_AGENT_PASSWORD", "s3cr3t")
    monkeypatch.setenv("CMMS_DB_NAME", "jurong")
    out = _dry_run(monkeypatch, capsys, clean_env)
    assert "s3cr3t" not in out and "IDENTIFIED BY ****" in out
    assert "App user 'agent_ro'@'%'" in out                        # the default user
    assert "GRANT SELECT ON `jurong`.`incident_detail`" in out
    assert "GRANT SELECT ON `jurong`.`img_incident_reports_audit`" in out
    assert "GRANT SELECT ON `jurong`.`sop_steps_audit`" in out
    granted = out.split("Will then check")[0]
    assert "cmms_employees" not in granted and "sop_documents_raw" not in granted


def test_settings_with_windows_line_endings_still_work(monkeypatch, capsys, clean_env):
    """`source .env` on a CRLF file leaves '\\r' on every value; shell values win over .env."""
    monkeypatch.setenv("CMMS_DB_AGENT_USERNAME", "agent_ro\r")
    monkeypatch.setenv("CMMS_DB_AGENT_PASSWORD", "s3cr3t\r")
    monkeypatch.setenv("CMMS_DB_NAME", "jurong\r")
    out = _dry_run(monkeypatch, capsys, clean_env)
    assert "App user 'agent_ro'@'%' on database `jurong`" in out
    assert "\r" not in out
    assert au.agent_settings() == ("agent_ro", "s3cr3t")


def test_crlf_env_file_is_read_without_carriage_returns(monkeypatch, capsys, clean_env):
    clean_env.write_bytes(b"CMMS_DB_AGENT_USERNAME=agent_ro\r\nCMMS_DB_NAME=jurong\r\n")
    out = _dry_run(monkeypatch, capsys, clean_env)
    assert "App user 'agent_ro'@'%' on database `jurong`" in out


def test_the_old_setting_names_alone_are_not_enough(monkeypatch, capsys, clean_env):
    """Before 14 Nov 2026 these were AGENT_DB_* and DB_NAME. Only CMMS_DB_* is read now."""
    clean_env.write_text("AGENT_DB_USERNAME=agent_old\nAGENT_DB_PASSWORD=s3cr3t\n"
                         "DB_NAME=jurong\n", encoding="utf-8")
    out = _dry_run(monkeypatch, capsys, clean_env)
    assert "agent_old" not in out and "`jurong`" not in out
    with pytest.raises(ValueError, match="CMMS_DB_AGENT_PASSWORD"):
        au.agent_settings()


def test_the_page_may_show_photos_but_never_the_photo_table_or_raw_ocr():
    """img_files_public: nameplates as they are, forms only redacted. The table behind it
    and img_documents_raw (a form's person as read) stay out of reach."""
    objs = au.allowed_objects(Names())
    assert "img_files_public" in objs
    assert "img_files" not in objs and "img_documents_raw" not in objs
    assert "scan_files_public" in au.allowed_objects(Names(image_prefix="scan_"))
    probes = dict(au.forbidden_probes(Names()))
    assert "img_documents_raw" in probes["raw OCR of the scans"]
    assert "`img_files`" in probes["the photo table itself"]