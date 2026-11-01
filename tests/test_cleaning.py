"""Tests for the cleaning steps. No database needed: pytest"""
import hashlib
from collections import Counter
from datetime import datetime

import pandas as pd
import pytest

import ingest_logs as logs
import ingest_spares as spares
from db_config import DbConfig, safe_identifier, to_records
from lineage import file_sha256


# ---------------------------------------------------------------- sensor log ---

def _ts(values):
    return logs.parse_timestamps(pd.DataFrame({"timestamp": values}))


def test_iso_with_offset_and_epoch_give_same_time():
    """Regression: the old script stored these 8 hours apart."""
    out = _ts(["2026-01-01T00:00:00+08:00", "1767196800000"])
    assert out["epoch_ms"].tolist() == [1767196800000, 1767196800000]
    assert out["sampled_at"].nunique() == 1
    assert str(out["sampled_at"].iloc[0]) == "2026-01-01 00:00:00"  # Singapore time
    assert out["ts_format"].tolist() == ["iso_offset", "epoch_ms"]


def test_naive_iso_is_assumed_singapore_and_z_is_utc():
    out = _ts(["2026-01-01 00:12:00", "2026-01-01T00:12:00Z"])
    assert out["ts_format"].tolist() == ["iso_naive", "iso_offset"]
    assert [str(x) for x in out["sampled_at"]] == ["2026-01-01 00:12:00", "2026-01-01 08:12:00"]


def test_bad_timestamps_are_marked_unknown_not_crash():
    out = _ts([None, "", "not a date"])
    assert out["ts_format"].tolist() == ["unknown"] * 3
    assert out["epoch_ms"].isna().all()


def test_temperature_units():
    df = pd.DataFrame({"temp": ["39.4", "102.92", "56.6", "141.4", "40", None],
                       "temp_unit": ["C", "F", "°C", "degF", "K", "C"]})
    out = logs.clean_temperatures(df)
    assert out["temp_c"].tolist()[:4] == [39.4, 39.4, 56.6, 60.78]
    assert out["unit_ok"].tolist() == [True, True, True, True, False, True]
    assert pd.isna(out["temp_c"].iloc[4])  # unknown unit -> NULL, not a guess


MESSY_CSV = """timestamp,machine_id,vib_rms_mms,temp,temp_unit
2026-01-01T00:00:00+08:00,CNC-01,1.78,39.4,C
1767196800000,cnc-01 ,1.80,102.9,F
,CNC-03,2.0,40,C
1767197520000,,2.0,40,C
1767197520000,CMP-01,,,
"""


@pytest.fixture
def lineage_run(tmp_path):
    path = tmp_path / "messy.csv"
    path.write_text(MESSY_CSV, encoding="utf-8")
    stats = Counter()
    chunks = list(logs.process(str(path), chunk_size=2, source_tz="Asia/Singapore",
                               stats=stats, first_raw_id=101, run_id=7))
    raw = pd.concat(c[0] for c in chunks)
    clean = pd.concat(c[1] for c in chunks)
    return raw, clean, stats


def test_every_file_row_is_kept_in_raw_exactly_as_written(lineage_run):
    raw, _, stats = lineage_run
    assert len(raw) == stats["rows_read"] == 5
    assert raw["raw_id"].tolist() == [101, 102, 103, 104, 105]
    assert raw["source_line"].tolist() == [2, 3, 4, 5, 6]  # header is line 1
    assert raw["machine_id"].iloc[1] == "cnc-01 "         # untouched
    assert (raw["run_id"] == 7).all() and (raw["source_file"] == "messy.csv").all()


def test_clean_rows_link_back_to_loaded_raw_rows(lineage_run):
    raw, clean, _ = lineage_run
    loaded = raw.set_index("raw_id")["status"]
    assert clean["raw_id"].tolist() == [101, 105]
    assert (loaded[clean["raw_id"]] == "loaded").all()
    assert set(raw.loc[raw["status"] == "loaded", "raw_id"]) == set(clean["raw_id"])


