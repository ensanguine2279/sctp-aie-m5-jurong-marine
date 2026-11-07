"""Tests for scripts/ingest_images.py. No database, no Tesseract and no LLM needed:
the OCR text below is what Tesseract actually read from the two sample scans (with its
mistakes), passed in as an Extraction. One test reads the real scans if Tesseract and
data/images/ are there, and is skipped otherwise."""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest

import ingest_images as ii
from ingest_images import Extraction, ImageRead
from lineage import folder_sha256

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------- OCR texts ---
# IR-2024-900.jpg, three preprocessing variants: note 'Loc ation', 'LTO', '9OO', 'EMPOO1'.
FORM_GRAY = """JURONG MARINE & ENGINEERING PTE LTO
WORKPLACE SAFETY & HEALTH — INCIDENT REPORT FORM (WSH-F02)
Report No: IR-2024-900
Date of incident: 01/07/2024
Loc ation/machine: LTH-01 — workshop bay
Person involved: EMP001
Description: weld flash burn
Severity (1 = MOST SEVERE): 4
Lost work days: 0
Reporting: found during audit
Immediate action taken:
first aid administered, sent to clinic
Root cause (5-Why attached?): see attached
Safety Officer signature: rt ™~
"""
FORM_OTSU = FORM_GRAY.replace("IR-2024-900", "IR-2024-9OO").replace("EMP001", "EMPOO1")
FORM_ADAPTIVE = FORM_GRAY.replace("Lost work days: 0", "Lost work days: O")

# nameplate_BLAST-01.jpg: Otsu reads 1000 h as 1660 h and the tag as 'UME): BLAST!'.
PLATE_GRAY = """@ @
CUSTOM
MODEL SG-Fab
SERIAL NO CU6744884
MFG DATE 03/2014
POWER 15 KW 3PH 389-440V
CAPACITY 450 A @ 60%
WEIGHT 7929 kg
LUBRICANT Shell Tonna $3 M66
GREASE INTERVAL 1000 h
ASSET TAG (JME): BLAST-01
@ @
"""
PLATE_OTSU = (PLATE_GRAY.replace("389-440V", "380-440V").replace("$3 M66", "$3 M68")
              .replace("1000 h", "1660 h").replace("(JME): BLAST-01", "UME): BLAST!")
              .replace("15 KW", "15 kW"))
PLATE_ADAPTIVE = (PLATE_GRAY.replace("389-440V", "380-440V").replace("Shell Tonna $3 M66",
                                                                     "Shelt Tonna $3 (68")
                  .replace("15 KW", "15 kW").replace("BLAST-01", "BLAST-01 ."))

FORM = Extraction(ii.TESSERACT_TAG, texts={"gray": FORM_GRAY, "otsu": FORM_OTSU,
                                           "adaptive": FORM_ADAPTIVE}, signature=1)
PLATE = Extraction(ii.TESSERACT_TAG, texts={"gray": PLATE_GRAY, "otsu": PLATE_OTSU,
                                            "adaptive": PLATE_ADAPTIVE})


def form_text(**changes) -> Extraction:
    """The incident form with some lines changed, e.g. date='04/07/2025'."""
    labels = {"report_no": "Report No", "date": "Date of incident", "person": "Person involved",
              "severity_label": None, "severity": None, "location": "Loc ation/machine"}
    text = FORM_GRAY
    for k, v in changes.items():
        if k == "severity_line":
            text = text.replace("Severity (1 = MOST SEVERE): 4", v)
        else:
            old = next(line for line in text.splitlines() if line.startswith(labels[k]))
            text = text.replace(old, f"{labels[k]}: {v}")
    return Extraction(ii.TESSERACT_TAG, texts={"gray": text}, signature=1)


def run(*extractions, files=None, **kw):
    reads = [ImageRead(f"img{i}.jpg" if not files else files[i], f"{i:064x}", ex)
             for i, ex in enumerate(extractions)]
    stats = Counter()
    out = ii.process(reads, stats, source_folder="images", **kw)
    return out, stats


# ----------------------------------------------------------------- parsing ---

def test_classify():
    assert ii.classify(FORM_GRAY) == "incident_report"
    assert ii.classify(PLATE_GRAY) == "nameplate"
    assert ii.classify("a photo of a cat") == "unknown"


def test_fuzzy_labels_allow_split_words_but_not_inside_character_classes():
    import re
    assert re.search(ii.fuzzy("Location"), "Loc ation/machine")
    assert r"\s?" not in ii.fuzzy("[JU]")                 # class left alone
    assert ii.fuzzy(r"\s*") == r"\s*"                       # escapes left alone


def test_incident_text_parsed_label_by_label():
    f = ii.parse_incident_text(FORM_GRAY)
    assert f["report_no"] == "IR-2024-900" and f["date"] == "01/07/2024"
    assert f["location"] == "LTH-01 — workshop bay"               # 'Loc ation' still found
    assert f["severity"] == "4" and f["severity_label"] == "1 = MOST SEVERE"
    assert f["immediate_action"] == "first aid administered, sent to clinic"   # next line
    assert f["root_cause"] == "see attached"


def test_nameplate_text_parsed_with_ocr_fixes():
    f = ii.parse_nameplate_text(PLATE_OTSU)
    assert f["manufacturer"] == "CUSTOM"                     # not the '@ @' screw holes
    assert f["lubricant"] == "Shell Tonna S3 M68"            # '$3' -> 'S3'
    assert f["asset_tag"] == "BLAST!"                        # '(JME)' read as 'UME)'


@pytest.mark.parametrize("label,scale", [
    ("1 = MOST SEVERE", "1_worst"), ("5 = most severe", "5_worst"),
    ("l = MOST SEVERE", "1_worst"), ("1 = least severe", "5_worst"),
    ("5=WORST", "5_worst"), ("blurred", None), (None, None),
])
def test_scale_read_from_the_printed_label(label, scale):
    assert ii.scale_from_label(label) == scale


def test_ids_fixed_only_where_digits_belong():
    assert ii.norm_id("IR-2024-9OO", 2) == "IR-2024-900"
    assert ii.norm_id("EMPOO1", 3) == "EMP001"
    out = ii.clean_nameplate_ids(ii.pd.DataFrame(
        {"asset_tag": ["blast-O1", "BLAST!", "WELD-0l"], "serial_no": [None] * 3,
         "manufacturer": [None] * 3, "model": [None] * 3, "lubricant": [None] * 3}))
    tags = out["asset_tag"]
    assert (tags[0], tags[2]) == ("BLAST-01", "WELD-01")     # BLAST, not BLA5T
    assert ii.pd.isna(tags[1])                                # 'BLAST!' has no number: no tag


@pytest.mark.parametrize("line,tag", [
    # what Tesseract read on the CNC-01, PMP-T01 and WELD-02 plates (all three were rejected
    # as missing_key before: '{ME}' and 'GME)' didn't match the label, and the T in PMP-T01
    # didn't fit the tag pattern)
    ("ASSET TAG {ME}: CNC-01", "CNC-01"),
    ("ASSET TAG (JME): PMP-T01", "PMP-T01"),
    ("ASSET TAG UME): PMP-T01", "PMP-T01"),
    ("ASSET TAG (JME): PMP-TO1", "PMP-T01"),            # O for 0 fixed, the T kept
    ("ASSET TAG GME): WELD-02", "WELD-02"),
    ("ASSET TAG (JME: BLAST-01", "BLAST-01"),
    ("ASSET TAG: MEX-01", "MEX-01"),                     # a tag starting 'ME' isn't a label
])
def test_asset_tag_read_whatever_ocr_makes_of_the_jme_label(line, tag):
    plate = PLATE_GRAY.replace("ASSET TAG (JME): BLAST-01", line)
    out, _ = run(Extraction(ii.TESSERACT_TAG, texts={"gray": plate}))
    assert out["nameplate"]["asset_tag"].tolist() == [tag]


