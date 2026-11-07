"""Tests for scripts/agent_tools.py (the agent's tools). No MySQL needed.

The tools run their real SQL on an in-memory SQLite database whose tables have the names
and columns of the views and audit views agent_ro may read. The rows are a small,
hand-made sample shaped like the client's data (machine names and some values are
illustrative); the SOPs are the client's three files, loaded through ingest_sops.process.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import numpy as np
import pandas as pd
import pytest

import agent_tools as at
import ingest_sops as isops

# ------------------------------------------------------------------ fixture ---

MACHINES = [  # machine_id, machine_name, model
    ("BLAST-01", "Blasting Chamber", "Custom SG-Fab"), ("CMP-01", "Air Compressor", "Atlas Copco GA30"),
    ("CNC-01", "CNC Lathe 1", "Haas ST-20"), ("CNC-02", "CNC Lathe 2", "Haas ST-20"),
    ("CNC-03", "CNC Machining Centre", "DMG Mori DMU-50"), ("CRN-01", "Overhead Crane 10T", "Konecranes CXT"),
    ("CRN-02", "Jib Crane 2T", "Demag KBK"), ("LTH-01", "Manual Lathe", "Colchester Triumph"),
    ("MILL-01", "Milling Machine", "Bridgeport GX-1000"), ("PMP-T01", "Test Pump Rig", "KSB Etanorm"),
    ("WELD-01", "MIG Welder", "Lincoln R450"), ("WELD-02", "TIG Welder", "Miller XMT-350"),
]
SENSORED = ["CNC-01", "CNC-02", "PMP-T01"]
CMMS = "jurongmarine_cmms.sql"
SHA = "a" * 64

INCIDENTS = [  # id, date, machine, description, logged, severity_5_worst, lost_days
    (917, "2024-01-24", "CRN-01", "near miss: sling snapped during lift", 4, 2, 0),
    (900, "2024-07-01", "LTH-01", "weld flash burn", 4, 2, 0),
    (914, "2025-01-03", "CRN-02", "weld flash burn", 5, 1, 0),
    (919, "2025-03-10", "CNC-02", "finger caught in chuck (glove)", 1, 5, 5),
    (982, "2025-05-20", "CNC-02", "falling object from crane path - helmet cracked", 4, 2, 14),
    (907, "2025-07-04", "LTH-01", "heat stress - confined space", 2, None, 1),
    (906, "2025-07-11", "CNC-03", "slip on hydraulic oil", 3, 3, 0),
    (904, "2025-12-29", "CNC-02", "slip on hydraulic oil", 2, 2, 1),
    (933, "2026-02-10", "WELD-02", "heat stress - confined space", 5, 5, 0),
    (944, "2026-04-28", "CRN-01", "near miss: sling snapped during lift", 4, 4, 5),
]

SOP_FILES = {
    "SOP-CONFINED-03.txt": "JME SOP-CONFINED-03 — CONFINED SPACE ENTRY (rev 2, Sep 2025)\n"
        "Applies to: blasting chamber (BLAST-01), ballast tanks on client vessels, test pump sump.\n"
        "- Entry permit required from WSH officer. Valid 8 hours max.\n"
        "- Gas test before entry AND every 2 hours: O2 19.5-23.5%, LEL <10%, H2S <10ppm, CO <30ppm.\n"
        "- Attendant stationed outside at all times with communication.\n"
        "- MOM notification required for vessel tank entry (Shipyard regs).",
    "SOP-CRANE-02.txt": "JME SOP-CRANE-02 — OVERHEAD CRANE LIFTING (rev 6, Mar 2026)\n"
        "- Only certified crane operators (list with HR) may operate CRN-01/CRN-02.\n"
        "- Inspect slings/shackles before EVERY lift. Discard slings with >10% wear, kinks, or broken strands.\n"
        "- Max load: CRN-01 = 10,000 kg, CRN-02 = 2,000 kg. NEVER bypass limit switch.\n"
        "- Tandem lifts require lifting plan signed by PE (Professional Engineer).\n"
        "- No person under suspended load. Use tag lines for long items.\n"
        "- After the Aug 2025 near-miss (sling snapped), ALL slings >2 years old were scrapped. Check purchase date on sling tag.",
    "SOP-LOTO-01.txt": "JME SOP-LOTO-01 — LOCKOUT/TAGOUT PROCEDURE (rev 4, Jan 2026)\n"
        "1. Identify all energy sources (electrical, hydraulic, pneumatic, gravity).\n"
        "2. Notify affected personnel and shift supervisor.\n"
        "3. Shut down machine using normal stop procedure.\n"
        "4. Isolate at main breaker; apply personal lock + tag. One lock per person working.\n"
        "5. Verify zero energy: attempt restart, test with multimeter (electricians only).\n"
        "6. For CNC machines: also release residual hydraulic pressure via manual valve (see machine manual).\n"
        "7. After work: ensure all guards restored, all personnel clear, remove locks (owner only), notify supervisor.\n"
        "VIOLATION = immediate stop-work + incident report. No exceptions, including 'quick jobs'.",
}


def _readings() -> pd.DataFrame:
    """One reading per machine every 12 minutes, Jan-Jun 2026, every 50th missing. CNC-02's
    vibration climbs from April (as in the client's file); the others stay flat."""
    rng = np.random.default_rng(7)
    ts = pd.date_range("2026-01-01", "2026-06-30 23:48", freq="12min")
    out, line = [], 2
    for m in SENSORED:
        days = (ts - ts[0]).days.to_numpy()
        vib = 2.3 + rng.normal(0, 0.25, len(ts))
        temp = 52 + rng.normal(0, 6, len(ts))
        if m == "CNC-02":
            ramp = np.clip(days - 90, 0, None) / 90
            vib += 2.1 * ramp
            temp += 4 * ramp
        keep = np.arange(len(ts)) % 50 != 0
        for t, v, c in zip(ts[keep], vib[keep], temp[keep]):
            out.append((m, t.strftime("%Y-%m-%d %H:%M:%S"), round(v, 2), round(c, 2), line))
            line += 1
    df = pd.DataFrame(out, columns=["machine_id", "sampled_at", "vib_rms_mms", "temp_c", "source_line"])
    return df.assign(source_file="vibration_temp_2026H1.csv", file_sha256=SHA)


def build(conn: sqlite3.Connection) -> None:
    def put(name, rows, cols=None):
        (rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows, columns=cols)
         ).to_sql(name, conn, index=False)

    readings = _readings()
    put("vibration_temp_audit", readings)
    per = readings.groupby("machine_id")["sampled_at"].agg(["count", "min", "max"])
    put("machine_overview", [
        {"machine_id": m, "machine_name": n, "model": mo, "year_installed": 2015, "work_orders": 0,
         "breakdowns": 0, "open_work_orders": 0, "incidents": 0, "spare_parts": 0,
         "spares_below_min": 0,
         "sensor_readings": int(per.loc[m, "count"]) if m in per.index else 0,
         "first_reading": per.loc[m, "min"] if m in per.index else None,
         "last_reading": per.loc[m, "max"] if m in per.index else None}
        for m, n, mo in MACHINES])

    names = {m: n for m, n, _ in MACHINES}
    put("incident_detail", [
        {"incident_id": i, "incident_date": d, "machine_id": m, "machine_name": names[m],
         "description": desc, "severity_5_worst": s5,
         "severity_scale": "ambiguous" if s5 is None else ("1_worst" if d < "2025-07-01" else "5_worst"),
         "lost_days": ld, "reporting": "reported same day", "emp_code": "EMP009", "trade": "Fitter",
         "shift": "night", "machine_known": 1, "employee_known": 1, "raw_id": 1000 + i}
        for i, d, m, desc, logged, s5, ld in INCIDENTS])
    put("cmms_safety_incidents_audit", [
        {"incident_id": i, "raw_id": 1000 + i, "severity_as_logged": logged, "source_file": CMMS,
         "source_line": 300 + n, "file_sha256": SHA, "raw_tuple": "(...)"}
        for n, (i, d, m, desc, logged, s5, ld) in enumerate(INCIDENTS)])
    put("img_incident_reports_audit", [
        {"incident_id": 914, "report_no": "IR-2025-914", "source_file": "IR-2025-914.jpg",
         "image_sha256": "b" * 64, "severity_scale": "1_worst", "immediate_action": "first aid",
         "root_cause": "see attached", "signature_present": 1, "warnings": "ocr_disagreed_location",
         "emp_code": "EMP029"},
        {"incident_id": 904, "report_no": "IR-2025-904", "source_file": "IR-2025-904.jpg",
         "image_sha256": "f" * 64, "severity_scale": "5_worst", "immediate_action": "first aid",
         "root_cause": "N/A near miss", "signature_present": 1,
         "warnings": "llm_read_scale_digit_severity,checked_by_eye_severity", "emp_code": "EMP009"}])
    put("incident_report_vs_cmms", [
        {"incident_id": 914, "report_no": "IR-2025-914", "field": f, "form_value": fv,
         "logged_value": lv, "agrees": ag}
        for f, fv, lv, ag in [("machine_id", "CRN-02", "CRN-02", 1),
                              ("description", "weld flash bur", "weld flash burn", 0),
                              ("severity_5_worst", None, "1", None)]])

    plate_cols = {f: None for f in at.NAMEPLATE_FIELDS}
    plates = {  # machine -> (fields, warnings); MILL-01 has no photo in this sample
        "CNC-02": ({"lubricant": "ISO VG32", "grease_interval_h": 500, "plate_model": "ST-20"}, None),
        "CNC-03": ({"lubricant": "ISO VG32", "grease_interval_h": 590, "weight_kg": 8892},
                   "ocr_disagreed_grease_interval,ocr_no_majority_grease_interval,"
                   "ocr_disagreed_weight,ocr_no_majority_weight"),
        "WELD-02": ({"lubricant": "Shell Tonna S3 M58", "grease_interval_h": 2000,
                     "capacity_raw": "30 kW FAD 88 cfm"},
                    "failed_format_lubricant,lubricant_without_grade"),
        "BLAST-01": ({"lubricant": "Shell Tonna S3 M68", "grease_interval_h": 1000,
                      "capacity_raw": "450 A @ 60%"},
                     "duty_cycle_rating_on_non_welder,placeholder_manufacturer,ocr_disagreed_lubricant"),
        # Loaded with --engine both (6 Nov 2026): the model's 500 h and 2000 kg won, and
        # match the photo checked by eye.
        "CRN-02": ({"lubricant": "ISO VG68", "grease_interval_h": 500, "capacity_raw": "2000 kg"},
                   "engines_disagree_grease_interval,engines_disagree_capacity"),
        "CNC-01": ({"lubricant": "ISO VG32", "grease_interval_h": 500, "power_kw": 22.0,
                    "phases": 3, "voltage_min_v": 415, "voltage_max_v": 415,
                    "capacity_raw": "250 mm swing", "weight_kg": 9257.0},
                   "engines_disagree_power,one_engine_only_capacity"),
    }
    both = {"CRN-02", "CNC-01"}
    rows, audit = [], []
    for n, (m, name, model) in enumerate(MACHINES):
        f, w = plates.get(m, ({}, None))
        has = m != "MILL-01"
        rows.append({"machine_id": m, "machine_name": name, "logged_model": model,
                     "year_installed": 2015, "has_nameplate": int(has), **plate_cols, **f,
                     "model_agrees": 1 if has else None, "made_by_install_year": 1 if has else None,
                     "raw_id": 5000 + n if has else None})
        if has:
            audit.append({"raw_id": 5000 + n, "asset_tag": m, "source_file": f"nameplate_{m}.jpg",
                          "image_sha256": "c" * 64, "warnings": w,
                          "ocr_engine": "both:tesseract-v2+llm-v2:gpt-4o-mini" if m in both
                          else "tesseract-v2"})
    put("machine_nameplate", rows)
    put("img_nameplates_audit", audit)
    import hashlib
    good, bad = b"\xff\xd8 a nameplate photo", b"\xff\xd8 changed after ingest"
    put("img_files_public", [
        {"image_sha256": "c" * 64, "variant": "original", "mime": "image/jpeg",
         "bytes_sha256": hashlib.sha256(good).hexdigest(), "width": 800, "height": 600,
         "image_bytes": good, "source_file": "nameplate_CNC-02.jpg", "doc_type": "nameplate"},
        {"image_sha256": "d" * 64, "variant": "redacted", "mime": "image/jpeg",
         "bytes_sha256": hashlib.sha256(good).hexdigest(), "width": 800, "height": 600,
         "image_bytes": bad, "source_file": "IR-2025-914.jpg", "doc_type": "incident_report"}])

    spares = [  # part, description, qty, min, size_mm, lead (min, max, local), listed_for, row
        ("BRG-6310", "Bearing 6310 (spindle)", 2, 4, None, (14, 21, 0), ["CNC-02"], 5),
        ("HYD-OIL-46", "Hydraulic oil ISO VG46 (20 L drum)", 9, 4, None, (None, None, 1), ["ALL"], 12),
        ("BELT-5V-850", "V-belt 5V 85 in", 3, 2, 2159.0, (7, 7, 0), ["CMP-01"], 6),
        ("WAY-OIL-32", "Slideway oil ISO VG32 (4 L)", 5, 2, None, (None, None, 1), ["CNC-03"], 14),
    ]
    put("machine_spares", [
        {"machine_id": m, "machine_name": names[m], "part_no": p, "description": d, "qty": q,
         "min_qty": mn, "below_min": int(q < mn), "unit_cost": 48.0, "cost_per": "each",
         "size_mm": sz, "lead_time_min_days": lt[0], "lead_time_max_days": lt[1],
         "is_local_supply": lt[2], "location": "S1-A2", "listed_for": lf[0]}
        for p, d, q, mn, sz, lt, lf, row in spares
        for m in (names if lf == ["ALL"] else lf)])
    put("critical_spares_audit", [
        {"part_no": p, "source_file": "critical_spares.xlsx", "source_row": row, "source_rev": "11",
         "file_sha256": SHA, "lead_time_raw": "2-3 weeks" if p == "BRG-6310" else "local",
         "unit_cost_raw": "$48", "qty_raw": str(q)}
        for p, d, q, mn, sz, lt, lf, row in spares])

    wos = [  # wo_id, date, machine, type, description, hours, cost, status
        (10727, "2025-02-01", "CNC-02", "PM", "quarterly PM", 2.0, 120.0, "CLOSED"),
        (10728, "2025-02-01", "CNC-02", "PM", "quarterly PM", 2.0, 120.0, "CLOSED"),
        (11529, "2026-05-15", "CNC-02", "BREAKDOWN", "spindle vibration high", 6.5, 500.0, "CLOSED"),
        (11591, "2026-06-19", "CNC-02", "CORRECTIVE", "bearing noise - gearbox", 3.0, 941.13, "OPEN"),
        (11570, "2026-06-10", "CRN-01", "CORRECTIVE", "crane limit switch fault", 1.0, 80.0, "AWAITING PARTS"),
    ]
    put("work_order_detail", [
        {"wo_id": w, "wo_date": d, "machine_id": m, "machine_name": names[m], "model": "", "wo_type": t,
         "description": de, "duration_hrs": h, "cost_sgd": c, "status": s, "emp_code": "EMP009",
         "trade": "Fitter", "shift": "day", "machine_known": 1, "employee_known": 1, "raw_id": 2000 + n}
        for n, (w, d, m, t, de, h, c, s) in enumerate(wos)])
    put("cmms_work_orders_audit", [
        {"wo_id": w, "raw_id": 2000 + n, "source_file": CMMS, "source_line": 900 + n, "file_sha256": SHA}
        for n, (w, *_) in enumerate(wos)])

    files = [isops.SopFile(n, t.encode("utf-8")) for n, t in sorted(SOP_FILES.items())]
    out = isops.process(files, run_id=1, machines={m: n for m, n, _ in MACHINES})
    raw = out["raw"][["raw_id", "source_folder", "source_file", "file_sha256"]]
    put("sops", out["sops"])
    put("sop_machines", out["links"])
    put("sops_audit", out["sops"].drop(columns=["full_text", "search_text"]).merge(raw, on="raw_id"))
    steps = out["steps"].merge(out["sops"][["doc_no", "revision", "title", "rev_label", "is_current",
                                            "raw_id"]], on=["doc_no", "revision"]).merge(raw, on="raw_id")
    put("sop_steps_audit", steps)
    ms = pd.DataFrame(MACHINES, columns=["machine_id", "machine_name", "model"]).merge(
        out["links"].merge(out["sops"], on=["doc_no", "revision"]), on="machine_id", how="left")
    ms["has_sop"] = ms["doc_no"].notna().astype(int)
    put("machine_sops", ms.rename(columns={"how": "linked_by", "evidence": "sop_words"})[
        ["machine_id", "machine_name", "has_sop", "doc_no", "revision", "title", "rev_label",
         "rev_month", "step_count", "linked_by", "sop_words", "raw_id"]])


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    build(c)
    yield c
    c.close()