def test_rejects_have_reason_and_duplicates_point_to_kept_row(lineage_run):
    raw, _, _ = lineage_run
    rej = raw[raw["status"] == "rejected"].set_index("source_line")
    assert rej["reject_reason"].to_dict() == {
        3: "duplicate_reading", 4: "bad_timestamp", 5: "missing_machine_id"}
    assert rej.loc[3, "duplicate_of_raw_id"] == 101  # crosses a chunk boundary
    assert raw.set_index("source_line").loc[6, "warnings"] == "missing_temp,missing_vibration"


def test_duplicates_against_rows_already_in_db():
    seen = {("CNC-01", 1767196800000): 55}
    df = pd.DataFrame({"timestamp": ["1767196800000"], "machine_id": ["CNC-01"],
                       "vib_rms_mms": ["1"], "temp": ["40"], "temp_unit": ["C"], "raw_id": [200]})
    out = (df.pipe(logs.parse_timestamps).pipe(logs.clean_temperatures)
             .pipe(logs.clean_values).pipe(logs.split_rejects, seen=seen))
    assert out["reject_reason"].tolist() == ["duplicate_reading"]
    assert out["duplicate_of_raw_id"].tolist() == [55]


# ------------------------------------------------------------------- spares ---

@pytest.mark.parametrize("raw,expected", [
    ("$12.50", ("12.50", None)), ("$38/spool", ("38.00", "spool")), (None, (None, None)),
])
def test_parse_cost(raw, expected):
    value, per = spares.parse_cost(raw)
    assert (str(value) if value is not None else None, per) == expected


@pytest.mark.parametrize("raw,expected", [
    ("3 days", (3, 3, 0, None)),
    ("2-3 weeks (Germany)", (14, 21, 0, "Germany")),
    ("6-8 weeks!!", (42, 56, 0, None)),
    ("1 wk", (7, 7, 0, None)),
    ("local", (None, None, 1, None)),
])
def test_parse_lead_time(raw, expected):
    assert spares.parse_lead_time(raw) == expected


@pytest.mark.parametrize("desc,unit,mm", [
    ("V-Belt 5V x 85 in", "in", 2159.0),
    ("Mech seal kit 1-3/8 inch", "in", 34.92),
    ("Welding wire ER70S-6 0.045in", "in", 1.14),
    ("Insert tip DNMG 2.4mm nose", "mm", 2.4),
    ("Bearing 6205 2RS", None, None),        # designation, not decoded
    ("Hydraulic oil ISO VG32 (20L)", None, None),
])
def test_parse_size(desc, unit, mm):
    _, u, size_mm, _ = spares.parse_size(desc)
    assert (u, size_mm) == (unit, mm)


def test_split_machines():
    assert spares.split_machines("CNC-01/02/03") == ["CNC-01", "CNC-02", "CNC-03"]
    assert spares.split_machines("ALL") == ["ALL"]
    assert spares.split_machines(None) == []


# ------------------------------------------------------------------ config ---

def test_password_never_printed():
    cfg = DbConfig(user="u", password="s3cr3t", host="h", port=3306, name="d")
    assert "s3cr3t" not in repr(cfg) and "s3cr3t" not in cfg.describe()


def test_password_with_special_characters_is_escaped():
    pytest.importorskip("sqlalchemy")
    url = DbConfig(user="u", password="p@ss:w/rd", host="h", port=3306, name="d").url()
    assert url.password == "p@ss:w/rd" and url.host == "h"


def test_table_name_whitelist():
    assert safe_identifier("vibration_temp") == "vibration_temp"
    with pytest.raises(ValueError):
        safe_identifier("x; DROP TABLE y")


# ----------------------------------------------------------------- lineage ---

def test_file_sha256(tmp_path):
    p = tmp_path / "f.csv"
    p.write_bytes(b"abc")
    assert file_sha256(str(p)) == hashlib.sha256(b"abc").hexdigest()


def test_records_are_plain_python_types():
    df = pd.DataFrame({"i": pd.array([1, None], dtype="Int64"), "f": [1.5, float("nan")],
                       "t": pd.to_datetime(["2026-01-01", None])})
    rows = to_records(df)
    assert type(rows[0]["i"]) is int and type(rows[0]["f"]) is float
    assert type(rows[0]["t"]) is datetime
    assert rows[1] == {"i": None, "f": None, "t": None}