def test_tag_with_a_letter_before_the_digits():
    assert ii.asset_tag_value("GME): WELD-02") == "WELD-02"
    assert ii.asset_tag_value("UME): BLAST!") == "BLAST!"     # label gone, value kept as read
    out = ii.clean_nameplate_ids(ii.pd.DataFrame(
        {"asset_tag": ["pmp-tO1", "LTH-O1"], "serial_no": [None] * 2,
         "manufacturer": [None] * 2, "model": [None] * 2, "lubricant": [None] * 2}))
    assert out["asset_tag"].tolist() == ["PMP-T01", "LTH-01"]  # O as 0 unless a digit follows
    assert ii.VALIDATORS["nameplate"]["asset_tag"]("PMP-T01")
    assert not ii.VALIDATORS["nameplate"]["asset_tag"]("PMP-TO1")   # vote prefers PMP-T01


def test_incident_form_on_a_machine_with_a_letter_in_its_id():
    """Forms 941 and 955 name PMP-T01: before, they were loaded with missing_machine_id."""
    out, _ = run(form_text(location="PMP-T01 — pump room"))
    assert out["incident_report"].iloc[0]["machine_id"] == "PMP-T01"
    assert "missing_machine_id" not in (out["documents"].iloc[0]["warnings"] or "")


# ------------------------------------------------------------------- voting ---

def test_vote_takes_the_majority_reading_that_passes_its_check():
    fields, notes = ii.vote({k: ii.parse_nameplate_text(t) for k, t in PLATE.texts.items()},
                            "nameplate")
    assert fields["grease_interval"] == "1000 h"             # 2 of 3, not Otsu's 1660
    assert fields["asset_tag"] == "BLAST-01"                 # 'BLAST!' fails the tag check
    assert fields["lubricant"] == "Shell Tonna S3 M68"       # M66 isn't an ISO VG grade
    flagged = {n["field"] for n in notes if n["issue"] == "disagreed"}
    assert {"grease_interval", "asset_tag", "lubricant"} <= flagged
    g = next(n for n in notes if n["field"] == "grease_interval")
    assert g["readings"] == {"gray": "1000 h", "otsu": "1660 h", "adaptive": "1000 h"}


def test_vote_ties_go_to_the_first_variant_and_spacing_is_not_a_disagreement():
    fields, notes = ii.vote({"a": {"capacity": "450 A @ 60%"}, "b": {"capacity": "450A @ 60%"}},
                            "nameplate")
    assert fields["capacity"] == "450 A @ 60%" and notes == []


def test_value_that_fails_every_check_is_kept_but_flagged():
    fields, notes = ii.vote({"a": {"asset_tag": "BLAST!"}}, "nameplate")
    assert fields["asset_tag"] == "BLAST!"
    assert notes == [{"field": "asset_tag", "issue": "failed_format", "chose": "BLAST!"}]


def test_vote_keeps_the_only_reading_shaped_like_a_machine_id():
    """trap_list I10: IR-2025-914.jpg. No two passes agreed on the location; only the
    adaptive reading is a machine id. Choosing 'CRN-O02' gave machine_id CRN-002, an orphan."""
    readings = {"gray": {"location": "CRN-O02 — workshop bay", "severity": "3)"},
                "otsu": {"location": "CRN-G2 — workshop bay", "severity": "5"},
                "adaptive": {"location": "CRN-02 — workshop bay", "severity": "5"}}
    fields, notes = ii.vote(readings, "incident_report")
    assert fields["location"] == "CRN-02 — workshop bay"
    assert fields["severity"] == "5"                                  # majority
    assert {n["field"] for n in notes if n["issue"] == "disagreed"} == {"location", "severity"}
    assert not [n for n in notes if n["issue"] == "failed_format"]


def test_machine_id_check_allows_a_letter_but_not_a_misread_digit():
    """PMP-T01 is a real id; CRN-O02 / CRN-G2 / BLAST-O1 are OCR misreads (trap_list I10)."""
    check = ii.VALIDATORS["incident_report"]["location"]
    assert check("PMP-T01 — test bay") and check("CRN-02 — workshop bay")
    assert not check("CRN-O02 — workshop bay") and not check("CRN-G2 — workshop bay")
    assert not ii.VALIDATORS["nameplate"]["asset_tag"]("BLAST-O1")


# ------------------------------------------------- severity from the value ink ---
# The new form's label runs into the value column: OCR reads the line as
# 'Severity (5 = MOST SEVERE, new W8H matrix):' and the blue number under it is lost (I12).
NEW_FORM = FORM_GRAY.replace("Severity (1 = MOST SEVERE): 4",
                             "Severity (5 = MOST SEVERE, new W8H matrix):").replace(
    "IR-2024-900", "IR-2025-904").replace("01/07/2024", "29/12/2025")


def test_value_ink_reading_recovers_a_severity_the_page_text_lost():
    out, _ = run(Extraction(ii.TESSERACT_TAG, texts={"gray": NEW_FORM, "otsu": NEW_FORM},
                            signature=1, ink={"severity": "2"}))
    r = out["incident_report"].iloc[0]
    assert (r["incident_id"], r["severity_scale"], r["severity_5_worst"]) == (904, "5_worst", 2)
    assert "read_from_value_ink_severity" in out["documents"].iloc[0]["warnings"]


def test_value_ink_never_outvotes_page_readings_that_agree():
    ex = Extraction(ii.TESSERACT_TAG, texts=FORM.texts, signature=1, ink={"severity": "5"})
    out, _ = run(ex)
    assert out["incident_report"].iloc[0]["severity_as_logged"] == 4      # 3 page reads say 4
    assert "read_from_value_ink" not in out["documents"].iloc[0]["warnings"]


def test_without_a_value_ink_reading_the_severity_stays_unread():
    out, _ = run(Extraction(ii.TESSERACT_TAG, texts={"gray": NEW_FORM}, signature=1))
    assert ii.pd.isna(out["incident_report"].iloc[0]["severity_5_worst"])


@pytest.mark.parametrize("readings,digit", [
    ({0: "3", 1: "3", 2: "3", 3: "3", 4: "3"}, "3"),
    ({0: "2", 1: "2", 2: "2", 3: "4"}, "2"),               # 3 vs 1: clear majority
    ({0: "3", 1: "3", 2: "3", 3: "4", 4: "4"}, None),      # 3 vs 2: too close, abstain
    ({0: "5", 1: "5"}, None),                              # only 2 masks: too few
    ({}, None),
])
def test_ink_vote_abstains_rather_than_guess(readings, digit):
    assert ii.ink_majority(readings) == digit