@pytest.fixture
def db(conn):
    return at.sqlite_db(conn)


def call(db, name, **args):
    r = at.run_tool(db, name, args)
    json.dumps(r.to_dict())            # every result must be JSON-safe
    return r


def known():
    return [{"machine_id": m, "machine_name": n, "model": mo} for m, n, mo in MACHINES]


# ------------------------------------------------------------ the registry ---

def test_every_tool_has_an_llm_schema():
    schemas = {s["function"]["name"]: s for s in at.tool_schemas()}
    assert set(schemas) == set(at.TOOLS) and len(schemas) == 12
    for s in schemas.values():
        assert s["type"] == "function" and s["function"]["description"]
        assert s["function"]["parameters"]["type"] == "object"


def test_severity_scale_is_offered_as_a_choice_not_free_text():
    props = {s["function"]["name"]: s["function"]["parameters"] for s in at.tool_schemas()}
    scale = json.dumps(props["count_incidents"])
    assert "5_is_worst" in scale and "1_is_worst" in scale


def test_results_for_the_model_carry_no_sql_but_the_trace_does(db):
    r = call(db, "list_machines")
    assert r.queries and "SELECT" in r.queries[0]["sql"]
    assert "SELECT" not in r.for_model()


# -------------------------------------------------------------- machines ---

