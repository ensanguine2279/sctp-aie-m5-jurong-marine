"""Tests for ingest/ingest_spares.py (critical spares sheet). No database needed.

Run:  pytest tests/test_spares.py
"""
import pandas as pd
import pytest

import ingest_spares as spares


# --------------------------------------------------------------- parsing ---

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


# --------------------------------------------------------------- lineage ---

@pytest.fixture
def messy_sheet(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["TEST CRITICAL SPARES LIST rev 12"])
    ws.append([])
    ws.append(["Part No", "Description", "For Machine", "Qty", "Min Qty", "Unit Cost",
               "Lead Time", "Location"])
    ws.append(["brg-1 ", "Bearing", "CNC-01/02", 8, 4, "$12.50", "3 days", "S1"])     # row 4
    ws.append(["BRG-1", "Bearing again", "CNC-03", 1, 1, "$13", "1 wk", "S1"])        # row 5 dup
    ws.append([None, "No part number", "CNC-01", 1, 1, "$1", "local", "S1"])          # row 6
    ws.append([])                                                                     # row 7 blank
    ws.append(["SEAL-1", "Seal 1-3/8 inch", "PMP-01", None, "two", "ask Halim",
               "when available", "S2"])                                               # row 8
    path = tmp_path / "spares.xlsx"
    wb.save(path)
    return spares.prepare(str(path), run_id=3)


def test_spares_raw_keeps_every_sheet_row_as_written(messy_sheet):
    raw, _, _, meta = messy_sheet
    assert meta["source_rev"] == "12"
    assert raw["source_row"].tolist() == [4, 5, 6, 8]          # blank row 7 skipped
    assert raw["raw_id"].tolist() == [300004, 300005, 300006, 300008]
    assert raw["part_no"].iloc[0] == "brg-1"                   # trimmed text, not upper-cased
    assert raw["qty"].iloc[0] == "8" and raw["unit_cost"].iloc[3] == "ask Halim"


def test_spares_clean_links_to_raw_and_rejects_have_reasons(messy_sheet):
    raw, clean, links, _ = messy_sheet
    assert clean["part_no"].tolist() == ["BRG-1", "SEAL-1"]
    assert clean["raw_id"].tolist() == [300004, 300008]
    rej = raw[raw["status"] == "rejected"].set_index("source_row")
    assert rej["reject_reason"].to_dict() == {5: "duplicate_part_no", 6: "missing_part_no"}
    assert rej.loc[5, "duplicate_of_raw_id"] == 300004
    assert links.values.tolist() == [["BRG-1", "CNC-01"], ["BRG-1", "CNC-02"],
                                     ["SEAL-1", "PMP-01"]]


def test_spares_warnings_for_unreadable_values(messy_sheet):
    raw, clean, _, _ = messy_sheet
    warn = raw.set_index("source_row").loc[8, "warnings"].split(",")
    assert set(warn) == {"qty_missing_or_invalid", "min_qty_missing_or_invalid",
                         "unit_cost_unparsed", "lead_time_unparsed"}
    seal = clean.set_index("part_no").loc["SEAL-1"]
    assert seal["qty"] == 0 and pd.isna(seal["unit_cost"]) and seal["size_mm"] == 34.92