def _mock_new_form_line(digit: str):
    """Like the real forms: grey label, small navy value printed over its 'WSH'."""
    import cv2
    import numpy as np
    img = np.full((60, 760, 3), 240, np.uint8)
    cv2.putText(img, "Severity (5 = MOST SEVERE, new WSH matrix):", (10, 35),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (100, 98, 95), 1, cv2.LINE_AA)
    cv2.putText(img, digit, (282, 37), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (125, 72, 78), 2,
                cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs Tesseract")
def test_value_ink_reader_never_returns_a_wrong_digit():
    """On a mock of the overlapping line: the right digit, or nothing. Never another digit."""
    for d in "12345":
        assert ii.severity_from_ink(_mock_new_form_line(d)) in (d, None)
    assert ii.severity_from_ink(_mock_new_form_line(" ")) is None


# ----------------------------------------------------------- incident forms ---

def test_incident_form_end_to_end():
    out, stats = run(FORM)
    r = out["incident_report"].iloc[0]
    assert (r["incident_id"], r["report_no"], str(r["incident_date"])) == \
        (900, "IR-2024-900", "2024-07-01")
    assert (r["machine_id"], r["emp_code"], r["lost_days"]) == ("LTH-01", "EMP001", 0)
    assert (r["severity_as_logged"], r["severity_scale"], r["severity_5_worst"]) == \
        (4, "1_worst", 2)                                     # same as the CMMS date rule
    assert r["root_cause_attached"] == 1 and r["signature_present"] == 1
    warnings = out["documents"].iloc[0]["warnings"].split(",")
    assert "root_cause_in_attachment" in warnings
    assert "form_scale_contradicts_date_rule" not in warnings
    assert stats["incident_reports:scale:1_worst"] == 1


def test_day_first_dates_are_not_flagged_on_forms():
    """Every form writes dd/mm/yyyy: a warning on all 30 hid the ones that matter."""
    out, _ = run(form_text(date="01/07/2024"), form_text(report_no="IR-2024-919",
                                                          date="24/08/2024"))
    assert str(out["incident_report"].iloc[0]["incident_date"]) == "2024-07-01"
    assert str(out["incident_report"].iloc[1]["incident_date"]) == "2024-08-24"
    assert not out["documents"]["warnings"].str.contains("date_assumed_day_first").any()


def test_form_whose_printed_scale_contradicts_its_date_is_flagged_not_overruled():
    """C3: an old-period form printed with the new scale. The form wins; it's flagged."""
    out, _ = run(form_text(severity_line="Severity (5 = MOST SEVERE): 4"))
    r = out["incident_report"].iloc[0]
    assert (r["severity_scale"], r["severity_5_worst"]) == ("5_worst", 4)
    assert "form_scale_contradicts_date_rule" in out["documents"].iloc[0]["warnings"]


def test_form_from_the_cutover_month_settles_what_the_date_rule_cannot():
    """C2 / Q1: incident 907 (4 Jul 2025) is NULL in the CMMS; its form says which scale."""
    out, _ = run(form_text(report_no="IR-2025-907", date="04/07/2025",
                           severity_line="Severity (5 = MOST SEVERE): 2"))
    r = out["incident_report"].iloc[0]
    assert (r["incident_id"], r["severity_5_worst"]) == (907, 2)
    assert "form_scale_in_cutover_month" in out["documents"].iloc[0]["warnings"]


def test_unreadable_scale_is_never_guessed_from_the_date():
    out, _ = run(form_text(severity_line="Severity (smudged): 4"))
    r = out["incident_report"].iloc[0]
    assert r["severity_scale"] == "unknown" and ii.pd.isna(r["severity_5_worst"])
    assert "severity_scale_not_on_form" in out["documents"].iloc[0]["warnings"]


def test_report_number_year_checked_against_the_date():
    out, _ = run(form_text(report_no="IR-2023-900"))
    assert "report_year_differs_from_date" in out["documents"].iloc[0]["warnings"]


def test_a_name_instead_of_an_employee_code_is_not_stored():
    """PDPA: the clean table (and so the app) only ever gets EMP codes."""
    out, _ = run(form_text(person="Seah Jia Hui"))
    r = out["incident_report"].iloc[0]
    assert ii.pd.isna(r["emp_code"])
    assert "Seah" not in out["incident_report"].to_string()
    assert "person_not_an_emp_code" in out["documents"].iloc[0]["warnings"]
    assert "Seah" not in ii.for_display("incident_report", out["incident_report"]).to_string()


# --------------------------------------------------------------- nameplates ---

def test_nameplate_end_to_end():
    out, _ = run(PLATE)
    p = out["nameplate"].iloc[0]
    assert (p["asset_tag"], p["manufacturer"], p["model"], p["serial_no"]) == \
        ("BLAST-01", "CUSTOM", "SG-Fab", "CU6744884")
    assert (p["mfg_month"], p["power_kw"], p["phases"]) == ("2014-03", 15.0, 3)
    assert (p["voltage_min_v"], p["voltage_max_v"]) == (380, 440)
    assert (p["rated_current_a"], p["duty_cycle_pct"], p["weight_kg"]) == (450.0, 60.0, 7929.0)
    assert (p["lubricant"], p["grease_interval_h"]) == ("Shell Tonna S3 M68", 1000)
    warnings = out["documents"].iloc[0]["warnings"].split(",")
    assert "placeholder_manufacturer" in warnings
    assert "duty_cycle_rating_on_non_welder" in warnings       # 450 A @ 60% on a blast chamber
    assert "ocr_disagreed_grease_interval" in warnings


def test_welding_rating_on_a_welder_is_fine_and_weight_in_lb_is_converted():
    plate = PLATE_GRAY.replace("BLAST-01", "WELD-01").replace("7929 kg", "2,000 lb")
    out, _ = run(Extraction(ii.TESSERACT_TAG, texts={"gray": plate}))
    p = out["nameplate"].iloc[0]
    assert p["weight_kg"] == pytest.approx(907.18)
    warnings = out["documents"].iloc[0]["warnings"]
    assert "weight_converted_to_kg" in warnings and "duty_cycle_rating_on_non_welder" not in warnings


def test_unknown_machine_flagged_when_the_cmms_is_loaded():
    out, stats = run(PLATE, FORM, machine_ids={"LTH-01", "CNC-02"})
    warnings = out["documents"].set_index("doc_type")["warnings"]
    assert "unknown_machine" in warnings["nameplate"]           # BLAST-01 not in that list
    assert "unknown_machine" not in warnings["incident_report"]
    assert stats["machines_checked"] == 1


# ---------------------------------------------------------------- rejects ---

def test_rejects_keep_the_image_and_say_why():
    broken = Extraction(ii.TESSERACT_TAG, error="ValueError: not an image")
    cat = Extraction(ii.TESSERACT_TAG, texts={"gray": "a photo of a cat"})
    no_tag = Extraction(ii.TESSERACT_TAG, texts={"gray": PLATE_GRAY.replace(
        "ASSET TAG (JME): BLAST-01", "ASSET TAG (JME):")})
    out, stats = run(PLATE, broken, cat, no_tag)
    docs = out["documents"].set_index("source_file")
    assert docs.loc["img0.jpg", "status"] == "loaded"
    assert docs.loc["img1.jpg", "reject_reason"] == "unreadable_image"
    assert docs.loc["img2.jpg", "reject_reason"] == "unknown_document_type"
    assert docs.loc["img3.jpg", "reject_reason"] == "missing_key"
    assert stats["rows_read"] == 4 == stats["rows_loaded"] + stats["rows_rejected"]
    assert len(out["nameplate"]) == 1


def test_same_image_twice_and_same_key_twice():
    reads = [ImageRead("a.jpg", "a" * 64, PLATE), ImageRead("copy.jpg", "a" * 64, PLATE),
             ImageRead("b.jpg", "b" * 64, PLATE),
             ImageRead("c.jpg", "c" * 64, Extraction(ii.TESSERACT_TAG, texts={
                 "gray": PLATE_GRAY.replace("1000 h", "500 h")}))]
    out = ii.process(reads, Counter(), first_raw_id=10)
    docs = out["documents"].set_index("source_file")
    assert (docs.loc["copy.jpg", "reject_reason"], docs.loc["copy.jpg", "duplicate_of_raw_id"]) \
        == ("duplicate_image", 10)
    assert docs.loc["b.jpg", "reject_reason"] == "duplicate_key"           # same values
    assert docs.loc["c.jpg", "reject_reason"] == "duplicate_key_conflict"  # different: review
    assert out["nameplate"]["raw_id"].tolist() == [10]


def test_keys_already_loaded_from_another_folder_count_as_duplicates():
    out, _ = run(FORM, seen={"incident_report": {900: 77}}, seen_images={})
    doc = out["documents"].iloc[0]
    assert (doc["reject_reason"], doc["duplicate_of_raw_id"]) == ("duplicate_key", 77)


def test_raw_rows_keep_what_was_read():
    out, _ = run(PLATE)
    doc = out["documents"].iloc[0]
    assert json.loads(doc["fields_raw"])["grease_interval"] == "1000 h"
    assert any(n["field"] == "grease_interval" for n in json.loads(doc["ocr_notes"]))
    assert "1660 h" in doc["ocr_text"]                        # every variant kept
    assert list(out["documents"].columns) == ii.RAW_COLUMNS


# -------------------------------------------------------------- LLM engine ---

def test_llm_fields_get_the_same_parsing_and_checks():
    ex = Extraction("llm:gpt-4o-mini", signature=0, fields={
        "doc_type": "Incident Report", "report_no": "IR-2026-923", "date": "30/04/2026",
        "location": "CRN-01 overhead crane", "person": "EMP030",
        "description": "finger caught in chuck (glove)", "severity": "1",
        "severity_label": "5 = MOST SEVERE", "lost_work_days": "1",
        "reporting": "found during audit", "immediate_action": None, "root_cause": None})
    out, _ = run(ex)
    r = out["incident_report"].iloc[0]
    assert (r["incident_id"], r["machine_id"], r["severity_5_worst"]) == (923, "CRN-01", 1)
    assert r["signature_present"] == 0
    assert "signature_missing" in out["documents"].iloc[0]["warnings"]
    assert out["documents"].iloc[0]["ocr_engine"] == "llm:gpt-4o-mini"


# ------------------------------------------------------- both engines ---

LLM = "llm:gpt-4o-mini"
PLATE_LLM = {"doc_type": "nameplate", "manufacturer": "CUSTOM", "model": "SG-Fab",
             "serial_no": "CU6744884", "mfg_date": "03/2014", "power": "15 kW 3PH 380-440V",
             "capacity": "450 A @ 60%", "weight": "7929 kg", "lubricant": "Shell Tonna S3 M68",
             "grease_interval": "1000 h", "asset_tag": "BLAST-01"}
FORM_LLM = {"doc_type": "incident_report", "report_no": "IR-2024-900", "date": "01/07/2024",
            "location": "LTH-01 — workshop bay", "person": "EMP001",
            "description": "weld flash burn", "severity": "4",
            "severity_label": "1 = MOST SEVERE", "lost_work_days": "0",
            "reporting": "found during audit",
            "immediate_action": "first aid administered, sent to clinic",
            "root_cause": "see attached"}


def both(tess: Extraction, llm_fields: dict | None = None, *, llm_sig=None,
         llm_error=None) -> Extraction:
    llm = Extraction(LLM, fields=llm_fields, signature=llm_sig, error=llm_error)
    return ii.combine(tess, llm, "gpt-4o-mini")


def warnings_of(out) -> list[str]:
    return (out["documents"].iloc[0]["warnings"] or "").split(",")


def test_engines_agreeing_confirm_what_tesseract_was_unsure_of():
    """BLAST-01: Tesseract's passes disagreed on the grease interval, tag and oil; the vision
    model reads the same values, so those doubts are dropped."""
    out, _ = run(both(PLATE, PLATE_LLM))
    p = out["nameplate"].iloc[0]
    assert (p["asset_tag"], p["grease_interval_h"], p["lubricant"]) == \
        ("BLAST-01", 1000, "Shell Tonna S3 M68")
    w = warnings_of(out)
    assert not [x for x in w if x.startswith(("ocr_disagreed", "engines_disagree"))]
    assert out["documents"].iloc[0]["ocr_engine"] == "both:tesseract-v3+llm-v3:gpt-4o-mini"


def test_engines_disagreeing_are_flagged_with_both_readings():
    """CRN-02: Tesseract's passes read 509 / 566 / 500 h (no majority, 509 kept)."""
    tess = plate({"GREASE INTERVAL": "509 h"}, {"GREASE INTERVAL": "566 h"},
                 {"GREASE INTERVAL": "500 h"})
    out, _ = run(both(tess, PLATE_LLM | {"grease_interval": "500 h"}))
    assert out["nameplate"].iloc[0]["grease_interval_h"] == 500     # the vision model's
    w = warnings_of(out)
    assert "engines_disagree_grease_interval" in w
    # One warning per field: Tesseract's own doubts stay in ocr_notes, not in the warnings
    assert "ocr_no_majority_grease_interval" not in w
    assert "ocr_disagreed_grease_interval" not in w
    notes = json.loads(out["documents"].iloc[0]["ocr_notes"])
    assert {n["issue"] for n in notes if n["field"] == "grease_interval"} == \
        {"engines_disagree", "disagreed", "no_majority"}
    note = next(n for n in json.loads(out["documents"].iloc[0]["ocr_notes"])
                if n["issue"] == "engines_disagree" and n["field"] == "grease_interval")
    assert note["readings"] == {"tesseract": "509 h", "llm": "500 h"}


def test_a_lone_model_misreading_never_overrules_a_tesseract_majority():
    """BLAST-01: two of Tesseract's three passes read 1000 h. A model reading of 100 h
    (also a valid interval) is flagged, not stored."""
    out, _ = run(both(PLATE, PLATE_LLM | {"grease_interval": "100 h"}))
    assert out["nameplate"].iloc[0]["grease_interval_h"] == 1000
    assert "engines_disagree_grease_interval" in warnings_of(out)


def test_on_disagreement_the_reading_that_passes_its_check_wins():
    out, _ = run(both(PLATE, PLATE_LLM | {"asset_tag": "BLAST-O1"}))     # fails the tag check
    assert out["nameplate"].iloc[0]["asset_tag"] == "BLAST-01"
    assert "engines_disagree_asset_tag" in warnings_of(out)


def test_a_value_both_engines_read_wrong_is_still_flagged():
    """WELD-02: Tesseract read 'M58' on all three passes; if the model agrees, the grade
    check still catches it."""
    tess = plate({"LUBRICANT": "Shell Tonna S3 M58"})
    out, _ = run(both(tess, PLATE_LLM | {"lubricant": "Shell Tonna S3 M58"}))
    w = warnings_of(out)
    assert "failed_format_lubricant" in w and "engines_disagree_lubricant" not in w


def test_vision_model_supplies_a_severity_tesseract_lost():
    """I12: the new form's severity under the label print; the ink reader abstained, and
    the page text took the next line ('Lost work days: 0') as the value."""
    tess = Extraction(ii.TESSERACT_TAG, texts={"gray": NEW_FORM}, signature=1)
    llm = FORM_LLM | {"report_no": "IR-2025-904", "date": "29/12/2025", "severity": "2",
                      "severity_label": "5 = MOST SEVERE, new WSH matrix"}
    out, _ = run(both(tess, llm, llm_sig=1))
    r = out["incident_report"].iloc[0]
    assert (r["severity_scale"], r["severity_5_worst"]) == ("5_worst", 2)
    w = warnings_of(out)
    assert "engines_disagree_severity" in w                   # 'Lost work days: 0' fails
    assert "engines_disagree_severity_label" not in w        # W8H vs WSH: same scale


def test_value_ink_and_vision_model_agreeing_need_no_flag_beyond_the_ink_note():
    tess = Extraction(ii.TESSERACT_TAG, texts={"gray": NEW_FORM}, signature=1,
                      ink={"severity": "2"})
    llm = FORM_LLM | {"report_no": "IR-2025-904", "date": "29/12/2025", "severity": "2",
                      "severity_label": "5 = MOST SEVERE"}
    out, _ = run(both(tess, llm, llm_sig=1))
    w = warnings_of(out)
    assert out["incident_report"].iloc[0]["severity_5_worst"] == 2
    assert "engines_disagree_severity" not in w and "one_engine_only_severity" not in w


def test_a_field_only_one_engine_read_is_kept_and_flagged():
    llm = {k: v for k, v in PLATE_LLM.items() if k != "capacity"}
    out, _ = run(both(PLATE, llm))
    assert out["nameplate"].iloc[0]["rated_current_a"] == 450.0
    assert "one_engine_only_capacity" in warnings_of(out)


# What the two engines read on the 42 scans (6 Nov 2026, engines_disagree notes). The
# model read the scale's '5' from '(5 = MOST SEVERE ...)' as the severity on every new form.

def new_form(ink: str | None, llm_severity: str | None, *, page_severity=None) -> Extraction:
    text = NEW_FORM if page_severity is None else NEW_FORM.replace(
        "new W8H matrix):", f"new W8H matrix): {page_severity}")
    tess = Extraction(ii.TESSERACT_TAG, texts={"gray": text, "otsu": text, "adaptive": text},
                      signature=1, ink={"severity": ink} if ink else None)
    llm = FORM_LLM | {"report_no": "IR-2025-904", "date": "29/12/2025",
                      "severity": llm_severity, "severity_label": "MOST SEVERE, new WSH matrix"}
    return both(tess, llm, llm_sig=1)


@pytest.mark.parametrize("ink,model,kept", [
    ("2", "5", 2),      # 904: ink 2 = CMMS; the model gave the label's 5
    ("3", "5", 3),      # 906
    ("2", "5", 2),      # 975
    ("5", "2", 5),      # 913: ink 5 = CMMS; the model 2
])
def test_blue_ink_reading_beats_the_model_on_the_new_forms(ink, model, kept):
    out, _ = run(new_form(ink, model))
    assert out["incident_report"].iloc[0]["severity_5_worst"] == kept


def test_model_reading_the_scale_digit_is_never_used_on_its_own():
    """911: the ink reader abstained, the page text took the next line, the model gave 5."""
    out, _ = run(new_form(None, "5"))
    assert ii.pd.isna(out["incident_report"].iloc[0]["severity_5_worst"])
    w = warnings_of(out)
    assert "llm_read_scale_digit_severity" in w
    assert "engines_disagree_severity" not in w            # the model's 5 wasn't a candidate
    assert "one_engine_only_severity" not in w             # one flag per form, not two


def test_blue_ink_kept_over_a_scale_digit_has_one_severity_flag_only():
    out, _ = run(new_form("2", "5"))                        # 904
    w = warnings_of(out)
    assert out["incident_report"].iloc[0]["severity_5_worst"] == 2
    assert "llm_read_scale_digit_severity" in w and "read_from_value_ink_severity" in w
    assert "one_engine_only_severity" not in w


def test_model_severity_that_is_not_the_scale_digit_fills_a_lost_value():
    out, _ = run(new_form(None, "3"))
    assert out["incident_report"].iloc[0]["severity_5_worst"] == 3
    assert "engines_disagree_severity" in warnings_of(out)


def test_scale_digit_guard_on_the_old_form_too():
    out, _ = run(both(FORM, FORM_LLM | {"severity": "1"}))    # '1 = MOST SEVERE': page says 4
    assert out["incident_report"].iloc[0]["severity_as_logged"] == 4
    assert "llm_read_scale_digit_severity" in warnings_of(out)


def test_scale_digit_agreeing_with_tesseract_is_kept():
    out, _ = run(both(form_text(severity_line="Severity (1 = MOST SEVERE): 1"),
                      FORM_LLM | {"severity": "1"}))
    assert out["incident_report"].iloc[0]["severity_as_logged"] == 1
    assert "llm_read_scale_digit" not in (out["documents"].iloc[0]["warnings"] or "")


@pytest.mark.parametrize("field,tess_line,llm_value,kept", [
    ("immediate_action", "area cordoned, toalbox briefing conducted",     # IR-2024-982
     "area cordoned, toolbox briefing conducted", "area cordoned, toolbox briefing conducted"),
    ("description", "weld flash bum", "weld flash burn", "weld flash burn"),
])
def test_free_text_goes_to_the_model(field, tess_line, llm_value, kept):
    label = {"immediate_action": "first aid administered, sent to clinic",
             "description": "weld flash burn"}[field]
    texts = {k: v.replace(label, tess_line) for k, v in FORM.texts.items()}
    out, _ = run(both(Extraction(ii.TESSERACT_TAG, texts=texts, signature=1),
                      FORM_LLM | {field: llm_value}, llm_sig=1))
    assert out["incident_report"].iloc[0][field] == kept
    note = next(n for n in json.loads(out["documents"].iloc[0]["ocr_notes"])
                if n["field"] == field)
    assert note["rule"] == "free_text_llm"


@pytest.mark.parametrize("tess,model,agree", [
    ("22 kW 3PH 415y SOHz", "22 kW 3PH 415V 50Hz", True),     # CNC-01: typos, same values
    ("7.5 kW 3PH 415V 56Hz", "7.5 kW 3PH 415V 50Hz", True),   # PMP-T01: Hz isn't stored
    ("15 kW 3PH 389-440V", "15 kW 3PH 380-440V", False),      # a real voltage difference
    ("22 kW 3PH 415V", "15 kW 3PH 415V", False),              # kW
    ("22 kW 3PH 415y SOHz", "22 kW", False),                  # the model's line cut short
    ("22 kW 1PH 415V", "22 kW 3PH 415V", False),              # phases
    ("22 kW 3PH", "22 kW 3PH 415V", False),                   # a voltage only one has
])
def test_power_lines_are_compared_on_kw_phases_and_voltage(tess, model, agree):
    assert ii.powers_agree(tess, model) is agree


def test_power_typos_alone_are_not_a_disagreement_and_the_clean_text_is_kept():
    """CNC-01: '22 kW 3PH 415y SOHz' x3 vs the model's '22 kW 3PH 415V 50Hz'."""
    tess = plate({"POWER": "22 kW 3PH 415y SOHz"}, {"POWER": "22 kW 3PH 415y SOHz"},
                 {"POWER": "22 kW 3PH 415y SOHz"})
    out, _ = run(both(tess, PLATE_LLM | {"power": "22 kW 3PH 415V 50Hz"}))
    p = out["nameplate"].iloc[0]
    assert (p["power_kw"], p["phases"], p["voltage_min_v"]) == (22.0, 3, 415)
    assert "engines_disagree_power" not in warnings_of(out)


@pytest.mark.parametrize("label,field,tess,llm", [
    ("MFG DATE", "mfg_date", "10/2018 a", "10/2018"),          # CNC-01, 6 Nov 2026
    ("WEIGHT", "weight", "3588 ka", "3588 kg"),                # CMP-01
    ("GREASE INTERVAL", "grease_interval", "1000 hrs", "1000 h"),
])
def test_engines_are_compared_on_the_stored_value_not_the_text(label, field, tess, llm):
    out, _ = run(both(plate({label: tess}), PLATE_LLM | {field: llm}))
    w = warnings_of(out)
    assert f"engines_disagree_{field}" not in w
    assert json.loads(out["documents"].iloc[0]["fields_raw"])[field] == llm   # cleaner text


@pytest.mark.parametrize("text,hours", [
    ("1000 h", 1000), ("1000 hrs", 1000), ("1,000 hours", 1000), ("1O00 h", 1000),
    ("500h", 500), ("590 h", 590), ("5@8 4", None),
])
def test_grease_interval_units_are_never_mangled_by_the_digit_fixes(text, hours):
    assert ii.hours_of(text) == hours


@pytest.mark.parametrize("text,kg", [
    ("3588 ka", 3588.0), ("3588 kq", 3588.0), ("9257 kg ,", 9257.0), ("2,000 lb", 907.18),
    ("1.5 t", 1500.0), ("heavy", None),
])
def test_weight_reads_ocrs_kg_misreadings(text, kg):
    assert ii.weight_of(text)[0] == kg


def test_a_real_difference_in_the_stored_value_is_still_a_disagreement():
    out, _ = run(both(plate({"MFG DATE": "10/2018"}), PLATE_LLM | {"mfg_date": "11/2018"}))
    assert "engines_disagree_mfg_date" in warnings_of(out)


def test_a_power_line_the_model_split_keeps_tesseracts_whole_line():
    """CNC-01 (6 Nov 2026): Tesseract read the power line whole, with typos; the model put
    '22 kW' under power and the rest under capacity, which the plate doesn't have."""
    tess = plate({"POWER": "22 kW 3PH 415y SOHz", "CAPACITY": None})
    out, _ = run(both(tess, PLATE_LLM | {"power": "22 kW", "capacity": "3PH 415V 50Hz"}))
    p = out["nameplate"].iloc[0]
    assert (p["power_kw"], p["phases"]) == (22.0, 3)            # phases kept
    w = warnings_of(out)
    assert "capacity_looks_like_power_line" in w and "one_engine_only_capacity" in w
    note = next(n for n in json.loads(out["documents"].iloc[0]["ocr_notes"])
                if n["field"] == "power")
    assert note["rule"] == "llm_reading_cut_short"


@pytest.mark.parametrize("power,volts", [
    ("22 kW 3PH 415V 50Hz", (415, 415)), ("15 kW 3PH 380-440V", (380, 440)),
    ("22 kW 3PH 415y SOHz", (None, None)), ("7.5 kW", (None, None)),
])
def test_a_single_voltage_is_read_as_well_as_a_range(power, volts):
    assert ii.voltages_of(power) == volts


@pytest.mark.parametrize("capacity,looks", [
    ("3PH 415V 50Hz", True), ("450 A @ 60%", False), ("2000 kg", False),
    ("30 kW FAD 88 cfm", False), (None, False),
])
def test_capacity_that_is_really_a_power_line(capacity, looks):
    assert ii.looks_like_power_line(capacity) is looks


def test_document_only_one_engine_recognised_is_flagged_not_recognised():
    out, _ = run(both(FORM, {"doc_type": "unknown"}, llm_sig=1))
    assert out["incident_report"].iloc[0]["incident_id"] == 900
    w = warnings_of(out)
    assert "not_recognised_by_llm" in w and "engine_failed_llm" not in w


def test_signature_disagreement_is_left_unknown():
    out, _ = run(both(FORM, FORM_LLM, llm_sig=0))                 # Tesseract saw ink
    assert ii.pd.isna(out["incident_report"].iloc[0]["signature_present"])
    w = warnings_of(out)
    assert "signature_unknown" in w and "engines_disagree_signature" in w
    out, _ = run(both(FORM, FORM_LLM, llm_sig=1))
    assert out["incident_report"].iloc[0]["signature_present"] == 1


def test_one_engine_failing_falls_back_to_the_other_and_says_so():
    out, _ = run(both(PLATE, llm_error="APITimeoutError: timed out"))
    assert out["nameplate"].iloc[0]["asset_tag"] == "BLAST-01"
    assert "engine_failed_llm" in warnings_of(out)
    bad = Extraction(ii.TESSERACT_TAG, error="ValueError: not an image")
    out, _ = run(ii.combine(bad, Extraction(LLM, error="APIError: 500"), "gpt-4o-mini"))
    assert out["documents"].iloc[0]["reject_reason"] == "unreadable_image"


def test_engines_seeing_different_document_types_reject_the_image():
    out, _ = run(both(PLATE, FORM_LLM))
    assert out["documents"].iloc[0]["reject_reason"] == "unknown_document_type"


def test_a_name_read_by_either_engine_is_never_stored():
    out, _ = run(both(FORM, FORM_LLM | {"person": "Seah Jia Hui"}))
    assert out["incident_report"].iloc[0]["emp_code"] == "EMP001"     # the code passes its check
    assert "Seah" not in out["incident_report"].to_string()
    out, _ = run(both(form_text(person="Seah Jia Hui"), FORM_LLM | {"person": "Seah Jia Hui"}))
    assert "Seah" not in out["incident_report"].to_string()


def test_both_engines_read_and_cached_separately(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ii, "extract_tesseract", lambda p: calls.append("t") or PLATE)
    monkeypatch.setattr(ii, "extract_llm",
                        lambda p, m: calls.append("l") or Extraction(f"llm:{m}", fields=PLATE_LLM))
    for _ in range(2):
        ex = ii.extract(tmp_path / "a.jpg", "f" * 64, "both", "gpt-4o-mini", tmp_path / "c")
        assert set(ex.parts) == {"tesseract", "llm"}
    assert calls == ["t", "l"]
    assert len(list((tmp_path / "c").glob("*.json"))) == 2


def test_llm_setup_problems_stop_the_run(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=object))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with pytest.raises(RuntimeError, match="OPENAI_BASE_URL"):
        ii.llm_client()


