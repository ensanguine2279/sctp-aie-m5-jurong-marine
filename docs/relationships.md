# relationships.py: linking the sources

`scripts/relationships.py` ties the cleaned tables of the three sources together. Run it
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

Every run checks each relationship (rows, matched, NULL, orphans, parent keys that are
not unique), prints the result and writes it to the **`table_relationships`** catalog.
The agent can read the catalog to find join paths; the data quality report can quote
the orphan counts. Keys are compared the way MySQL does (case-insensitive, trimmed).

## Views

| View | What it gives |
|---|---|
| `machine_overview` | One row per machine: work orders, breakdowns, open WOs, incidents, lost days, spares, spares below min, sensor readings and their date range |
| `machine_spares` | Spares per machine, with `ALL` expanded to every machine |
| `work_order_detail` | Work orders + machine name/model + employee trade/shift |
| `incident_detail` | Incidents + machine + employee trade/shift; severity as `severity_5_worst` only |
| `work_order_sensor_window` | Each work order with readings in the `--window-days` (default 7) up to its date: count, vibration avg/max, temperature avg/max |

Orphans are kept, not dropped: the views LEFT JOIN and flag them with `machine_known` /
`employee_known`. `emp_name` is never selected (PDPA). An `emp_code` that appears twice in
`cmms_employees` gets no trade/shift instead of duplicating the work order. Each detail view
keeps `raw_id`, so an answer can be cited through the matching `*_audit` view.

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
```

## Tests

`tests/test_relationships.py` (no database): orphan/NULL/wildcard counting, MySQL-style key
comparison, when a FK is added or skipped, table prefixes, no view exposes `emp_name`, the
incident view offers only `severity_5_worst`, the Mermaid output, the run fingerprint and
totals, and that both a successful and a failed run are recorded. The test for
`start_run` without a file is in `tests/test_shared.py` with the other `lineage.py` tests. Not covered: the SQL
against a real MySQL. After changing a view, run the script on a loaded database and query it.