@pytest.mark.parametrize("said,mid", [
    ("CNC-02", "CNC-02"), ("cnc 2", "CNC-02"), ("CNC2", "CNC-02"), ("cnc-02", "CNC-02"),
    ("pmp-t1", "PMP-T01"), ("CNC-02 spindle", "CNC-02"), ("the compressor", "CMP-01"),
    ("air compressor", "CMP-01"), ("crane 2", "CRN-02"), ("overhead crane", "CRN-01"),
    ("sandblasting booth", "BLAST-01"), ("test pump sump", "PMP-T01"),
])
def test_machines_are_found_however_they_are_written(said, mid):
    assert at.resolve_machine(said, known()) == mid


@pytest.mark.parametrize("said", ["crane", "the lathe", "welder"])
def test_two_candidates_means_ask_never_pick(said):
    with pytest.raises(at.ToolError) as e:
        at.resolve_machine(said, known())
    assert e.value.code == "ambiguous_machine" and "Ask which one" in e.value.message


def test_an_unknown_machine_is_unknown_not_the_nearest(db):
    r = call(db, "get_nameplate", machine="forklift")
    assert not r.ok and r.error["code"] == "unknown_machine"
    assert "CNC-02" in r.error["message"]                 # lists what does exist


# ---------------------------------------------- severity: the kill-shot ---

