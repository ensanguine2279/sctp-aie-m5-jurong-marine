"""Tests for scripts/ingest_cmms.py. No database needed: pytest tests/test_cmms.py"""
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ingest_cmms as ic  # noqa: E402


# ---- reading the dump ---------------------------------------------------------

def test_semicolons_and_quotes_inside_strings_do_not_split_statements():
    sql = "INSERT INTO t VALUES (1,'a;b','it''s','x\\'y');\nINSERT INTO t VALUES (2,'c','d','e');"
    stmts = list(ic.iter_statements(sql))
    assert len(stmts) == 2
    assert stmts[1].line == 2


def test_comments_and_directives_are_ignored():
    sql = "-- header; with semicolon\n/*!40101 SET NAMES utf8 */;\n# note;\nSELECT 1;"
    stmts = list(ic.iter_statements(sql))
    assert [s.text for s in stmts] == ["SELECT 1"]
    assert stmts[0].line == 4


def test_multi_row_insert_keeps_line_and_tuple_number():
    sql = ("INSERT INTO `jurong_marine`.`machines` (`machine_id`,`model`) VALUES\n"
           "('CNC-01','Mazak'),\n('CNC-02',NULL);")
    tuples = ic.parse_dump(sql).tuples["machines"]
    assert [(t["source_line"], t["tuple_no"]) for t in tuples] == [(2, 1), (3, 2)]
    assert tuples[1]["values"] == ["CNC-02", None]
    assert tuples[0]["raw_tuple"] == "('CNC-01','Mazak')"
    assert tuples[0]["columns"] == ["machine_id", "model"]


def test_string_null_is_not_sql_null():
    tup = ic.parse_dump("INSERT INTO t VALUES ('NULL', NULL, -3.5);").tuples["t"][0]
    assert tup["values"] == ["NULL", None, "-3.5"]


def test_create_table_gives_column_order():
    sql = ('CREATE TABLE "work_orders" (\n "wo_id" int NOT NULL,\n "cost_sgd" decimal(8,2),\n'
           ' PRIMARY KEY ("wo_id")\n);')
    assert ic.parse_dump(sql).create_columns["work_orders"] == ["wo_id", "cost_sgd"]


def test_insert_column_list_maps_values_by_name():
    sql = "INSERT INTO machines (model, machine_id) VALUES ('Haas','CNC-02');"
    frame = ic.to_frame(ic.SPEC_BY_NAME["machines"], ic.parse_dump(sql))
    assert frame.loc[0, "machine_id"] == "CNC-02"
    assert frame.loc[0, "model"] == "Haas"


def test_bad_tuples_are_rejected_not_crashed():
    sql = "INSERT INTO machines VALUES ('CNC-01' 'a');\nINSERT INTO machines VALUES ('CNC-02','x');"
    raw, clean = ic.process(ic.parse_dump(sql), Counter())["machines"]
    assert list(raw["reject_reason"]) == ["malformed_tuple", "column_count_mismatch"]
    assert len(clean) == 0


# ---- cleaning steps -------------------------------------------------------------

def test_ids_are_trimmed_and_upper_cased_like_the_logs():
    df = pd.DataFrame({"machine_id": [" cnc-02 ", None]}, dtype=object)
    out = ic.clean_text_columns(df, codes=("machine_id",))
    assert out.loc[0, "machine_id"] == "CNC-02"
    assert pd.isna(out.loc[1, "machine_id"])        # None or NaN, depending on pandas version


def test_numbers():
    assert ic.parse_int("1,200") == 1200
    assert ic.parse_int("12.5") is None
    assert ic.parse_decimal("S$1,250.5", 2) == Decimal("1250.50")
    assert ic.parse_decimal("abc", 2) is None


def test_unreadable_number_is_flagged_not_silently_null():
    df = pd.DataFrame({"lost_days": ["3", "abc", None]}, dtype=object)
    out = ic.parse_numbers(df, ints=("lost_days",))
    assert list(out["lost_days_given"]) == [True, True, False]
    assert out["lost_days"].isna().tolist() == [False, True, True]


def test_dates():
    assert ic.parse_date("2025-06-30") == (date(2025, 6, 30), "iso")
    assert ic.parse_date("03/04/2025") == (date(2025, 4, 3), "dmy_slash")
    assert ic.parse_date("0000-00-00")[1] == "zero_date"
    assert ic.parse_date("soon")[1] == "unparseable"


# ---- severity scale flip -------------------------------------------------------

def severity(rows, **kw):
    df = pd.DataFrame(rows, columns=["severity", "incident_date"], dtype=object)
    df = ic.parse_numbers(df, ints=("severity",))
    return ic.normalise_severity(df, **kw)


def test_old_scale_is_inverted_and_new_scale_kept():
    out = severity([["1", date(2025, 3, 1)], ["5", date(2025, 6, 30)], ["5", date(2025, 8, 1)]])
    assert list(out["severity_scale"]) == ["1_worst", "1_worst", "5_worst"]
    assert list(out["severity_5_worst"]) == [5, 1, 5]
    assert list(out["severity_as_logged"]) == [1, 5, 5]


