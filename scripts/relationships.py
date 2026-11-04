#!/usr/bin/env python3
"""Establish the relationships between the cleaned tables of all four sources.

Each ingest script loads one source on its own:

    ingest_cmms.py    cmms_machines, cmms_employees, cmms_work_orders, cmms_safety_incidents
    ingest_logs.py    vibration_temp
    ingest_spares.py  critical_spares, critical_spare_machines
    ingest_images.py  img_nameplates, img_incident_reports
    ingest_sops.py    sops, sop_steps, sop_machines

They share keys (machine_id, emp_code, part_no, incident_id), but nothing ties the sources
together in the database. This script does, in four steps:

    check                 every relationship: child rows, matched, NULL, orphans (child
                          values with no parent row), ambiguous parents (key not unique)
    table_relationships   the catalog: one row per relationship with its check result, so
                          the agent and the data quality report can read the join paths
    views                 cross-source views that join along those paths safely
    --foreign-keys        optional: real FOREIGN KEYs where the data allows (no orphans)

Relationships (child -> parent):

    cmms_work_orders.machine_id          -> cmms_machines.machine_id
    cmms_safety_incidents.machine_id     -> cmms_machines.machine_id
    vibration_temp.machine_id            -> cmms_machines.machine_id   sensor CSV -> CMMS
    critical_spare_machines.machine_code -> cmms_machines.machine_id   spares sheet -> CMMS ('ALL' = every machine)
    critical_spare_machines.part_no      -> critical_spares.part_no    FK already made by ingest_spares.py
    cmms_work_orders.emp_code            -> cmms_employees.emp_code
    cmms_safety_incidents.emp_code       -> cmms_employees.emp_code
    img_nameplates.asset_tag             -> cmms_machines.machine_id   nameplate photo -> CMMS
    img_incident_reports.incident_id     -> cmms_safety_incidents.incident_id   scanned form -> CMMS
    img_incident_reports.machine_id      -> cmms_machines.machine_id
    sop_machines.machine_id              -> cmms_machines.machine_id   machine named in an SOP -> CMMS
    sop_machines.doc_no                  -> sops.doc_no                FK (doc_no, revision) made by ingest_sops.py

Views:

    machine_overview          one row per machine: work orders, incidents, spares, sensor coverage
    machine_spares            spares per machine, 'ALL' expanded to every machine
    work_order_detail         work orders + machine + employee trade/shift (no names: PDPA)
    incident_detail           incidents + machine + employee trade/shift, severity on the
                              5 = worst scale only (severity_5_worst)
    work_order_sensor_window  each work order with the sensor readings in the N days up to it
    machine_nameplate         one row per machine: what its nameplate photo says (oil, grease
                              interval, power, ...), has_nameplate = 0 if there is no photo
    machine_sops              one row per machine and current SOP that names it (title,
                              revision, how it was linked); has_sop = 0 if none does
    incident_report_vs_cmms   one row per scanned form and field: the value on the form, the
                              value logged in the CMMS, and whether they agree

Orphans are reported, never dropped: the views use LEFT JOINs and flag them with
machine_known / employee_known.

Each real run is recorded in ingest_runs like a load: script 'relationships.py', source
'(database)', file_sha256 = a fingerprint of the check results (same hash = the data
looked the same), rows_read = child rows checked, rows_loaded = matched, rows_rejected =
orphans. Catalog rows carry its run_id. A failed run is recorded with its error.

Foreign keys are opt-in because the loads replace rows: reloading the CMMS dump deletes
the cmms_machines rows other tables would point at, and --recreate drops the tables.
Before reloading, run --drop-foreign-keys; after loading, run this script again.

Usage
    python scripts/relationships.py --dry-run --mermaid docs/er_diagram.md
    python scripts/relationships.py                     # check + catalog + views; safe after every load
    python scripts/relationships.py --foreign-keys      # also enforce where there are no orphans
    python scripts/relationships.py --drop-foreign-keys # before reloading a source
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from db_config import add_common_args, load_db_config, safe_identifier
from lineage import finish_run, start_run

SOURCE_LABEL = "(database)"  # ingest_runs.source_file for a run that reads no file
FK_PREFIX = "fk_rel_"  # only constraints with this prefix are ever dropped by this script
CATALOG = "table_relationships"


# ------------------------------------------------------------------ tables ---

@dataclass(frozen=True)
class Names:
    """Logical table name -> real table name (the ingest scripts' --prefix / --table)."""
    cmms_prefix: str = "cmms_"
    sensor_table: str = "vibration_temp"
    image_prefix: str = "img_"

    def mapping(self) -> dict[str, str]:
        m = {
            "machines": f"{self.cmms_prefix}machines",
            "employees": f"{self.cmms_prefix}employees",
            "work_orders": f"{self.cmms_prefix}work_orders",
            "safety_incidents": f"{self.cmms_prefix}safety_incidents",
            "readings": self.sensor_table,
            "spares": "critical_spares",
            "spare_machines": "critical_spare_machines",
            "nameplates": f"{self.image_prefix}nameplates",
            "incident_reports": f"{self.image_prefix}incident_reports",
            "sops": "sops",                          # ingest_sops.py has no prefix option
            "sop_machines": "sop_machines",
        }
        return {k: safe_identifier(v) for k, v in m.items()}

    def __getitem__(self, logical: str) -> str:
        return self.mapping()[logical]


# Cleaned-schema columns, for the ER diagram: (type, column, keys, comment).
# Surrogate ids and raw_id (lineage to *_raw) are left out to keep the diagram readable.
ENTITIES: dict[str, list[tuple[str, str, str, str]]] = {
    "machines": [
        ("varchar", "machine_id", "UK", ""), ("varchar", "machine_name", "", ""),
        ("varchar", "model", "", ""), ("int", "year_installed", "", ""),
    ],
    "employees": [
        ("int", "emp_id", "UK", ""), ("varchar", "emp_code", "", "not unique in source"),
        ("varchar", "emp_name", "", "PDPA: kept out of views"),
        ("varchar", "trade", "", ""), ("varchar", "shift", "", ""),
    ],
    "work_orders": [
        ("int", "wo_id", "UK", ""), ("varchar", "machine_id", "FK", ""),
        ("varchar", "emp_code", "FK", ""), ("varchar", "wo_type", "", ""),
        ("varchar", "description", "", ""), ("date", "wo_date", "", ""),
        ("decimal", "duration_hrs", "", ""), ("decimal", "cost_sgd", "", ""),
        ("varchar", "status", "", ""),
    ],
    "safety_incidents": [
        ("int", "incident_id", "UK", ""), ("date", "incident_date", "", ""),
        ("varchar", "machine_id", "FK", ""), ("varchar", "emp_code", "FK", ""),
        ("varchar", "description", "", ""),
        ("int", "severity_as_logged", "", "meaning depends on date"),
        ("varchar", "severity_scale", "", ""),
        ("tinyint", "severity_5_worst", "", "always query this one"),
        ("int", "lost_days", "", ""), ("varchar", "reporting", "", ""),
    ],
    "readings": [
        ("varchar", "machine_id", "FK", ""), ("datetime", "sampled_at", "", "Singapore time"),
        ("bigint", "epoch_ms", "UK", "UTC; unique with machine_id"),
        ("decimal", "vib_rms_mms", "", ""), ("decimal", "temp_c", "", "Celsius only"),
    ],
    "spares": [
        ("varchar", "part_no", "PK", ""), ("varchar", "description", "", ""),
        ("int", "qty", "", ""), ("int", "min_qty", "", ""), ("tinyint", "below_min", "", ""),
        ("decimal", "unit_cost", "", ""), ("varchar", "cost_per", "", ""),
        ("decimal", "size_mm", "", "metric and imperial both in mm"),
        ("int", "lead_time_min_days", "", ""), ("int", "lead_time_max_days", "", ""),
        ("tinyint", "is_local_supply", "", ""), ("varchar", "location", "", ""),
    ],
    "spare_machines": [
        ("varchar", "part_no", "PK, FK", ""),
        ("varchar", "machine_code", "PK, FK", "ALL = every machine"),
    ],
    "nameplates": [
        ("varchar", "asset_tag", "UK, FK", "= machine_id"), ("varchar", "manufacturer", "", ""),
        ("varchar", "model", "", ""), ("varchar", "serial_no", "", ""),
        ("char", "mfg_month", "", "YYYY-MM"), ("decimal", "power_kw", "", ""),
        ("decimal", "weight_kg", "", ""), ("varchar", "lubricant", "", ""),
        ("int", "grease_interval_h", "", ""),
    ],
    "incident_reports": [
        ("int", "incident_id", "UK, FK", "from report no IR-yyyy-nnn"),
        ("varchar", "report_no", "", ""), ("date", "incident_date", "", ""),
        ("varchar", "machine_id", "FK", ""), ("varchar", "emp_code", "", ""),
        ("varchar", "severity_scale", "", "printed on the form"),
        ("tinyint", "severity_5_worst", "", "converted with the printed scale"),
        ("int", "lost_days", "", ""), ("varchar", "immediate_action", "", ""),
        ("varchar", "root_cause", "", ""), ("tinyint", "signature_present", "", ""),
    ],
    "sops": [
        ("varchar", "doc_no", "PK", "e.g. SOP-LOTO-01"), ("int", "revision", "PK", ""),
        ("varchar", "title", "", ""), ("char", "rev_month", "", "YYYY-MM"),
        ("tinyint", "is_current", "", "highest revision loaded"),
        ("int", "step_count", "", "steps in sop_steps, quoted whole"),
        ("text", "search_text", "", "what the app searches (FULLTEXT)"),
    ],
    "sop_machines": [
        ("varchar", "doc_no", "PK, FK", ""), ("int", "revision", "PK, FK", ""),
        ("varchar", "machine_id", "PK, FK", ""),
        ("varchar", "how", "", "id | name | alias"),
        ("varchar", "evidence", "", "the SOP's words"),
    ],
}


# ----------------------------------------------------------- relationships ---

@dataclass(frozen=True)
class Relationship:
    name: str              # short id; a foreign key made here is named fk_rel_<name>
    child: str             # logical table names (see Names)
    child_col: str
    parent: str
    parent_col: str
    meaning: str           # label in the ER diagram and the catalog
    parent_end: str        # Mermaid: '||' exactly one parent, '|o' zero or one
    can_enforce: bool      # could be a real FOREIGN KEY if the data is clean
    wildcard: str | None = None   # child value that means "every parent"
    note: str = ""


RELATIONSHIPS = [
    Relationship("wo_machine", "work_orders", "machine_id", "machines", "machine_id",
                 "has work orders", "|o", True, note="CMMS work order -> CMMS machine"),
    Relationship("incident_machine", "safety_incidents", "machine_id", "machines", "machine_id",
                 "has incidents", "|o", True, note="CMMS incident -> CMMS machine"),
    Relationship("reading_machine", "readings", "machine_id", "machines", "machine_id",
                 "has sensor readings", "||", True, note="sensor CSV -> CMMS machine"),
    Relationship("spare_machine", "spare_machines", "machine_code", "machines", "machine_id",
                 "uses spares", "|o", False, wildcard="ALL",
                 note="spares sheet -> CMMS machine; 'ALL' means every machine, so no FK"),
    Relationship("spare_part", "spare_machines", "part_no", "spares", "part_no",
                 "is listed for", "||", True, note="FK fk_csm_part made by ingest_spares.py"),
    Relationship("wo_employee", "work_orders", "emp_code", "employees", "emp_code",
                 "did work orders", "|o", False,
                 note="emp_code has no unique key in cmms_employees, so no FK"),
    Relationship("incident_employee", "safety_incidents", "emp_code", "employees", "emp_code",
                 "involved in incidents", "|o", False,
                 note="emp_code has no unique key in cmms_employees, so no FK"),
    Relationship("nameplate_machine", "nameplates", "asset_tag", "machines", "machine_id",
                 "has nameplate photo", "||", True, note="nameplate photo -> CMMS machine"),
    Relationship("report_incident", "incident_reports", "incident_id", "safety_incidents",
                 "incident_id", "has scanned form", "||", True,
                 note="scanned incident form -> CMMS incident (IR-yyyy-nnn -> nnn)"),
    Relationship("report_machine", "incident_reports", "machine_id", "machines", "machine_id",
                 "named on scanned forms", "|o", True,
                 note="machine on the scanned form -> CMMS machine"),
    Relationship("sop_machine", "sop_machines", "machine_id", "machines", "machine_id",
                 "covered by SOPs", "||", True,
                 note="machine named in an SOP (id, CMMS name or alias) -> CMMS machine"),
    Relationship("sop_doc", "sop_machines", "doc_no", "sops", "doc_no",
                 "names machines", "||", False,
                 note="FK fk_sop_machines on (doc_no, revision) made by ingest_sops.py; "
                      "doc_no alone repeats across revisions"),
]


# ------------------------------------------------------------------- check ---

def _norm(v) -> str:
    """Compare keys the way MySQL's default collation does: trimmed, case-insensitive."""
    return str(v).strip().casefold()


@dataclass
class Check:
    rows: int = 0
    matched: int = 0
    null_rows: int = 0
    wildcard_rows: int = 0
    orphans: dict = field(default_factory=dict)   # child value -> rows
    ambiguous: list = field(default_factory=list)  # parent keys that occur more than once

    @property
    def orphan_rows(self) -> int:
        return sum(self.orphans.values())

    def orphan_text(self, limit: int = 500) -> str | None:
        if not self.orphans:
            return None
        s = ", ".join(f"{k} ({n})" for k, n in sorted(self.orphans.items(), key=lambda t: -t[1]))
        return s if len(s) <= limit else s[:limit - 3] + "..."


def check_relationship(child_counts: dict, parent_counts: dict,
                       wildcard: str | None = None) -> Check:
    """child_counts / parent_counts: key value -> number of rows (None = NULL).

    A child row is matched if its key is a parent key (or the wildcard), NULL if it
    has no key, and an orphan otherwise.
    """
    parent = Counter()
    for k, n in parent_counts.items():
        if k is not None and str(k).strip():
            parent[_norm(k)] += n
    wild = _norm(wildcard) if wildcard else None

    c = Check()
    for k, n in child_counts.items():
        c.rows += n
        if k is None or not str(k).strip():
            c.null_rows += n
        elif wild is not None and _norm(k) == wild:
            c.wildcard_rows += n
            c.matched += n
        elif _norm(k) in parent:
            c.matched += n
        else:
            c.orphans[str(k)] = c.orphans.get(str(k), 0) + n
    c.ambiguous = sorted({str(k) for k in parent_counts
                          if k is not None and str(k).strip() and parent[_norm(k)] > 1})
    return c


def fingerprint(names: Names, checks: dict) -> str:
    """SHA-256 of the check results: the same hash means the data looked the same.

    Stands in for the file hash in ingest_runs. Order-independent; covers the table
    names, counts, orphan keys and ambiguous keys of every relationship.
    """
    state = {}
    for rel in RELATIONSHIPS:
        c = checks.get(rel.name)
        state[rel.name] = None if c is None else {
            "child": names[rel.child], "parent": names[rel.parent],
            "rows": c.rows, "matched": c.matched, "null": c.null_rows,
            "wildcard": c.wildcard_rows, "orphans": dict(sorted(c.orphans.items())),
            "ambiguous": sorted(c.ambiguous),
        }
    blob = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def run_totals(checks: dict) -> dict:
    """rows_read / rows_loaded / rows_rejected for ingest_runs."""
    done = [c for c in checks.values() if c is not None]
    return {"rows_read": sum(c.rows for c in done),
            "rows_loaded": sum(c.matched for c in done),
            "rows_rejected": sum(c.orphan_rows for c in done)}


def plan_foreign_key(rel: Relationship, check: Check | None, enforced: bool) -> tuple[str, str]:
    """('exists' | 'add' | 'skip', reason)."""
    if enforced:
        return "exists", ""
    if not rel.can_enforce:
        return "skip", rel.note
    if check is None:
        return "skip", "table not loaded"
    if check.orphan_rows:
        return "skip", f"{check.orphan_rows} orphan row(s): fix the data first"
    if check.ambiguous:
        return "skip", f"parent key not unique: {', '.join(check.ambiguous[:5])}"
    return "add", ""


# ------------------------------------------------------------------- views ---

# Employees with a unique emp_code; an ambiguous code gets no trade/shift instead of
# duplicating the work order. emp_name is never selected (PDPA).
_EMPLOYEES = """(SELECT emp_code, MIN(trade) AS trade, MIN(shift) AS shift
          FROM {employees} WHERE emp_code IS NOT NULL
          GROUP BY emp_code HAVING COUNT(*) = 1)"""

# Scanned form vs CMMS, one row per field. Every value is cast to text with ONE explicit
# collation, so a date, a number and a string compare the same way and MySQL never meets
# two collations it can't mix. Severity is compared only on the 5 = worst scale.
_TEXT = "CAST({} AS CHAR CHARACTER SET utf8mb4) COLLATE utf8mb4_unicode_ci"
REPORT_FIELDS = [  # (field, on the form, logged in the CMMS)
    ("incident_date", "r.incident_date", "i.incident_date"),
    ("machine_id", "r.machine_id", "i.machine_id"),
    ("emp_code", "r.emp_code", "i.emp_code"),
    ("description", "r.description", "i.description"),
    ("severity_scale", "r.severity_scale", "i.severity_scale"),
    ("severity_5_worst", "r.severity_5_worst", "i.severity_5_worst"),
    ("lost_days", "r.lost_days", "i.lost_days"),
    ("reporting", "r.reporting", "i.reporting"),
]
_REPORT_UNION = "\n      UNION ALL\n".join(
    f"""      SELECT r.incident_id, r.report_no, '{name}' AS field,
             {_TEXT.format(form)} AS form_value, {_TEXT.format(logged)} AS logged_value,
             (i.incident_id IS NOT NULL) AS incident_logged, r.raw_id
      FROM {{incident_reports}} r
      LEFT JOIN {{safety_incidents}} i ON i.incident_id = r.incident_id"""
    for name, form, logged in REPORT_FIELDS)

VIEWS: dict[str, tuple[tuple[str, ...], str]] = {
    "machine_spares": (("machines", "spares", "spare_machines"), """
    CREATE OR REPLACE VIEW machine_spares AS
    SELECT m.machine_id, m.machine_name, s.part_no, s.description, s.qty, s.min_qty,
           s.below_min, s.unit_cost, s.cost_per, s.size_mm, s.lead_time_min_days,
           s.lead_time_max_days, s.is_local_supply, s.location,
           sm.machine_code AS listed_for
    FROM {spare_machines} sm
    JOIN {spares} s ON s.part_no = sm.part_no
    JOIN {machines} m ON m.machine_id = sm.machine_code OR sm.machine_code = 'ALL'
    """),
    "work_order_detail": (("work_orders", "machines", "employees"), """
    CREATE OR REPLACE VIEW work_order_detail AS
    SELECT w.wo_id, w.wo_date, w.machine_id, m.machine_name, m.model, w.wo_type,
           w.description, w.duration_hrs, w.cost_sgd, w.status, w.emp_code, e.trade, e.shift,
           (m.machine_id IS NOT NULL) AS machine_known,
           (e.emp_code IS NOT NULL) AS employee_known,
           w.raw_id
    FROM {work_orders} w
    LEFT JOIN {machines} m ON m.machine_id = w.machine_id
    LEFT JOIN """ + _EMPLOYEES + """ e ON e.emp_code = w.emp_code
    """),
    "incident_detail": (("safety_incidents", "machines", "employees"), """
    CREATE OR REPLACE VIEW incident_detail AS
    SELECT i.incident_id, i.incident_date, i.machine_id, m.machine_name, i.description,
           i.severity_5_worst, i.severity_scale, i.lost_days, i.reporting,
           i.emp_code, e.trade, e.shift,
           (m.machine_id IS NOT NULL) AS machine_known,
           (e.emp_code IS NOT NULL) AS employee_known,
           i.raw_id
    FROM {safety_incidents} i
    LEFT JOIN {machines} m ON m.machine_id = i.machine_id
    LEFT JOIN """ + _EMPLOYEES + """ e ON e.emp_code = i.emp_code
    """),
    "work_order_sensor_window": (("work_orders", "readings"), """
    CREATE OR REPLACE VIEW work_order_sensor_window AS
    SELECT w.wo_id, w.wo_date, w.machine_id, w.wo_type, w.description, w.status,
           {window_days} AS window_days,
           COUNT(v.id) AS readings,
           ROUND(AVG(v.vib_rms_mms), 2) AS vib_avg, MAX(v.vib_rms_mms) AS vib_max,
           ROUND(AVG(v.temp_c), 2) AS temp_c_avg, MAX(v.temp_c) AS temp_c_max,
           MIN(v.sampled_at) AS first_reading, MAX(v.sampled_at) AS last_reading
    FROM {work_orders} w
    LEFT JOIN {readings} v
           ON v.machine_id = w.machine_id
          AND v.sampled_at >= w.wo_date - INTERVAL {window_days} DAY
          AND v.sampled_at <  w.wo_date + INTERVAL 1 DAY
    GROUP BY w.wo_id, w.wo_date, w.machine_id, w.wo_type, w.description, w.status
    """),
    "machine_overview": (("machines", "work_orders", "safety_incidents", "readings",
                          "spares", "spare_machines"), """
    CREATE OR REPLACE VIEW machine_overview AS
    SELECT m.machine_id, m.machine_name, m.model, m.year_installed,
      (SELECT COUNT(*) FROM {work_orders} w WHERE w.machine_id = m.machine_id) AS work_orders,
      (SELECT COUNT(*) FROM {work_orders} w
        WHERE w.machine_id = m.machine_id AND w.wo_type = 'breakdown') AS breakdowns,
      (SELECT COUNT(*) FROM {work_orders} w
        WHERE w.machine_id = m.machine_id AND w.status <> 'closed') AS open_work_orders,
      (SELECT MAX(w.wo_date) FROM {work_orders} w WHERE w.machine_id = m.machine_id)
        AS last_work_order,
      (SELECT COUNT(*) FROM {safety_incidents} i WHERE i.machine_id = m.machine_id) AS incidents,
      (SELECT COALESCE(SUM(i.lost_days), 0) FROM {safety_incidents} i
        WHERE i.machine_id = m.machine_id) AS incident_lost_days,
      (SELECT COUNT(*) FROM {spare_machines} sm
        WHERE sm.machine_code IN (m.machine_id, 'ALL')) AS spare_parts,
      (SELECT COUNT(*) FROM {spare_machines} sm JOIN {spares} s ON s.part_no = sm.part_no
        WHERE sm.machine_code IN (m.machine_id, 'ALL') AND s.below_min = 1) AS spares_below_min,
      (SELECT COUNT(*) FROM {readings} v WHERE v.machine_id = m.machine_id) AS sensor_readings,
      (SELECT MIN(v.sampled_at) FROM {readings} v WHERE v.machine_id = m.machine_id)
        AS first_reading,
      (SELECT MAX(v.sampled_at) FROM {readings} v WHERE v.machine_id = m.machine_id)
        AS last_reading
    FROM {machines} m
    """),
    "machine_nameplate": (("machines", "nameplates"), """
    CREATE OR REPLACE VIEW machine_nameplate AS
    SELECT m.machine_id, m.machine_name, m.model AS logged_model, m.year_installed,
           (n.asset_tag IS NOT NULL) AS has_nameplate,
           n.manufacturer, n.model AS plate_model, n.serial_no, n.mfg_month,
           n.power_kw, n.phases, n.voltage_min_v, n.voltage_max_v, n.capacity_raw,
           n.rated_current_a, n.duty_cycle_pct, n.weight_kg, n.lubricant, n.grease_interval_h,
           CASE WHEN n.asset_tag IS NULL OR m.model IS NULL THEN NULL
                WHEN LOWER(TRIM(m.model)) IN (LOWER(TRIM(CONCAT_WS(' ', n.manufacturer, n.model))),
                                              LOWER(TRIM(n.model))) THEN 1 ELSE 0 END
             AS model_agrees,
           CASE WHEN n.mfg_month IS NULL OR m.year_installed IS NULL THEN NULL
                WHEN CAST(LEFT(n.mfg_month, 4) AS UNSIGNED) <= m.year_installed THEN 1 ELSE 0 END
             AS made_by_install_year,
           n.raw_id
    FROM {machines} m
    LEFT JOIN {nameplates} n ON n.asset_tag = m.machine_id
    """),
    # One row per machine and current SOP; a machine no SOP names keeps one row with
    # has_sop = 0, so the app says "no SOP in the data covers it" (trap list D3). The
    # nested join applies is_current before the LEFT JOIN: a machine named only in a
    # superseded revision counts as having no SOP, not as a row with a NULL title.
    "machine_sops": (("machines", "sops", "sop_machines"), """
    CREATE OR REPLACE VIEW machine_sops AS
    SELECT m.machine_id, m.machine_name,
           (s.doc_no IS NOT NULL) AS has_sop,
           s.doc_no, s.revision, s.title, s.rev_label, s.rev_month, s.step_count,
           sm.how AS linked_by, sm.evidence AS sop_words,
           s.raw_id
    FROM {machines} m
    LEFT JOIN ({sop_machines} sm
               JOIN {sops} s ON s.doc_no = sm.doc_no AND s.revision = sm.revision
                            AND s.is_current = 1)
           ON sm.machine_id = m.machine_id
    """),
    "incident_report_vs_cmms": (("incident_reports", "safety_incidents"), """
    CREATE OR REPLACE VIEW incident_report_vs_cmms AS
    SELECT x.incident_id, x.report_no, x.field, x.form_value, x.logged_value,
           CASE WHEN NOT x.incident_logged THEN NULL
                WHEN x.form_value IS NULL OR x.logged_value IS NULL THEN NULL
                WHEN x.field = 'severity_scale' AND x.logged_value = 'ambiguous' THEN NULL
                WHEN LOWER(TRIM(x.form_value)) = LOWER(TRIM(x.logged_value)) THEN 1 ELSE 0 END
             AS agrees,
           x.incident_logged, x.raw_id
    FROM (
""" + _REPORT_UNION + """
    ) x
    """),
}


def render_view(name: str, names: Names, window_days: int = 7) -> str:
    """The CREATE VIEW statement for `name` with real table names filled in."""
    if not isinstance(window_days, int) or not 1 <= window_days <= 365:
        raise ValueError(f"window_days must be 1-365, got {window_days!r}")
    _, sql = VIEWS[name]
    return sql.format(**names.mapping(), window_days=window_days)


# ---------------------------------------------------------------- ER diagram ---

def to_mermaid(names: Names, enforced: set[str]) -> str:
    """Mermaid erDiagram of the cleaned schema. Solid line = FOREIGN KEY, dotted = soft."""
    lines = ["erDiagram",
             "    %% solid line = FOREIGN KEY in MySQL; dotted = soft (joined in views,",
             "    %% checked for orphans by scripts/relationships.py, not enforced)"]
    for rel in RELATIONSHIPS:
        line = "--" if rel.name in enforced else ".."
        lines.append(f'    {names[rel.parent]} {rel.parent_end}{line}o{{ {names[rel.child]} '
                     f': "{rel.meaning}"')
    for logical, cols in ENTITIES.items():
        lines.append(f"    {names[logical]} {{")
        for typ, col, keys, comment in cols:
            extra = f" {keys}" if keys else ""
            extra += f' "{comment}"' if comment else ""
            lines.append(f"        {typ} {col}{extra}")
        lines.append("    }")
    return "\n".join(lines) + "\n"


def mermaid_doc(names: Names, enforced: set[str], today: date | None = None) -> str:
    today = today or date.today()
    return (
        "# ER diagram: cleaned schema\n\n"
        f"Generated by `scripts/relationships.py` on {today.isoformat()}. Solid lines are "
        "FOREIGN KEYs in MySQL; dotted lines are soft relationships, joined in the views "
        "and checked for orphans on every run, but not enforced.\n\n"
        f"```mermaid\n{to_mermaid(names, enforced)}```\n\n"
        "Lineage is left out: every cleaned table links to its `*_raw` table by `raw_id`, "
        "each raw row to `ingest_runs` by `run_id`, and the `*_audit` views join the three.\n"
    )


# ------------------------------------------------------------------ database ---

CATALOG_DDL = f"""
CREATE TABLE IF NOT EXISTS {CATALOG} (
    name           VARCHAR(40)  NOT NULL PRIMARY KEY,
    child_table    VARCHAR(64)  NOT NULL,
    child_column   VARCHAR(64)  NOT NULL,
    parent_table   VARCHAR(64)  NOT NULL,
    parent_column  VARCHAR(64)  NOT NULL,
    meaning        VARCHAR(100) NOT NULL,
    enforced       VARCHAR(4)   NOT NULL COMMENT 'fk = FOREIGN KEY in MySQL | soft = checked only',
    child_rows     INT          NULL COMMENT 'NULL = table not loaded',
    matched_rows   INT          NULL,
    null_rows      INT          NULL,
    wildcard_rows  INT          NULL COMMENT 'e.g. spares listed for ALL machines',
    orphan_rows    INT          NULL COMMENT 'child rows whose key has no parent row',
    orphan_values  VARCHAR(500) NULL COMMENT 'orphan keys with row counts',
    ambiguous_keys VARCHAR(255) NULL COMMENT 'parent keys that occur more than once',
    note           VARCHAR(255) NULL,
    checked_at     DATETIME     NOT NULL,
    run_id         BIGINT       NULL COMMENT 'the ingest_runs row that wrote this',
    CONSTRAINT fk_{CATALOG}_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# A catalog made before run_id existed gets the column added.
CATALOG_ADD_RUN_ID = f"""
ALTER TABLE {CATALOG}
    ADD COLUMN run_id BIGINT NULL COMMENT 'the ingest_runs row that wrote this',
    ADD CONSTRAINT fk_{CATALOG}_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
        ON DELETE SET NULL
"""


def existing_tables(conn) -> set[str]:
    from sqlalchemy import text

    return {t.lower() for (t,) in conn.execute(text(
        "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_TYPE = 'BASE TABLE'"))}


def existing_foreign_keys(conn) -> list[tuple[str, str, str, str, str]]:
    """(constraint, table, column, referenced table, referenced column), lower-case."""
    from sqlalchemy import text

    rows = conn.execute(text(
        "SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, "
        "REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL"))
    return [tuple(str(v).lower() for v in r) for r in rows]


def enforced_names(fks: list[tuple], names: Names) -> set[str]:
    """Relationships that have a FOREIGN KEY on exactly their columns, whoever made it.
    A composite key counts for each of its columns (KEY_COLUMN_USAGE has one row per
    column): fk_sop_machines on (doc_no, revision) enforces sop_doc."""
    have = {fk[1:] for fk in fks}
    return {r.name for r in RELATIONSHIPS
            if (names[r.child].lower(), r.child_col, names[r.parent].lower(), r.parent_col)
            in have}


def key_counts(conn, table: str, column: str) -> dict:
    from sqlalchemy import text

    return {k: int(n) for k, n in conn.execute(
        text(f"SELECT `{column}`, COUNT(*) FROM `{table}` GROUP BY `{column}`"))}


def run_checks(conn, names: Names, tables: set[str]) -> dict[str, Check | None]:
    out = {}
    for rel in RELATIONSHIPS:
        child, parent = names[rel.child], names[rel.parent]
        if child.lower() not in tables or parent.lower() not in tables:
            out[rel.name] = None
            continue
        out[rel.name] = check_relationship(key_counts(conn, child, rel.child_col),
                                           key_counts(conn, parent, rel.parent_col),
                                           rel.wildcard)
    return out


def drop_foreign_keys(engine) -> list[str]:
    """Drop the FOREIGN KEYs this script made (fk_rel_*), and only those."""
    from sqlalchemy import text

    dropped = []
    with engine.begin() as conn:
        for name, table, *_ in existing_foreign_keys(conn):
            if name.startswith(FK_PREFIX) and name not in dropped:
                conn.execute(text(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`"))
                dropped.append(name)
    return dropped


def add_foreign_keys(engine, names: Names, checks: dict, enforced: set[str]) -> list[str]:
    """Add a FOREIGN KEY for each relationship whose data allows it. Returns report lines."""
    from sqlalchemy import exc, text

    report = []
    for rel in RELATIONSHIPS:
        action, reason = plan_foreign_key(rel, checks.get(rel.name), rel.name in enforced)
        if action != "add":
            report.append(f"  {rel.name:<18} {action}" + (f": {reason}" if reason else ""))
            continue
        fk = f"{FK_PREFIX}{rel.name}"
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE `{names[rel.child]}` ADD CONSTRAINT `{fk}` "
                    f"FOREIGN KEY (`{rel.child_col}`) "
                    f"REFERENCES `{names[rel.parent]}` (`{rel.parent_col}`) "
                    f"ON DELETE RESTRICT ON UPDATE CASCADE"))
            report.append(f"  {rel.name:<18} added {fk}")
        except exc.DBAPIError as e:
            report.append(f"  {rel.name:<18} FAILED: {e.orig}")
    return report


def ensure_catalog(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:  # DDL commits implicitly in MySQL
        conn.execute(text(CATALOG_DDL))
        has_run_id = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = :t AND COLUMN_NAME = 'run_id'"), {"t": CATALOG}).scalar_one()
        if not has_run_id:
            conn.execute(text(CATALOG_ADD_RUN_ID))


def write_catalog(engine, names: Names, checks: dict, enforced: set[str],
                  run_id: int | None = None) -> None:
    from sqlalchemy import text

    ensure_catalog(engine)
    now = datetime.now().replace(microsecond=0)
    rows = []
    for rel in RELATIONSHIPS:
        c = checks.get(rel.name)
        rows.append({
            "name": rel.name, "child_table": names[rel.child], "child_column": rel.child_col,
            "parent_table": names[rel.parent], "parent_column": rel.parent_col,
            "meaning": rel.meaning, "enforced": "fk" if rel.name in enforced else "soft",
            "child_rows": c and c.rows, "matched_rows": c and c.matched,
            "null_rows": c and c.null_rows, "wildcard_rows": c and c.wildcard_rows,
            "orphan_rows": c and c.orphan_rows, "orphan_values": c and c.orphan_text(),
            "ambiguous_keys": (c and ", ".join(c.ambiguous)[:255]) or None,
            "note": rel.note if c else f"not checked: load {names[rel.child]} and "
                                       f"{names[rel.parent]} first",
            "checked_at": now, "run_id": run_id,
        })
    cols = list(rows[0])
    with engine.begin() as conn:  # replace the catalog in one transaction
        conn.execute(text(f"DELETE FROM {CATALOG}"))
        conn.execute(text(f"INSERT INTO {CATALOG} ({', '.join(cols)}) "
                          f"VALUES ({', '.join(':' + c for c in cols)})"), rows)


def create_views(engine, names: Names, tables: set[str], window_days: int) -> list[str]:
    from sqlalchemy import text

    report = []
    with engine.begin() as conn:
        for view, (needs, _) in VIEWS.items():
            missing = [names[t] for t in needs if names[t].lower() not in tables]
            if missing:
                report.append(f"  {view:<26} skipped: load {', '.join(missing)} first")
                continue
            conn.execute(text(render_view(view, names, window_days)))
            report.append(f"  {view:<26} created")
    return report


# ------------------------------------------------------------------- report ---

def print_checks(names: Names, checks: dict, enforced: set[str]) -> None:
    print("\nRelationships (child -> parent)")
    for rel in RELATIONSHIPS:
        c = checks.get(rel.name)
        tag = "fk" if rel.name in enforced else "soft"
        print(f"  {rel.name:<18} {names[rel.child]}.{rel.child_col} -> "
              f"{names[rel.parent]}.{rel.parent_col}  [{tag}]")
        if c is None:
            print("      not checked: a table is not loaded yet")
            continue
        wild = f", {rel.wildcard}={c.wildcard_rows}" if rel.wildcard else ""
        print(f"      rows={c.rows}  matched={c.matched}{wild}  null={c.null_rows}  "
              f"orphans={c.orphan_rows}")
        if c.orphans:
            print(f"      orphan keys: {c.orphan_text(200)}")
        if c.ambiguous:
            print(f"      parent key not unique: {', '.join(c.ambiguous[:10])}")


def print_plan(names: Names, window_days: int) -> None:
    print("Relationships (child -> parent):")
    for rel in RELATIONSHIPS:
        how = "FK if no orphans" if rel.can_enforce else "soft"
        print(f"  {rel.name:<18} {names[rel.child]}.{rel.child_col} -> "
              f"{names[rel.parent]}.{rel.parent_col}  [{how}]  {rel.note}")
    print(f"\nViews (work_order_sensor_window uses {window_days} days):")
    for view, (needs, _) in VIEWS.items():
        print(f"  {view:<26} needs {', '.join(names[t] for t in needs)}")


# --------------------------------------------------------------------- main ---

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cmms-prefix", default="cmms_",
                    help="table prefix used by ingest_cmms.py (default %(default)s)")
    ap.add_argument("--sensor-table", default="vibration_temp",
                    help="table name used by ingest_logs.py (default %(default)s)")
    ap.add_argument("--image-prefix", default="img_",
                    help="table prefix used by ingest_images.py (default %(default)s)")
    ap.add_argument("--window-days", type=int, default=7,
                    help="days of sensor readings before each work order (default %(default)s)")
    ap.add_argument("--mermaid", metavar="PATH", help="also write the ER diagram (Markdown) here")
    fk = ap.add_mutually_exclusive_group()
    fk.add_argument("--foreign-keys", action="store_true",
                    help="add FOREIGN KEYs where the data has no orphans")
    fk.add_argument("--drop-foreign-keys", action="store_true",
                    help="drop the FOREIGN KEYs this script added (do this before reloading)")
    add_common_args(ap)
    a = ap.parse_args()

    try:
        names = Names(a.cmms_prefix, a.sensor_table, a.image_prefix)
        names.mapping()
        render_view("work_order_sensor_window", names, a.window_days)
    except ValueError as e:
        sys.exit(str(e))

    if a.dry_run:
        print_plan(names, a.window_days)
        enforced = {"spare_part", "sop_doc"}  # made by ingest_spares.py and ingest_sops.py
        if a.mermaid:
            Path(a.mermaid).parent.mkdir(parents=True, exist_ok=True)
            Path(a.mermaid).write_text(mermaid_doc(names, enforced), encoding="utf-8")
            print(f"\nER diagram written to {a.mermaid}")
        print("Dry run - nothing written to the database.")
        return

    try:
        cfg = load_db_config(a.env_file)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    engine = cfg.engine()
    print(f"Linking tables in {cfg.describe()}")

    # Read-only first: the checks give the fingerprint the run is recorded with.
    with engine.connect() as conn:
        tables = existing_tables(conn)
        checks = run_checks(conn, names, tables)
    totals = run_totals(checks)

    run_id = start_run(engine, "relationships.py", SOURCE_LABEL,
                       sha256=fingerprint(names, checks))
    try:
        if a.drop_foreign_keys:
            dropped = drop_foreign_keys(engine)
            print(f"Dropped {len(dropped)} foreign key(s): {', '.join(dropped) or '-'}")
        with engine.connect() as conn:
            enforced = enforced_names(existing_foreign_keys(conn), names)

        if a.foreign_keys:
            print("\nForeign keys:")
            print("\n".join(add_foreign_keys(engine, names, checks, enforced)))
            with engine.connect() as conn:
                enforced = enforced_names(existing_foreign_keys(conn), names)
            print("Reloading a source now needs --drop-foreign-keys first.")

        write_catalog(engine, names, checks, enforced, run_id)
        print_checks(names, checks, enforced)
        print("\nViews:")
        print("\n".join(create_views(engine, names, tables, a.window_days)))
        print(f"\nCatalog written to {CATALOG}.")
    except BaseException as e:
        finish_run(engine, run_id, ok=False, **totals, error=f"{type(e).__name__}: {e}")
        print(f"Failed. Recorded as run {run_id} in ingest_runs.", file=sys.stderr)
        raise
    finish_run(engine, run_id, ok=True, **totals)
    print(f"Recorded as run {run_id} in ingest_runs.")

    if a.mermaid:
        Path(a.mermaid).parent.mkdir(parents=True, exist_ok=True)
        Path(a.mermaid).write_text(mermaid_doc(names, enforced), encoding="utf-8")
        print(f"ER diagram written to {a.mermaid}")

    total = totals["rows_rejected"]
    print(f"Done. {total} orphan row(s) in total"
          + (" - see orphan_values in table_relationships." if total else "."))


if __name__ == "__main__":
    main()