YEAR_2025 = {"date_from": "2025-01-01", "date_to": "2025-12-31"}


@pytest.mark.parametrize("args", [
    {"at_least_as_severe_as": 1},                          # "how many sev-1 incidents?"
    {"exact_severity": 1},                                 # "severity 1 incidents"
    {"exact_severity": 5, "description_contains": "chuck"},
    {"at_least_as_severe_as": 4, "machine": "CNC-02"},
])
@pytest.mark.parametrize("tool", ["count_incidents", "list_incidents"])
def test_a_severity_number_without_its_scale_is_asked_about_not_guessed(db, tool, args):
    r = call(db, tool, **YEAR_2025, **args)
    assert not r.ok and r.error["code"] == "needs_clarification"
    assert "5 = most severe" in r.error["message"] and "1 = most severe" in r.error["message"]


@pytest.mark.parametrize("args", [
    {"exact_severity": 5, "severity_scale": "5_is_worst"},       # "worst, current scale"
    {"exact_severity": 1, "severity_scale": "1_is_worst"},       # "sev-1, old scale"
    {"at_least_as_severe_as": 5, "severity_scale": "5_is_worst"},
    {"at_least_as_severe_as": 1, "severity_scale": "1_is_worst"},
])
def test_the_same_question_on_either_scale_gives_the_same_incidents(db, args):
    """2025 straddles the switch: 919 (logged 1, old scale) is the only most-severe one."""
    r = call(db, "count_incidents", **YEAR_2025, **args)
    assert r.ok and r.data["count"] == 1 and r.data["incident_ids"] == [919]


def test_severity_is_never_counted_on_the_logged_numbers(db):
    """A naive 'logged >= 4' finds 917, 900, 914, 982, 944, 933 (six), mixing scales.
    On one scale only 919 (5), 933 (5) and 944 (4) are severity 4 or worse."""
    r = call(db, "count_incidents", at_least_as_severe_as=4, severity_scale="5_is_worst")
    assert sorted(r.data["incident_ids"]) == [919, 933, 944]
    r_old = call(db, "count_incidents", at_least_as_severe_as=2, severity_scale="1_is_worst")
    assert sorted(r_old.data["incident_ids"]) == [919, 933, 944]


def test_the_july_2025_incident_is_reported_apart_never_counted(db):
    r = call(db, "count_incidents", at_least_as_severe_as=1, severity_scale="5_is_worst")
    assert 907 not in r.data["incident_ids"]
    assert [u["incident_id"] for u in r.data["unplaced"]] == [907]
    assert any("July 2025" in n for n in r.notes)


def test_without_a_severity_filter_every_incident_counts(db):
    assert call(db, "count_incidents").data["count"] == len(INCIDENTS)


def test_grouping_by_scale_period_shows_the_switch(db):
    r = call(db, "count_incidents", group_by="scale_period")
    groups = {g["key"]: g["count"] for g in r.data["groups"]}
    assert sum(groups.values()) == len(INCIDENTS)
    assert groups["before 1 Jul 2025 (logged 1 = worst, converted)"] == 5


def test_every_count_carries_the_severity_rule_and_its_lines(db):
    r = call(db, "count_incidents", machine="CNC-02")
    assert "6 - logged value" in r.data["severity_rule"]
    src = r.sources[0]
    assert src["file"] == CMMS and src["locator"].startswith("lines ")


def test_incidents_are_listed_by_date_with_both_severities_and_no_people(db):
    r = call(db, "list_incidents", machine="CNC-02", newest_first=False)
    ids = [x["incident_id"] for x in r.data["incidents"]]
    assert ids == [919, 982, 904]                         # by date; ids aren't in date order
    first = r.data["incidents"][0]
    assert first["severity_5_worst"] == 5
    assert first["severity_as_logged"] == "1 on the old (1 = most severe) scale"
    text = json.dumps(r.to_dict())
    assert "EMP009" not in text and "Fitter" not in text  # no employee codes or trades


def test_description_search_treats_wildcards_as_text(db):
    assert call(db, "count_incidents", description_contains="100%").data["count"] == 0
    assert call(db, "count_incidents", description_contains="SLING").data["count"] == 2


