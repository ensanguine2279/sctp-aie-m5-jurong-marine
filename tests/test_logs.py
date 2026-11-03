"""Tests for scripts/ingest_logs.py (sensor log). No database needed.

Run:  pytest tests/test_logs.py
"""
from collections import Counter

import pandas as pd
import pytest

import ingest_logs as logs


# ------------------------------------------------------------- timestamps ---

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


# ------------------------------------------------------------ line endings ---
# trap_list.md L8: the client's vibration_temp_2026H1.csv has Windows (CRLF) line
# endings. A reader that splits on '\n' only leaves '\r' on the last field, so the
# header becomes 'temp_unit\r' and the load stops. These check that a CRLF file is
# read exactly like the same file with LF endings.

# First lines of vibration_temp_2026H1.csv verbatim, plus a Fahrenheit reading on a
# last line with no trailing newline (where a stray '\r' is most often left).
CRLF_ROWS = [
    "timestamp,machine_id,vib_rms_mms,temp,temp_unit",
    "2026-01-01T00:00:00+08:00,CNC-01,1.78,39.4,C",
    "2026-01-01T00:00:00+08:00,CNC-02,2.35,56.6,C",
    "1767196800000,CNC-03,2.19,141.4,F",
    "1767196800000,CMP-01,3.32,112.2,F",
    "1767196800000,PMP-T01,2.68,46.7,C",
    "1767197520000,CNC-01,2.01,47.4,C",
    "2026-01-01T00:12:00+08:00,CNC-03,2.21,140.0,F",
]


def _process_file(path):
    stats = Counter()
    chunks = list(logs.process(str(path), chunk_size=3, source_tz="Asia/Singapore",
                               stats=stats))
    raw = pd.concat([c[0] for c in chunks], ignore_index=True)
    clean = pd.concat([c[1] for c in chunks], ignore_index=True)
    return raw, clean, stats


def test_crlf_file_keeps_every_temperature(tmp_path):
    path = tmp_path / "crlf.csv"
    path.write_bytes("\r\n".join(CRLF_ROWS).encode())
    assert path.read_bytes().count(b"\r\n") == len(CRLF_ROWS) - 1  # really CRLF

    raw, clean, stats = _process_file(path)

    assert stats["rows_read"] == len(CRLF_ROWS) - 1
    assert stats["rows_rejected"] == 0
    assert not any(k.startswith("warn:") for k in stats), dict(stats)
    assert not raw["temp_unit"].str.contains("\r").any()  # raw text stored without '\r'
    assert set(raw["temp_unit"]) == {"C", "F"}
    assert clean["temp_c"].tolist() == [39.4, 56.6, 60.78, 44.56, 46.7, 47.4, 60.0]
    assert raw["source_line"].tolist() == list(range(2, len(CRLF_ROWS) + 1))


def test_crlf_and_lf_give_the_same_result(tmp_path):
    crlf, lf = tmp_path / "crlf.csv", tmp_path / "lf.csv"
    crlf.write_bytes(("\r\n".join(CRLF_ROWS) + "\r\n").encode())
    lf.write_bytes(("\n".join(CRLF_ROWS) + "\n").encode())

    raw_c, clean_c, stats_c = _process_file(crlf)
    raw_l, clean_l, stats_l = _process_file(lf)

    pd.testing.assert_frame_equal(clean_c, clean_l)
    pd.testing.assert_frame_equal(raw_c.drop(columns="source_file"),
                                  raw_l.drop(columns="source_file"))
    assert stats_c == stats_l