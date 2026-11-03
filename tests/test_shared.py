"""Tests for the shared modules used by both scripts: db_config.py and lineage.py.

Run:  pytest tests/test_shared.py
"""
import hashlib
from datetime import datetime

import pandas as pd
import pytest

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


def test_db_settings_with_windows_line_endings(monkeypatch, tmp_path):
    for key, value in {"DB_USERNAME": "ingest\r", "DB_PASSWORD": "s3cr3t\r",
                       "DB_HOST": "localhost\r", "DB_PORT": "3307\r",
                       "DB_NAME": "jurong_marine\r"}.items():
        monkeypatch.setenv(key, value)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")         # keep the repo's .env out of the test
    cfg = load_db_config(str(env_file))
    assert (cfg.user, cfg.password, cfg.host, cfg.port, cfg.name) == \
        ("ingest", "s3cr3t", "localhost", 3307, "jurong_marine")


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