# --------------------------------------------------------- one incident ---

def test_an_incident_shows_where_the_form_and_cmms_disagree(db):
    r = call(db, "get_incident", incident_id=914)
    assert r.ok and r.data["scanned_form"]["report_no"] == "IR-2025-914"
    assert any("disagree on: description" in n for n in r.notes)
    assert any("5-Why" in n for n in r.notes)
    assert {s["kind"] for s in r.sources} == {"file", "image"}
    assert "EMP029" not in json.dumps(r.to_dict())


def test_an_incident_without_a_form_says_so(db):
    r = call(db, "get_incident", incident_id=907)
    assert any("no scanned form" in n for n in r.notes)
    assert any("switch month" in n for n in r.notes)


def test_an_incident_that_doesnt_exist(db):
    r = call(db, "get_incident", incident_id=12345)
    assert not r.ok and r.error["code"] == "no_data"


# ------------------------------------------------------------ nameplates ---

def test_ocr_flags_become_a_confidence_per_field():
    flags = at.field_flags("ocr_disagreed_grease_interval,ocr_no_majority_grease_interval,"
                           "failed_format_lubricant,lubricant_without_grade,bad_weight,"
                           "ocr_disagreed_lubricant,something_new")
    assert at.confidence(flags["grease_interval_h"]) == "uncertain"
    assert at.confidence(flags["lubricant"]) == "unreadable"
    assert at.confidence(flags["weight_kg"]) == "unreadable"
    assert flags["_other"] == ["something_new"]
    assert at.confidence(at.field_flags("ocr_disagreed_lubricant")["lubricant"]) == "read_by_majority"
    assert at.confidence([]) == "clear"


@pytest.mark.parametrize("warnings,field,level", [
    # --engine both (ingest_images.py, 6 Nov 2026)
    ("engines_disagree_grease_interval", "grease_interval_h", "engines_disagreed"),
    ("engines_disagree_power", "voltage_max_v", "engines_disagreed"),
    ("ocr_no_majority_power", "phases", "uncertain"),
    ("engines_disagree_serial_no", "serial_no", "engines_disagreed"),
    ("engines_disagree_mfg_date", "mfg_month", "engines_disagreed"),
    ("one_engine_only_capacity", "capacity_raw", "read_by_one_engine"),
    ("engines_disagree_lubricant,failed_format_lubricant", "lubricant", "unreadable"),
    ("engines_disagree_weight,ocr_no_majority_weight", "weight_kg", "engines_disagreed"),
    # CNC-01: the model put the rest of the power line under capacity (trap list I25)
    ("one_engine_only_capacity,capacity_looks_like_power_line", "capacity_raw", "unreadable"),
])
def test_two_engine_warnings_become_a_confidence_too(warnings, field, level):
    flags = at.field_flags(warnings)
    assert "_other" not in flags                       # every warning tied to its field
    assert at.confidence(flags[field]) == level


def test_two_engine_disagreement_is_never_stated_as_fact(db):
    r = call(db, "get_nameplate", machine="CNC-01")
    f = r.data["fields"]
    assert f["power_kw"]["confidence"] == "engines_disagreed"
    # the power line's other parts were compared too: the doubt covers them all
    assert f["phases"]["confidence"] == f["voltage_min_v"]["confidence"] == "engines_disagreed"
    assert f["capacity_raw"]["confidence"] == "read_by_one_engine"
    assert any("Not reliably read" in n and "power_kw" in n for n in r.notes)
    assert any("chosen by rule" in n for n in r.notes)
    assert r.data["read_by"].startswith("both:")


def test_a_clear_reading_of_an_implausible_plate_value_carries_its_concern(db):
    """CNC-01: both engines read 9257 kg; a clear reading of a doubtful plate line."""
    w = call(db, "get_nameplate", machine="CNC-01").data["fields"]["weight_kg"]
    assert w["value"] == 9257.0 and w["confidence"] == "clear"
    assert "CNC-02" in w["concern"] and "I19" in w["concern"]


def test_eye_checks_cite_the_trap_list_item_for_their_field(db):
    r = call(db, "get_nameplate", machine="CRN-02")
    assert "trap list I15" in r.data["fields"]["grease_interval_h"]["checked"]
    oil = call(db, "get_nameplate", machine="WELD-02").data["fields"]["lubricant"]
    assert "trap list I18" in oil["concern"]


def test_a_tie_break_value_is_flagged_not_stated(db):
    """CNC-03's grease interval: three OCR readings, none agreeing (trap list I15)."""
    r = call(db, "get_nameplate", machine="CNC-03")
    assert r.data["fields"]["grease_interval_h"]["confidence"] == "uncertain"
    assert any("grease_interval_h" in n and "Not reliably read" in n for n in r.notes)
    assert r.sources[0]["file"] == "nameplate_CNC-03.jpg"


def test_known_plate_problems_travel_with_the_value(db):
    r = call(db, "get_nameplate", machine="BLAST-01")
    assert "welding-current" in r.data["fields"]["capacity_raw"]["concern"]
    assert r.data["fields"]["manufacturer"]["confidence"] in ("clear", "not_read")


def test_no_photo_means_no_nameplate_not_a_guess(db):
    r = call(db, "get_nameplate", machine="MILL-01")
    assert r.ok and r.data["has_nameplate"] is False
    assert "no nameplate photo" in r.notes[0]


# ----------------------------------------------------------- lubrication ---

def test_shop_wide_oil_is_not_a_conflict_with_the_plate(db):
    """Real data, 5 Nov 2026: CNC-03's plate VG68 was reported as disagreeing with the
    shop's VG32 hydraulic oil (listed for ALL). Different systems, not a conflict."""
    r = call(db, "machine_lubrication", machine="cnc 2")
    d = r.data
    assert d["nameplate"]["lubricant"]["value"] == "ISO VG32"
    assert [s["part_no"] for s in d["spares_sheet"]] == ["HYD-OIL-46"]
    assert d["grades_agree"] is None and "shop-wide stock" in d["which_to_trust"]
    assert not any("different oil grades" in n for n in r.notes)
    assert any("doesn't contradict the plate" in n and "HYD-OIL-46" in n for n in r.notes)
    assert {s["kind"] for s in r.sources} == {"image", "file"}
    assert any("trap list D10" in n for n in r.notes)