# ------------------------------------------------------- cache and lineage ---

def test_ocr_is_cached_by_image_hash_and_errors_are_not(tmp_path, monkeypatch):
    calls = []

    def fake(path):
        calls.append(path)
        if path.name == "bad.jpg":
            raise ValueError("not an image")
        return PLATE
    monkeypatch.setattr(ii, "extract_tesseract", fake)
    p, bad = tmp_path / "a.jpg", tmp_path / "bad.jpg"
    for _ in range(2):
        assert ii.extract(p, "f" * 64, "tesseract", "m", tmp_path / "cache").texts == PLATE.texts
        assert ii.extract(bad, "e" * 64, "tesseract", "m", tmp_path / "cache").error
    assert [c.name for c in calls] == ["a.jpg", "bad.jpg", "bad.jpg"]
    assert (tmp_path / "cache" / ".gitignore").read_text(encoding="utf-8").endswith("*\n")


def test_missing_tesseract_stops_the_run_instead_of_rejecting_every_image(monkeypatch, tmp_path):
    def no_tesseract(path):
        raise RuntimeError("Tesseract not found")
    monkeypatch.setattr(ii, "extract_tesseract", no_tesseract)
    with pytest.raises(RuntimeError):
        ii.extract(tmp_path / "a.jpg", "f" * 64, "tesseract", "m", None)


