# relationships.py: linking the sources

`scripts/relationships.py` ties the cleaned tables of the four sources together. Run it
after the ingest scripts; it is safe to run after every load.

```bash
python scripts/relationships.py --dry-run --mermaid docs/er_diagram.md   # no database needed
python scripts/relationships.py                                          # check + catalog + views
python scripts/relationships.py --foreign-keys                           # also enforce where clean
python scripts/relationships.py --drop-foreign-keys                      # before reloading a source
```

With uv (personal repo): `uv run python scripts/relationships.py ...`.

## Relationships

| Name | Child -> parent | Kind |
|---|---|---|
| `wo_machine` | `cmms_work_orders.machine_id` -> `cmms_machines.machine_id` | FK if no orphans |
| `incident_machine` | `cmms_safety_incidents.machine_id` -> `cmms_machines.machine_id` | FK if no orphans |
| `reading_machine` | `vibration_temp.machine_id` -> `cmms_machines.machine_id` | FK if no orphans |
| `spare_machine` | `critical_spare_machines.machine_code` -> `cmms_machines.machine_id` | soft: `ALL` means every machine |
| `spare_part` | `critical_spare_machines.part_no` -> `critical_spares.part_no` | FK, made by `ingest_spares.py` |
| `wo_employee` | `cmms_work_orders.emp_code` -> `cmms_employees.emp_code` | soft: `emp_code` has no unique key |
| `incident_employee` | `cmms_safety_incidents.emp_code` -> `cmms_employees.emp_code` | soft: `emp_code` has no unique key |
| `nameplate_machine` | `img_nameplates.asset_tag` -> `cmms_machines.machine_id` | FK if no orphans |
| `report_incident` | `img_incident_reports.incident_id` -> `cmms_safety_incidents.incident_id` | FK if no orphans |
| `report_machine` | `img_incident_reports.machine_id` -> `cmms_machines.machine_id` | FK if no orphans |

Every run checks each relationship (rows, matched, NULL, orphans, parent keys that are
not unique), prints the result and writes it to the **`table_relationships`** catalog.
The agent can read the catalog to find join paths; the data quality report can quote
the orphan counts. Keys are compared the way MySQL does (case-insensitive, trimmed).
An orphan on the image links usually means OCR misread a tag or report number; the
readings are in `img_documents_raw.ocr_notes`. The image tables' prefix is `--image-prefix`
(default `img_`), like `--cmms-prefix`.

## Views

| View | What it gives |
|---|---|
| `machine_overview` | One row per machine: work orders, breakdowns, open WOs, incidents, lost days, spares, spares below min, sensor readings and their date range |
| `machine_spares` | Spares per machine, with `ALL` expanded to every machine |
| `work_order_detail` | Work orders + machine name/model + employee trade/shift |
| `incident_detail` | Incidents + machine + employee trade/shift; severity as `severity_5_worst` only |
| `work_order_sensor_window` | Each work order with readings in the `--window-days` (default 7) up to its date: count, vibration avg/max, temperature avg/max |
| `machine_nameplate` | Every CMMS machine with its nameplate photo's values (lubricant, grease interval, power, weight, serial, ...); `has_nameplate = 0` when there's no photo, so the app says so instead of guessing; `model_agrees` (plate vs CMMS model), `made_by_install_year` |
| `incident_report_vs_cmms` | One row per scanned form and field (`incident_date`, `machine_id`, `emp_code`, `description`, `severity_scale`, `severity_5_worst`, `lost_days`, `reporting`): `form_value`, `logged_value`, `agrees` (1 / 0, NULL when a side is missing, the form's incident isn't in the CMMS, or the CMMS scale is `ambiguous`) |

Orphans are kept, not dropped: the views LEFT JOIN and flag them with `machine_known` /
`employee_known` (`has_nameplate` / `incident_logged` for the scans). `emp_name` is never
selected (PDPA). An `emp_code` that appears twice in `cmms_employees` gets no trade/shift
instead of duplicating the work order. Each detail view keeps `raw_id`, so an answer can be
cited through the matching `*_audit` view.

`incident_report_vs_cmms` compares severity on the 5 = worst scale only (each side
converted with its own rule: the scale printed on the form, the date rule in the CMMS), so a
1-worst number is never compared with a 5-worst one. Every value is cast to text with one
explicit collation (`utf8mb4_unicode_ci`), so dates, numbers and strings compare the same way
and MySQL never mixes collations (error 1267).

## Foreign keys

`--foreign-keys` adds a real FOREIGN KEY (`fk_rel_<name>`, `ON DELETE RESTRICT`) only for
relationships that can have one and whose data has no orphans; the others are skipped with
the reason. The loads replace rows (reloading the CMMS dump deletes `cmms_machines` rows that
other tables would point at; `--recreate` drops tables), so a reload fails while these keys
exist. Order of work: `--drop-foreign-keys`, reload, then `--foreign-keys` again.
`--drop-foreign-keys` only drops `fk_rel_*` keys, never the ingest scripts' own.

## Run log

Every real run (not `--dry-run`) writes a row to `ingest_runs`, like a load:

| Column | Value |
|---|---|
| `script` / `source_file` | `relationships.py` / `(database)` |
| `file_sha256` | fingerprint of the check results: the same hash means the data looked the same |
| `rows_read` / `rows_loaded` / `rows_rejected` | child rows checked / matched / orphans |
| `status`, `error` | `success`, or `failed` with the error |

The checks run first (read-only), then the run is recorded, then the foreign keys,
catalog and views are written. Each `table_relationships` row carries the `run_id` that
wrote it; a catalog made before this change gets the column added on the next run.

## Queries

```sql
-- Relationship health (paste into the data quality report)
SELECT name, child_table, child_column, enforced, child_rows, matched_rows, orphan_rows, orphan_values
FROM table_relationships;

-- Is the night shift right about CNC-02? Sensor readings before each of its work orders
SELECT wo_id, wo_date, wo_type, description, readings, vib_avg, vib_max, temp_c_max
FROM work_order_sensor_window WHERE machine_id = 'CNC-02' AND readings > 0 ORDER BY wo_date;

-- Spares for a machine, and which are below minimum stock
SELECT part_no, description, qty, min_qty, below_min, lead_time_max_days
FROM machine_spares WHERE machine_id = 'CRN-01';

-- What oil and grease interval does a machine take, and is there a nameplate photo?
SELECT machine_id, has_nameplate, lubricant, grease_interval_h, model_agrees
FROM machine_nameplate WHERE machine_id = 'BLAST-01';

-- Where scanned forms and the CMMS disagree
SELECT incident_id, field, form_value, logged_value
FROM incident_report_vs_cmms WHERE agrees = 0 ORDER BY incident_id, field;
```

## Tests

`tests/test_relationships.py` (no database): orphan/NULL/wildcard counting, MySQL-style key
comparison (integer keys too), when a FK is added or skipped, table prefixes (including the
image prefix), no view exposes `emp_name`, the incident view and the form comparison offer
only `severity_5_worst`, the form comparison has one branch per field on one collation and
leaves July 2025 open, the nameplate view keeps machines without a photo, the Mermaid
output, the run fingerprint and totals, and that both a successful and a failed run are
recorded. The test for `start_run` without a file is in `tests/test_shared.py` with the
other `lineage.py` tests. Not covered: the SQL against a real MySQL. After changing a view,
run the script on a loaded database and query it.