def test_a_machines_own_oil_is_compared_with_its_plate(db):
    r = call(db, "machine_lubrication", machine="CNC-03")
    assert r.data["grades_agree"] is True                 # plate VG32, WAY-OIL-32 for CNC-03


def test_a_value_checked_by_eye_travels_with_the_ocr_value(db):
    """Trap list I15: OCR stored 590 h; the photo, checked by eye, says 500 h."""
    r = call(db, "machine_lubrication", machine="CNC-03")
    g = r.data["nameplate"]["grease_interval_h"]
    assert g["value"] == 590 and g["confidence"] == "uncertain"      # stored value untouched
    assert "photo reads 500 h" in g["concern"] and "Stored 590" in g["concern"]
    assert any("photo reads 500 h" in n for n in r.notes)


def test_a_value_that_matches_the_eye_check_is_trusted_not_flagged(db):
    """CRN-02 after --engine both: 500 h and 2000 kg are stored, as the photo reads.
    The old note ('OCR stored 509 h') would describe an error that is no longer there."""
    r = call(db, "machine_lubrication", machine="CRN-02")
    g = r.data["nameplate"]["grease_interval_h"]
    assert g["value"] == 500 and g["confidence"] == "checked_by_eye"
    assert "concern" not in g and "as stored" in g["checked"]
    assert not any("not reliably read" in n for n in r.notes)
    text = json.dumps(r.to_dict())
    assert "509" not in text and "OCR stored" not in text
    cap = call(db, "get_nameplate", machine="crane 2").data["fields"]["capacity_raw"]
    assert cap["confidence"] == "checked_by_eye" and "SOP-CRANE-02" in cap["concern"]
    assert "2600" not in cap["concern"]


def test_an_unreadable_value_with_a_known_photo_value_gives_both(db):
    """WELD-02: OCR read 'M58' (not a grade); the photo, checked by eye, says M68."""
    r = call(db, "get_nameplate", machine="WELD-02")
    oil = r.data["fields"]["lubricant"]
    assert oil["confidence"] == "unreadable"
    assert "Shell Tonna S3 M68" in oil["concern"] and "Inverter welders" in oil["concern"]


def test_an_unreadable_oil_is_never_passed_off_as_read(db):
    r = call(db, "machine_lubrication", machine="WELD-02")
    assert r.data["nameplate"]["lubricant"]["confidence"] == "unreadable"
    assert any("not reliably read" in n for n in r.notes)
    assert any("Inverter welders" in n for n in r.notes)


# --------------------------------------------------------------- spares ---

def test_spares_for_a_machine_with_stock_vs_minimum_and_both_units(db):
    r = call(db, "get_spares", machine="CNC-02")
    parts = {p["part_no"]: p for p in r.data["parts"]}
    assert parts["BRG-6310"]["below_min"] is True and parts["BRG-6310"]["lead_time"] == "14-21 days"
    assert parts["BRG-6310"]["lead_time_as_written"] == "2-3 weeks"
    assert r.sources[0]["locator"] == "rows 5, 12"


def test_a_machine_with_only_shop_wide_spares_is_told_so(db):
    r = call(db, "get_spares", machine="MILL-01")
    assert any("no spares specifically for MILL-01" in n for n in r.notes)


def test_belt_sizes_come_with_both_units_and_the_part_number_warning(db):
    r = call(db, "get_spares", part_no="belt-5v-850")
    p = r.data["parts"][0]
    assert p["size_mm"] == 2159.0 and p["size_in"] == 85.0
    assert any("tenths of an inch" in n for n in r.notes)


def test_below_minimum_lists_each_part_once(db):
    r = call(db, "get_spares", below_min_only=True)
    assert [p["part_no"] for p in r.data["parts"]] == ["BRG-6310"]


# ---------------------------------------------------------- work orders ---

def test_work_orders_keep_open_costs_apart_and_flag_double_entries(db):
    r = call(db, "list_work_orders", machine="CNC-02")
    d = r.data
    assert d["count"] == 4 and d["cost_not_closed_sgd"] == 941.13
    assert d["cost_closed_sgd"] == 740.0
    assert d["possible_double_entries"] == [[10727, 10728]]
    assert d["work_orders"][0]["wo_id"] == 11591          # newest first


def test_open_means_anything_not_closed(db):
    r = call(db, "list_work_orders", status="open")
    assert sorted(w["wo_id"] for w in r.data["work_orders"]) == [11570, 11591]


def test_work_order_type_filter(db):
    r = call(db, "list_work_orders", machine="CNC-02", wo_type="preventive")
    assert r.data["count"] == 2


# --------------------------------------------------------------- sensors ---

def test_a_machine_without_sensors_has_no_trend_not_a_made_up_one(db):
    r = call(db, "sensor_trend", machine="CRN-01")
    assert not r.ok and r.error["code"] == "no_data" and "CNC-02" in r.error["message"]