def test_folder_hash_covers_names_and_bytes_not_order(tmp_path):
    (tmp_path / "b.jpg").write_bytes(b"2")
    (tmp_path / "a.jpg").write_bytes(b"1")
    h = folder_sha256(tmp_path, ii.IMAGE_EXTS)
    assert len(h) == 64
    (tmp_path / "readme.txt").write_text("ignored", encoding="utf-8")
    assert folder_sha256(tmp_path, ii.IMAGE_EXTS) == h
    (tmp_path / "a.jpg").write_bytes(b"changed")
    assert folder_sha256(tmp_path, ii.IMAGE_EXTS) != h


def test_audit_views_cite_the_image_but_not_the_raw_text():
    """The app reads *_audit; what OCR read verbatim stays in the raw table (PDPA)."""
    sql = "\n".join(ii.ddl("img_"))
    views = [s for s in ii.ddl("img_") if "CREATE OR REPLACE VIEW" in s and "_audit AS" in s]
    assert len(views) == 2
    for v in views:
        assert "r.source_file" in v and "r.image_sha256" in v and "folder_sha256" in v
        assert "fields_raw" not in v and "ocr_text" not in v and "ocr_notes" not in v
    assert "ON DELETE CASCADE" in sql and "UNIQUE KEY uq_img_nameplates_key (`asset_tag`)" in sql


