"""Tests for scripts/ingest_images.py. No database, no Tesseract and no LLM needed:
the OCR text below is what Tesseract actually read from the two sample scans (with its
mistakes), passed in as an Extraction. One test reads the real scans if Tesseract and
data/images/ are there, and is skipped otherwise."""
from __future__ import annotations

import json
import shutil
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
    assert "root_cause_in_attachment" in warnings and "date_assumed_day_first" in warnings
    assert "form_scale_contradicts_date_rule" not in warnings
    assert stats["incident_reports:scale:1_worst"] == 1


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
    views = [s for s in ii.ddl("img_") if "CREATE OR REPLACE VIEW" in s]
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