def test_weekly_trend_with_coverage_and_its_source_lines(db):
    r = call(db, "sensor_trend", machine="CNC-02", date_from="2026-01-05", date_to="2026-01-18")
    s = r.data["series"]
    assert [x["period_start"] for x in s] == [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-12")]
    assert s[0]["expected"] == 7 * 120 and 97 < s[0]["coverage_pct"] < 99   # every 50th missing
    assert r.sources[0]["file"] == "vibration_temp_2026H1.csv"
    assert "readings, lines" in r.sources[0]["locator"]


def test_too_many_daily_buckets_are_refused(db):
    r = call(db, "sensor_trend", machine="CNC-02", bucket="day")
    assert not r.ok and r.error["code"] == "bad_arguments"


def test_cnc02_is_off_and_it_is_the_machine_not_the_shop(db):
    r = call(db, "compare_sensor_windows", machine="CNC-02")
    d = r.data
    assert d["verdict"] == "machine_specific"
    assert d["elevated_metrics"] == ["vib", "temp"] and d["other_machines_elevated"] == []
    assert d["target"]["vib"]["recent_mean"] > d["target"]["vib"]["baseline_mean"] + 1.5
    assert d["baseline"] == {"from": date(2026, 1, 1), "to": date(2026, 3, 31)}
    assert d["recent"] == {"from": date(2026, 6, 1), "to": date(2026, 6, 30)}
    assert {c["machine_id"] for c in d["chart_weekly"]} == set(SENSORED)
    assert "3 standard deviations" in d["rule"] and "2 °C" in d["rule"]
    assert [x["days"] for x in d["chart_weekly"] if x["machine_id"] == "CNC-02"][0] == 4
    assert {s["what"] for s in r.sources} == {f"{m} sensor log" for m in SENSORED}


def test_a_temperature_rise_counts_in_degrees_not_percent():
    """CNC-02 on the real data: 51.93 -> 55.97 °C is 7.8% but 8 sd and 4.0 °C: elevated.
    A 10% rule on Celsius missed it (5 Nov 2026)."""
    days = pd.date_range("2026-01-01", "2026-06-30")
    rng = np.random.default_rng(1)
    temp = 51.93 + rng.normal(0, 0.5, len(days)) + np.where(days >= "2026-06-01", 4.04, 0)
    d = pd.DataFrame({"day": days, "readings": 115, "vib_mean": 2.3, "temp_mean": temp})
    s = at.window_stats(d, date(2026, 1, 1), date(2026, 3, 31), date(2026, 6, 1), date(2026, 6, 30))
    assert s["temp"]["change_pct"] < 10 and s["temp"]["elevated"] is True
    assert s["vib"]["elevated"] is False                  # flat
    small = d.assign(temp_mean=51.93 + np.where(days >= "2026-06-01", 1.5, 0))
    small.loc[:89, "temp_mean"] += rng.normal(0, 0.05, 90)
    s = at.window_stats(small, date(2026, 1, 1), date(2026, 3, 31), date(2026, 6, 1), date(2026, 6, 30))
    assert s["temp"]["change_in_sd"] > 3 and s["temp"]["elevated"] is False   # under 2 °C


def test_a_flat_machine_is_not_elevated(db):
    assert call(db, "compare_sensor_windows", machine="CNC-01").data["verdict"] == "not_elevated"


def test_overlapping_windows_are_refused(db):
    r = call(db, "compare_sensor_windows", machine="CNC-02", baseline_from="2026-01-01",
             baseline_to="2026-06-15", recent_from="2026-06-01", recent_to="2026-06-30")
    assert not r.ok and r.error["code"] == "bad_arguments"


# ------------------------------------------------------------------ SOPs ---

def test_the_blasting_chamber_sop_is_quoted_whole_with_its_lines(db):
    r = call(db, "find_sop", question="what's the SOP before entering the blasting chamber")
    q = r.data["quote_whole"]
    assert r.data["doc_no"] == "SOP-CONFINED-03"
    assert q.startswith("SOP-CONFINED-03 — CONFINED SPACE ENTRY (rev 2, Sep 2025)")
    assert "Applies to: blasting chamber (BLAST-01)" in q
    assert "O2 19.5-23.5%" in q and q.endswith("[Source: SOP-CONFINED-03, lines 3-6]")
    assert r.sources[0]["file"] == "SOP-CONFINED-03.txt" and r.sources[0]["locator"] == "lines 3-6"
    assert any("entry permit" in n for n in r.notes)


def test_no_sop_for_welding_means_no_sop(db):
    r = call(db, "find_sop", question="welding hot work PPE for grinding")
    assert r.ok and r.data["found"] is False
    assert "No SOP in the data covers this" in r.notes[0]


def test_lockout_on_a_cnc_names_the_missing_manual(db):
    r = call(db, "find_sop", question="how do I lock out", machine="CNC-02")
    assert r.data["doc_no"] == "SOP-LOTO-01"
    assert any("machine manual, which isn't in the data" in n for n in r.notes)


def test_a_machine_no_sop_names(db):
    r = call(db, "find_sop", machine="WELD-01")
    assert r.data["found"] is False and "No SOP in the data names WELD-01" in r.notes[0]


def test_an_sop_that_doesnt_name_the_machine_says_so(db):
    r = call(db, "find_sop", question="lockout tagout", machine="WELD-01")
    assert r.data["doc_no"] == "SOP-LOTO-01"
    assert any("doesn't name WELD-01" in n for n in r.notes)


# ------------------------------------------------------------- query_sql ---

@pytest.mark.parametrize("sql,why", [
    ("UPDATE cmms_machines_audit SET machine_name = 'x'", "single SELECT"),
    ("SELECT machine_id FROM machine_overview; DROP TABLE sops", "one statement"),
    ("SELECT emp_name FROM cmms_employees", "cmms_employees"),
    ("SELECT * FROM incident_detail", "SELECT *"),
    ("SELECT incident_id, severity_as_logged FROM cmms_safety_incidents_audit", "severity_5_worst"),
    ("SELECT machine_id, SLEEP(10) FROM machine_overview", "SLEEP"),
    ("SELECT machine_id FROM machine_overview UNION SELECT doc_no FROM sops", "single SELECT"),
])
def test_query_sql_refuses_what_it_must(sql, why):
    pytest.importorskip("sqlglot")
    with pytest.raises(at.ToolError) as e:
        at.check_sql(sql)
    assert e.value.code == "not_allowed" and why.lower() in e.value.message.lower()


def test_query_sql_caps_the_rows_and_allows_counts():
    pytest.importorskip("sqlglot")
    sql = at.check_sql("SELECT machine_id, COUNT(*) AS n FROM incident_detail GROUP BY machine_id")
    assert sql.endswith("LIMIT 200")
    assert at.check_sql("SELECT machine_id FROM machine_overview LIMIT 5").endswith("LIMIT 5")
    assert at.check_sql("SELECT machine_id FROM machine_overview LIMIT 5000").endswith("LIMIT 200")


def test_query_sql_runs_and_cites_what_it_read(db):
    pytest.importorskip("sqlglot")
    r = call(db, "query_sql", sql="SELECT machine_id, COUNT(*) AS n FROM incident_detail "
                                  "GROUP BY machine_id ORDER BY n DESC", reason="test")
    assert r.ok and r.data["rows"][0]["n"] == 3
    assert [s["object"] for s in r.sources] == ["incident_detail"]


def test_query_sql_reads_only_what_agent_user_grants():
    au = pytest.importorskip("agent_user")
    from relationships import Names

    assert at.ALLOWED_OBJECTS == set(au.allowed_objects(Names()))


# ------------------------------------------------------ honest failures ---

def test_a_dead_database_gives_couldnt_check_never_a_value():
    def down(sql, params):
        raise ConnectionError("MySQL server has gone away")

    r = at.run_tool(at.Db(down, "mysql"), "count_incidents", {})
    assert not r.ok and r.error["code"] == "tool_failed"
    assert "couldn't" in r.error["message"] and "don't estimate" in r.error["message"]
    assert r.data is None


def test_bad_arguments_are_explained_so_the_model_can_retry(db):
    r = call(db, "count_incidents", at_least_as_severe_as=9, severity_scale="5_is_worst")
    assert not r.ok and r.error["code"] == "bad_arguments"
    assert "at_least_as_severe_as" in r.error["message"]
    r = call(db, "list_machines", surprise=1)               # extra argument
    assert r.error["code"] == "bad_arguments"


def test_arguments_may_arrive_as_a_json_string(db):
    r = at.run_tool(db, "get_spares", '{"part_no": "BRG-6310"}')
    assert r.ok and r.data["count"] == 1


def test_an_unknown_tool(db):
    assert at.run_tool(db, "delete_everything", {}).error["code"] == "unknown_tool"


def test_the_app_never_connects_as_the_owner(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DB_USERNAME", "ingest")
    monkeypatch.setenv("AGENT_DB_USERNAME", "ingest")
    monkeypatch.setenv("AGENT_DB_PASSWORD", "x")
    with pytest.raises(ValueError, match="owner account"):
        at.connect(str(env))
    monkeypatch.delenv("AGENT_DB_PASSWORD")
    monkeypatch.setenv("AGENT_DB_USERNAME", "agent_ro")
    with pytest.raises(ValueError, match="AGENT_DB_PASSWORD"):
        at.connect(str(env))


# ---------------------------------------------------------------- photos ---

def test_the_photo_behind_a_source_is_fetched_and_hash_checked(db):
    img = at.fetch_image(db, "c" * 64)
    assert img["bytes"].startswith(b"\xff\xd8") and img["mime"] == "image/jpeg"
    assert img["source_file"] == "nameplate_CNC-02.jpg" and img["variant"] == "original"


def test_a_photo_whose_bytes_changed_is_never_shown(db):
    assert at.fetch_image(db, "d" * 64) is None             # bytes don't match their hash


@pytest.mark.parametrize("sha", ["e" * 64, "not-a-hash", None, "C" * 64])
def test_no_photo_or_a_bad_hash_gives_nothing(db, sha):
    assert at.fetch_image(db, sha) is None


def test_an_answers_photos_come_from_its_image_sources_once_each(db):
    r = call(db, "machine_lubrication", machine="CNC-02")
    srcs = at.image_sources(r.sources + r.sources)
    assert [s["file"] for s in srcs] == ["nameplate_CNC-02.jpg"]
    assert at.fetch_image(db, srcs[0]["sha256"]) is not None


def test_query_sql_never_returns_the_photo_bytes():
    pytest.importorskip("sqlglot")
    with pytest.raises(at.ToolError) as e:
        at.check_sql("SELECT image_bytes FROM img_files_public")
    assert e.value.code == "not_allowed"



@pytest.mark.parametrize("mid,value,level,date", [
    ("CRN-01", "KO4778956", "checked_by_eye", "7 Nov 2026"),   # after the I21 fix
    ("CRN-01", "KO04778956", None, "7 Nov 2026"),              # an older load: both given
    ("LTH-01", "CO5612371", "checked_by_eye", "7 Nov 2026"),
    ("CNC-03", "DM4958733", "checked_by_eye", "4 Nov 2026"),
])
def test_serials_checked_by_eye(mid, value, level, date):
    checked, note = at.eye_check(mid, "serial_no", value)
    assert checked == level
    assert "trap list I21" in note and date in note
    if level is None:
        assert "photo reads KO4778956" in note



def test_a_form_value_read_by_a_person_is_said_so(db):
    r = call(db, "get_incident", incident_id=904)
    assert any("severity couldn't be read by OCR" in n and "by a person" in n for n in r.notes)
    assert not any("by a person" in n for n in call(db, "get_incident", incident_id=914).notes)


@pytest.mark.parametrize("desc,mid,flagged", [
    ("weld flash burn", "CMP-01", True),          # 911: a welding injury on the air compressor
    ("weld flash burn", "WELD-01", False),
    ("near miss: sling snapped during lift", "CNC-02", True),       # 957
    ("near miss: sling snapped during lift", "CRN-01", False),
    ("finger caught in chuck (glove)", "CNC-02", False),
    ("finger caught in chuck (glove)", "CRN-01", True),             # 923
    ("heat stress - confined space", "WELD-02", False),             # not a clear case: left
    ("slip on hydraulic oil", "CNC-03", False),
])
def test_descriptions_that_dont_fit_the_machine(desc, mid, flagged):
    note = at.description_misfit(desc, mid)
    assert (note is not None) is flagged
    if flagged:
        assert mid in note and "trap list C6" in note


def test_get_incident_flags_a_description_that_doesnt_fit(db):
    assert any("trap list C6" in n for n in call(db, "get_incident", incident_id=914).notes)
    assert not any("trap list C6" in n for n in call(db, "get_incident", incident_id=906).notes)