# ---------------------------------------------------------------- main ---

def test_dry_run_prints_a_summary_the_pipeline_can_read(tmp_path, monkeypatch, capsys):
    import pipeline

    (tmp_path / "IR-2024-900.jpg").write_bytes(b"form")
    (tmp_path / "plate.png").write_bytes(b"plate")
    fakes = {"IR-2024-900.jpg": FORM, "plate.png": PLATE}
    monkeypatch.setattr(ii, "extract", lambda path, sha, *a: fakes[path.name])
    monkeypatch.setattr("sys.argv", ["ingest_images.py", str(tmp_path), "--dry-run"])
    ii.main()
    out = capsys.readouterr().out.splitlines()
    assert pipeline.parse_counts(out) == {"rows_read": 2, "rows_loaded": 2, "rows_rejected": 0}
    s = pipeline.summary_lines(out)
    assert "Document types: incident_report=1, nameplate=1 (total 2)" in s
    assert "Dry run - nothing written to the database." in out


# ------------------------------------------------------------ real scans ---

# ------------------------------------------- the 12 nameplates (trap list I14-I17) ---
# Readings below are what Tesseract made of the client's plates (img_documents_raw.ocr_notes,
# 4 Nov 2026); the right values are from the photos.

def plate(gray: dict, otsu: dict | None = None, adaptive: dict | None = None) -> Extraction:
    """The BLAST-01 plate text with some lines replaced, one dict per pass: {label: value}."""
    def text(changes):
        t = PLATE_GRAY
        for label, value in changes.items():
            old = next(ln for ln in t.splitlines() if ln.startswith(label))
            t = t.replace(old, f"{label} {value}" if value is not None else "")
        return t
    passes = {"gray": gray, "otsu": otsu, "adaptive": adaptive}
    return Extraction(ii.TESSERACT_TAG, texts={k: text(v) for k, v in passes.items() if v is not None})


