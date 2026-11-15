"""Tests for the shared modules used by both scripts: db_config.py and lineage.py.

Run:  pytest tests/test_shared.py
"""
import hashlib
from datetime import datetime

import pandas as pd
import pytest

import db_config
from db_config import DbConfig, getenv, load_db_config, safe_identifier, to_records
from lineage import file_sha256, start_run


# ---------------------------------------------------------------- config ---

def test_password_never_printed():
    cfg = DbConfig(user="u", password="s3cr3t", host="h", port=3306, name="d")
    assert "s3cr3t" not in repr(cfg) and "s3cr3t" not in cfg.describe()


def test_password_with_special_characters_is_escaped():
    pytest.importorskip("sqlalchemy")
    url = DbConfig(user="u", password="p@ss:w/rd", host="h", port=3306, name="d").url()
    assert url.password == "p@ss:w/rd" and url.host == "h"


def test_engine_keeps_values_out_of_error_messages():
    """A failed insert must not print or log the row (e.g. an employee's name, PDPA)."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import exc

    engine = DbConfig(user="u", password="p", host="h", port=3306, name="d").engine()
    assert engine.hide_parameters is True
    err = exc.StatementError("insert failed", "INSERT INTO t (emp_name) VALUES (%(n)s)",
                             {"n": "Seah Jia Hui"}, Exception("boom"),
                             hide_parameters=engine.hide_parameters)
    assert "Seah Jia Hui" not in str(err)


def test_table_name_whitelist():
    assert safe_identifier("vibration_temp") == "vibration_temp"
    with pytest.raises(ValueError):
        safe_identifier("x; DROP TABLE y")


def test_getenv_drops_windows_line_endings_but_keeps_spaces(monkeypatch):
    """`source .env` on a CRLF file leaves '\\r' on every value."""
    monkeypatch.setenv("X_USER", "agent_ro\r")
    monkeypatch.setenv("X_PW", " pass word \r\n")
    monkeypatch.delenv("X_MISSING", raising=False)
    assert getenv("X_USER") == "agent_ro"
    assert getenv("X_PW") == " pass word "          # a password's own spaces are kept
    assert getenv("X_MISSING") is None and getenv("X_MISSING", "d") == "d"


def _empty_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")         # keep the repo's .env out of the test
    return str(env_file)


def test_db_settings_with_windows_line_endings(monkeypatch, tmp_path):
    for key, value in {"CMMS_DB_USERNAME": "ingest\r", "CMMS_DB_PASSWORD": "s3cr3t\r",
                       "CMMS_DB_HOST": "localhost\r", "CMMS_DB_PORT": "3307\r",
                       "CMMS_DB_NAME": "jurong_marine\r"}.items():
        monkeypatch.setenv(key, value)
    cfg = load_db_config(_empty_env(tmp_path))
    assert (cfg.user, cfg.password, cfg.host, cfg.port, cfg.name) == \
        ("ingest", "s3cr3t", "localhost", 3307, "jurong_marine")


# ------------------------------------------- setting names (14 Nov 2026) ---

def test_the_old_names_alone_are_not_enough(monkeypatch, tmp_path):
    """Only CMMS_DB_* is read: a .env still on DB_* fails, naming what to set, rather than
    running on settings nobody meant to keep."""
    for key, value in {"DB_USERNAME": "ingest", "DB_PASSWORD": "s3cr3t", "DB_HOST": "h",
                       "DB_NAME": "jurong_marine"}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError) as e:
        load_db_config(_empty_env(tmp_path))
    assert "CMMS_DB_NAME" in str(e.value) and "s3cr3t" not in str(e.value)


def test_an_empty_password_is_allowed(monkeypatch, tmp_path):
    """Local root with no password: CMMS_DB_PASSWORD= is a value, not a missing setting."""
    for key, value in {"CMMS_DB_USERNAME": "root", "CMMS_DB_PASSWORD": "", "CMMS_DB_HOST": "h",
                       "CMMS_DB_NAME": "d"}.items():
        monkeypatch.setenv(key, value)
    assert load_db_config(_empty_env(tmp_path)).password == ""


def test_missing_settings_are_named_by_their_new_names(tmp_path):
    with pytest.raises(ValueError) as e:
        load_db_config(_empty_env(tmp_path))
    msg = str(e.value)
    for key in ("CMMS_DB_USERNAME", "CMMS_DB_HOST", "CMMS_DB_NAME", "CMMS_DB_PASSWORD"):
        assert key in msg
    assert msg.count("DB_USERNAME") == msg.count("CMMS_DB_USERNAME")     # no old name


def test_another_login_to_the_shop_database(monkeypatch):
    monkeypatch.setenv("CMMS_DB_HOST", "db.example.com")
    monkeypatch.setenv("CMMS_DB_PORT", "16543")
    monkeypatch.setenv("CMMS_DB_NAME", "defaultdb")
    cfg = db_config.cmms_config("agent_ro", "pw")
    assert (cfg.user, cfg.host, cfg.port, cfg.name) == ("agent_ro", "db.example.com", 16543,
                                                        "defaultdb")


# --------------------------------------------------------------- lineage ---

def test_file_sha256(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"abc")
    assert file_sha256(str(p)) == hashlib.sha256(b"abc").hexdigest()


def test_start_run_with_sha256_reads_no_file():
    pytest.importorskip("sqlalchemy")
    seen = {}

    class Conn:
        def execute(self, stmt, params=None):
            if params:
                seen.update(params)
            return type("R", (), {"lastrowid": 42})()

    class Engine:
        def begin(self):
            class Ctx:
                def __enter__(self):
                    return Conn()

                def __exit__(self, *a):
                    return False
            return Ctx()

    run_id = start_run(Engine(), "relationships.py", "(database)", sha256="a" * 64)
    assert run_id == 42
    assert seen["f"] == "(database)" and seen["h"] == "a" * 64
    with pytest.raises(ValueError):
        start_run(Engine(), "relationships.py", "(database)", sha256="short")


def test_records_are_plain_python_types():
    df = pd.DataFrame({"i": pd.array([1, None], dtype="Int64"), "f": [1.5, float("nan")],
                       "t": pd.to_datetime(["2026-01-01", None])})
    rows = to_records(df)
    assert type(rows[0]["i"]) is int and type(rows[0]["f"]) is float
    assert type(rows[0]["t"]) is datetime
    assert rows[1] == {"i": None, "f": None, "t": None}