def test_july_2025_is_left_unconverted():
    out = severity([["2", date(2025, 7, 15)]])
    assert out.loc[0, "severity_scale"] == "ambiguous"
    assert pd.isna(out.loc[0, "severity_5_worst"])
    assert out.loc[0, "severity_warning"] == "severity_scale_ambiguous"


def test_confirmed_cutover_removes_ambiguity():
    out = severity([["2", date(2025, 7, 13)], ["2", date(2025, 7, 14)]],
                   cutover=date(2025, 7, 14), ambiguous_until=None)
    assert list(out["severity_scale"]) == ["1_worst", "5_worst"]


def test_severity_is_never_guessed():
    out = severity([["9", date(2025, 8, 1)], ["3", None], [None, date(2025, 8, 1)]])
    assert list(out["severity_scale"]) == ["unknown"] * 3
    assert out["severity_5_worst"].isna().all()
    assert list(out["severity_warning"]) == ["severity_out_of_range", "severity_scale_unknown",
                                             "missing_severity"]


# ---- whole pipeline -----------------------------------------------------------

DUMP = """
INSERT INTO machines VALUES ('CNC-01','Lathe','Mazak',2015),(' cnc-02 ','Mill','Haas',2018);
INSERT INTO employees VALUES (1,'E001','Tan Ah Kow','Machinist','Day'),
 (2,'E002','Lim','machinist ','NIGHT'),(2,'E099','Dup','x','y');
INSERT INTO work_orders VALUES (10,'CNC-02','PM','Grease','2026-01-05',1.5,'120.00','E001','Done'),
 (11,'CNC-99','CM','Fix','2026-01-06',-2,'50','E777','open'),
 (10,'CNC-02','PM','Grease','2026-01-05',1.5,'120.00','E001','Done');
INSERT INTO safety_incidents VALUES (100,'2025-03-01','CNC-02','Cut',1,3,'E002','form A'),
 (101,'2025-09-01','CNC-02','Burn',5,10,'E002','form B'),
 (102,'2025-07-10','CNC-01','Slip',2,0,'E001',NULL);
INSERT INTO spares VALUES (1,'bearing');
"""


def run(**kw):
    stats = Counter()
    return ic.process(ic.parse_dump(DUMP), stats, **kw), stats


def by_key(df, key):
    return {r[key]: r for _, r in df.iterrows()}


def test_pipeline_end_to_end():
    tables, stats = run()

    raw, clean = tables["machines"]
    assert list(clean["machine_id"]) == ["CNC-01", "CNC-02"]

    raw, clean = tables["employees"]
    assert raw.loc[2, "reject_reason"] == "duplicate_key_conflict"
    assert raw.loc[2, "duplicate_of_raw_id"] == raw.loc[1, "raw_id"]
    assert list(clean["trade"]) == ["machinist", "machinist"]

    raw, clean = tables["work_orders"]
    assert raw.loc[2, "reject_reason"] == "duplicate_key"          # identical repeat
    wo = by_key(raw, "wo_id")
    assert pd.isna(wo["10"]["warnings"])
    assert set(wo["11"]["warnings"].split(",")) >= {"unknown_machine", "unknown_employee",
                                                    "negative_duration_hrs"}
    assert 11 in set(clean["wo_id"])                                 # orphans flagged, kept

    raw, clean = tables["safety_incidents"]
    inc = by_key(clean, "incident_id")
    assert inc[100]["severity_5_worst"] == 5                         # old 1 = worst -> 5
    assert inc[100]["severity_as_logged"] == 1
    assert inc[101]["severity_5_worst"] == 5
    assert inc[102]["severity_scale"] == "ambiguous"

    assert "spares" not in tables
    assert stats["skipped:spares"] == 1
    assert stats["rows_read"] == 11 and stats["rows_rejected"] == 2


def test_raw_ids_continue_from_the_database():
    tables, _ = run(first_raw_ids={"machines": 500})
    assert list(tables["machines"][0]["raw_id"]) == [500, 501]


def test_key_loaded_from_another_file_is_a_duplicate_but_still_a_parent():
    tables, _ = run(seen={"machines": {"CNC-01": 7}})
    raw, _ = tables["machines"]
    assert raw.loc[0, "duplicate_of_raw_id"] == 7
    wo = by_key(tables["safety_incidents"][0], "incident_id")
    warnings = wo["102"]["warnings"]
    assert pd.isna(warnings) or "unknown_machine" not in warnings   # CNC-01 still known


def test_raw_rows_keep_the_text_as_written():
    tables, _ = run()
    raw, _ = tables["machines"]
    assert raw.loc[1, "machine_id"] == " cnc-02 "
    assert raw.loc[1, "raw_tuple"] == "(' cnc-02 ','Mill','Haas',2018)"


def test_work_order_status_does_not_clash_with_load_status():
    tables, _ = run()
    raw, clean = tables["work_orders"]
    assert raw.columns.is_unique
    assert raw.loc[0, "status_src"] == "Done" and raw.loc[0, "status"] == "loaded"
    assert clean.iloc[0]["status"] == "done"


def test_ddl_builds_for_every_table():
    for spec in ic.SPECS:
        stmts = ic.ddl(spec, "cmms_" + spec.name)
        assert len(stmts) == 3
        assert f"cmms_{spec.name}_audit" in stmts[2]