@pytest.mark.parametrize("read,clean", [
    ("IS0 VG32", "ISO VG32"),                          # CNC-01, all three passes
    ("1S0 VG32", "ISO VG32"), ("[S0 VG32", "ISO VG32"), ("SO VG32", "ISO VG32"),   # MILL-01
    ("ISO vVG68", "ISO VG68"),                         # WELD-01
    ("Shell Tonna S3 N68 ;", "Shell Tonna S3 M68"),    # CMP-01: the plate says M68
    ("Shell Tonna $3 M68", "Shell Tonna S3 M68"),
    ("Shell Tonna S3 M58", "Shell Tonna S3 M58"),      # WELD-02: a misread digit isn't guessed
    ("ISO VG68", "ISO VG68"), ("ESSO VG32", "ESSO VG32"),
])
def test_oil_name_misreadings_are_undone(read, clean):
    assert ii.clean_lubricant(read) == clean


def test_oil_spellings_group_in_the_vote_so_the_majority_wins():
    """WELD-01: 'ISO vVG68' / 'IS0 VG68' / 'ISO VG68' were three different readings, and the
    gray one (wrong) won the tie. Cleaned first, all three agree."""
    ex = plate({"LUBRICANT": "ISO vVG68"}, {"LUBRICANT": "IS0 VG68"}, {"LUBRICANT": "ISO VG68"})
    out, _ = run(ex)
    assert out["nameplate"].iloc[0]["lubricant"] == "ISO VG68"
    assert "lubricant" not in (out["documents"].iloc[0]["warnings"] or "")


def test_a_grade_that_does_not_exist_is_still_flagged():
    out, _ = run(plate({"LUBRICANT": "Shell Tonna S3 M58"}))       # WELD-02 (plate: M68)
    assert "failed_format_lubricant" in out["documents"].iloc[0]["warnings"]


def test_no_two_readings_agreeing_is_flagged_as_no_majority():
    """CRN-02: '509 h' / '566 h' / '500 h' -> 509 was stored; the plate says 500 h."""
    ex = plate({"GREASE INTERVAL": "509 h"}, {"GREASE INTERVAL": "566 h"},
               {"GREASE INTERVAL": "500 h"})
    out, _ = run(ex)
    warnings = out["documents"].iloc[0]["warnings"].split(",")
    assert "ocr_no_majority_grease_interval" in warnings
    assert "ocr_disagreed_grease_interval" in warnings


def test_two_readings_agreeing_is_not_no_majority():
    out, _ = run(PLATE)                                    # 1000 h / 1660 h / 1000 h
    warnings = out["documents"].iloc[0]["warnings"].split(",")
    assert "ocr_disagreed_grease_interval" in warnings
    assert "ocr_no_majority_grease_interval" not in warnings


@pytest.mark.parametrize("value,ok", [
    ("06/2020", True), ("10/2018 a", True), ("11/2021 .", True), ("3/2014", True),
    ("06/2820", False), ("62/2016", False), ("13/2020", False), ("00/2020", False),
    ("06/1890", False), ("2016", False),
])
def test_manufacture_date_must_be_a_real_month_and_year(value, ok):
    assert ii.plausible_mfg_date(value) is ok


def test_impossible_dates_lose_the_vote():
    """PMP-T01: '06/2820' (gray) beat '06/2020' (otsu) on a tie. CRN-01: '62/2016' twice beat
    '02/2016', then failed cleaning, so the month was lost."""
    out, _ = run(plate({"MFG DATE": "06/2820"}, {"MFG DATE": "06/2020"}))
    assert out["nameplate"].iloc[0]["mfg_month"] == "2020-06"
    out, _ = run(plate({"MFG DATE": "62/2016"}, {"MFG DATE": "62/2016"}, {"MFG DATE": "02/2016"}))
    assert out["nameplate"].iloc[0]["mfg_month"] == "2016-02"


@pytest.mark.parametrize("lines,maker", [
    (["@ @", "MODEL CXT", "SERIAL NO KO4778956"], None),          # CRN-01: not 'MODEL CXT'
    (["KONECRANES", "MODEL CXT"], "KONECRANES"),
    (["| BRIDGEPORT .", "MODEL GX-1000"], "BRIDGEPORT"),          # junk around the bold name
    (["@ @", "Colchester", "MODEL Triumph"], None),               # not mostly capitals
    (["DMG", "MODEL Mori DMU-50"], "DMG"),                        # the plate splits DMG / Mori
    (["CUSTOM", "MODEL SG-Fab"], "CUSTOM"),
])
def test_manufacturer_is_never_a_label_line(lines, maker):
    assert ii.plate_maker(lines) == maker


SCANS = ROOT / "data" / "images"


@pytest.mark.skipif(shutil.which("tesseract") is None or not (SCANS / "IR-2024-900.jpg").is_file()
                    or not (SCANS / "nameplate_BLAST-01.jpg").is_file(),
                    reason="needs Tesseract and the sample scans in data/images/")
def test_the_real_scans_read_correctly():
    from lineage import file_sha256

    reads = [ImageRead(n, file_sha256(str(SCANS / n)), ii.extract_tesseract(SCANS / n))
             for n in ("IR-2024-900.jpg", "nameplate_BLAST-01.jpg")]
    out = ii.process(reads, Counter())
    r, p = out["incident_report"].iloc[0], out["nameplate"].iloc[0]
    assert (r["incident_id"], str(r["incident_date"]), r["machine_id"], r["emp_code"]) == \
        (900, "2024-07-01", "LTH-01", "EMP001")
    assert (r["severity_scale"], r["severity_5_worst"], r["signature_present"]) == \
        ("1_worst", 2, 1)
    assert (p["asset_tag"], p["serial_no"], p["grease_interval_h"], p["lubricant"]) == \
        ("BLAST-01", "CU6744884", 1000, "Shell Tonna S3 M68")


@pytest.mark.skipif(shutil.which("tesseract") is None or not (SCANS / "IR-2025-904.jpg").is_file()
                    or not (SCANS / "IR-2025-906.jpg").is_file(),
                    reason="needs Tesseract and the scans in data/images/")
def test_the_real_new_forms_give_their_severity_from_the_value_ink():
    """I12: the CMMS logs 904 as 2 and 906 as 3; the page text loses both."""
    assert ii.severity_from_ink(ii.read_image(SCANS / "IR-2025-904.jpg")) == "2"
    assert ii.severity_from_ink(ii.read_image(SCANS / "IR-2025-906.jpg")) == "3"


# ------------------------------------------------- photos for the app (PDPA) ---

def _jpg(path, color=(200, 200, 200), size=(120, 200)):
    import cv2
    import numpy as np
    img = np.full((*size, 3), color, np.uint8)
    cv2.imwrite(str(path), img)
    return path


def _docs(*rows):
    return ii.pd.DataFrame([{"raw_id": i + 1, "source_file": f, "image_sha256": f"{i:064x}",
                             "doc_type": t, "status": st, "warnings": None}
                            for i, (f, t, st) in enumerate(rows)])


def test_redaction_covers_the_boxes_and_nothing_else():
    import numpy as np
    img = np.full((100, 200, 3), 30, np.uint8)
    out = ii.redact(img, [(50, 10, 200, 40)])
    assert (out[12:38, 185:198] == ii.REDACT_FILL).all()            # covered (clear of the label)
    assert (out[60:100] == 30).all() and (out[:, :45] == 30).all()  # the rest untouched
    assert (img == 30).all()                                        # the input isn't changed


def test_nameplates_are_stored_as_they_are_and_forms_only_redacted(tmp_path):
    import hashlib
    _jpg(tmp_path / "plate.jpg")
    _jpg(tmp_path / "form.jpg", color=(20, 20, 20))
    docs = _docs(("plate.jpg", "nameplate", "loaded"), ("form.jpg", "incident_report", "loaded"),
                 ("cat.jpg", "unknown", "rejected"))
    stats = Counter()
    rows, skipped = ii.image_files(tmp_path, docs, stats,
                                   boxes_fn=lambda p, sha, c: ([(0, 0, 200, 60)], None))
    by = rows.set_index("variant")
    plate = by.loc["original"]
    assert plate["image_bytes"] == (tmp_path / "plate.jpg").read_bytes()   # byte for byte
    assert plate["bytes_sha256"] == hashlib.sha256(plate["image_bytes"]).hexdigest()
    form = by.loc["redacted"]
    assert form["image_bytes"] != (tmp_path / "form.jpg").read_bytes()
    assert form["bytes_sha256"] == hashlib.sha256(form["image_bytes"]).hexdigest()
    assert len(rows) == 2 and not skipped                                  # rejected: no photo
    assert stats["files:stored:original"] == stats["files:stored:redacted"] == 1


def test_a_form_whose_labels_cant_be_found_is_not_stored_at_all(tmp_path):
    _jpg(tmp_path / "form.jpg")
    docs = _docs(("form.jpg", "incident_report", "loaded"))
    stats = Counter()
    rows, skipped = ii.image_files(tmp_path, docs, stats,
                                   boxes_fn=lambda p, sha, c: (None, "labels_not_found"))
    assert len(rows) == 0 and skipped == {1: "labels_not_found"}
    assert stats["files:not_stored:labels_not_found"] == 1
    flagged = ii.flag_unstored(docs, skipped)
    assert flagged.loc[0, "warnings"] == "form_image_not_stored"


def test_redacted_forms_can_be_written_out_to_check_by_eye(tmp_path):
    _jpg(tmp_path / "IR-2024-900.jpg")
    docs = _docs(("IR-2024-900.jpg", "incident_report", "loaded"))
    ii.image_files(tmp_path, docs, Counter(), preview_dir=tmp_path / "preview",
                   boxes_fn=lambda p, sha, c: ([(0, 0, 50, 50)], None))
    assert (tmp_path / "preview" / "IR-2024-900_redacted.jpg").is_file()


def test_the_public_view_offers_nameplates_and_redacted_forms_only():
    sql = "\n".join(ii.ddl("img_"))
    view = next(s for s in ii.ddl("img_") if "VIEW img_files_public" in s)
    assert "doc_type = 'nameplate' AND f.variant = 'original'" in view
    assert "doc_type = 'incident_report' AND f.variant = 'redacted'" in view
    assert "status = 'loaded'" in view
    assert "image_bytes" not in "\n".join(s for s in ii.ddl("img_") if "_audit AS" in s)
    assert "PRIMARY KEY (image_sha256, variant)" in sql


@pytest.mark.skipif(shutil.which("tesseract") is None or not (SCANS / "IR-2024-900.jpg").is_file(),
                    reason="needs Tesseract and the sample scan")
def test_the_real_form_redacted_shows_no_employee_code_and_keeps_the_rest():
    import re
    import pytesseract
    img = ii.read_image(SCANS / "IR-2024-900.jpg")
    boxes = ii.redaction_boxes(img)
    assert boxes is not None and len(boxes) == 2
    text = pytesseract.image_to_string(ii.redact(img, boxes), config="--psm 6")
    assert not re.search(r"EMP\s*[0-9O]{2,}", text, re.I)          # the code is gone
    assert "weld flash burn" in text and "IR-2024-900" in text.replace("9OO", "900")


def _ink_left_below(img, boxes) -> int:
    """Blue (value) ink pixels in the 120 px under the signature box, across its width."""
    import numpy as np
    x0, y0, x1, y1 = boxes[1]
    band = img[y1: y1 + 120, x0: x1].astype(int)
    if band.size == 0:
        return 0
    b, g, r = band[..., 0], band[..., 1], band[..., 2]
    return int(((b - r > 40) & (b + g + r < 600)).sum())


FORMS = sorted(SCANS.glob("IR-*.jpg")) if SCANS.is_dir() else []


@pytest.mark.skipif(shutil.which("tesseract") is None or not FORMS,
                    reason="needs Tesseract and the scanned forms in data/images/")
@pytest.mark.parametrize("form", FORMS, ids=lambda p: p.stem)
def test_no_signature_ink_is_left_below_the_box_on_any_real_form(form):
    """6 Nov 2026: on 917, 918, 955 and 957 the signature's tail showed under the box."""
    img = ii.read_image(form)
    boxes = ii.redaction_boxes(img)
    assert boxes is not None, "labels not found: this form would not be stored"
    assert _ink_left_below(ii.redact(img, boxes), boxes) < 20



# ------------------------------------------------------------- signatures ---

def test_only_blue_ink_counts_as_a_signature():
    import numpy as np
    paper = np.full((60, 200, 3), 235, np.uint8)
    assert ii.blue_ink(paper) == 0
    stain = paper.copy(); stain[10:40, 20:60] = (170, 200, 215)            # beige coffee ring
    grey = paper.copy(); grey[20:24, 10:190] = (95, 98, 100)                # grey label print
    assert ii.blue_ink(stain) == 0 and ii.blue_ink(grey) == 0
    ink = paper.copy(); ink[30:33, 20:120] = (125, 60, 40)                  # navy pen stroke
    assert ii.blue_ink(ink) >= ii.SIGNATURE_MIN_INK


SIGNED_BY_EYE = ["IR-2024-917", "IR-2025-918", "IR-2024-936", "IR-2026-955"]   # 7 Nov 2026


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs Tesseract")
@pytest.mark.parametrize("name", SIGNED_BY_EYE)
def test_forms_checked_by_eye_as_signed_read_as_signed(name):
    """I23: Tesseract read these four as unsigned; the model said signed; checked by eye,
    all four are signed. The signature is mostly below its label."""
    path = SCANS / f"{name}.jpg"
    if not path.is_file():
        pytest.skip(f"{path.name} not in data/images/")
    img = ii.read_image(path)
    assert ii.signature_ink(ii.preprocess_variants(img)["otsu"], img) == 1


@pytest.mark.skipif(shutil.which("tesseract") is None or not FORMS,
                    reason="needs Tesseract and the scanned forms in data/images/")
@pytest.mark.parametrize("form", FORMS, ids=lambda p: p.stem)
def test_the_signature_label_is_found_on_every_real_form(form):
    img = ii.read_image(form)
    assert ii.signature_ink(ii.preprocess_variants(img)["otsu"], img) in (0, 1)