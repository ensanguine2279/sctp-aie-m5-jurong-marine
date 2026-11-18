# pipeline.py: run everything, report the results

`ingest/pipeline.py` runs every ingest script, then `relationships.py` (and optionally
`agent_user.py`), and writes the results to a Markdown report: one table row per step,
with the numbers you'd otherwise collect from six terminal outputs.

```bash
python ingest/pipeline.py --dry-run                      # check everything; nothing written to the database
python ingest/pipeline.py --recreate                     # first load (drops and recreates the tables)
python ingest/pipeline.py                                # later loads
python ingest/pipeline.py --foreign-keys --agent check   # with foreign keys, then check the app user
python ingest/pipeline.py --only sops,relationships      # reload the SOPs and re-link
python ingest/pipeline.py --save-log                     # also keep the full on-screen log
python ingest/pipeline.py --dry-run --audit              # a new sensor file: profile it first
python ingest/pipeline.py --image-engine both            # read the scans with Tesseract and the model
```

**Which engine reads the scans:** `--image-engine`, else `IMAGE_ENGINE` in `.env` (or the
shell), else Tesseract alone. Put `IMAGE_ENGINE=both` in `.env` so every run stores the
two-engine values ([`ingest_images.md`](ingest_images.md#two-engines---engine-both)). The
report's header says which engine ran (`Image engine`). Reads are cached, so a reload with
`both` costs model budget only for scans or prompts the cache hasn't seen.

With uv (personal repo): `uv run python ingest/pipeline.py ...`.

The images step needs Tesseract installed ([Setup](ingest_images.md#setup-tesseract)). Without
it the step fails with install instructions and, as for any failed step, the steps after it
don't run; `--only logs,spares,cmms,sops,relationships` skips it.

## What it runs

| # | Step | Command | When | Details |
|---|---|---|---|---|
| 1 | Sensor CSV audit | `audit_sensor_csv.py data/logs/vibration_temp_2026H1.csv` | only with `--audit`; first, before anything is written | [below](#profiling-a-new-sensor-file---audit), [`trap_list.md`](trap_list.md#sensor-log-vibration_temp_2026h1csv) |
| 2 | Drop foreign keys | `relationships.py --drop-foreign-keys` | only with `--foreign-keys` (real loads) | [Foreign keys](relationships.md#foreign-keys) |
| 3 | Sensor logs | `ingest_logs.py data/logs/vibration_temp_2026H1.csv` | always | [`ingest_logs.md`](ingest_logs.md) |
| 4 | Critical spares | `ingest_spares.py data/tabular/critical_spares.xlsx` | always | [`ingest_spares.md`](ingest_spares.md) |
| 5 | CMMS dump | `ingest_cmms.py data/sql/jurongmarine_cmms.sql` | always | [`ingest_cmms.md`](ingest_cmms.md) |
| 6 | Scanned images | `ingest_images.py data/images` | always; after the CMMS so machine ids can be checked | [`ingest_images.md`](ingest_images.md) |
| 7 | SOP documents | `ingest_sops.py data/sops` | always; after the CMMS so CMMS machine names can be matched | [`ingest_sops.md`](ingest_sops.md) |
| 8 | Links between sources | `relationships.py --mermaid docs/er_diagram.md` (`--foreign-keys` with the option) | always; after the SOPs, so `machine_sops` is rebuilt over the new tables | [`relationships.md`](relationships.md), [ER diagram](er_diagram.md) |
| 9 | App user (PDPA) | `agent_user.py --check`, or the full setup | only with `--agent check` / `--agent setup` | [`agent_user.md`](agent_user.md) |

Each step runs as its own process, exactly as if you typed it, with the same Python (so
`uv run` and an activated `.venv` both work). Its output streams to the terminal as usual.
`--dry-run`, `--recreate` and `--env-file` are passed on to the scripts that take them.

**Grants survive `--recreate`.** MySQL doesn't remove a table's grants when the table is
dropped, and they apply again to the new table of the same name, so the app user can still
read `sops` and the rest after a recreate. Run `--agent setup` when the allow-list itself
has changed (a new view such as `machine_sops`), and `--agent check` to prove it.

**The first failed step stops the run.** The steps after it are listed as "not run", so
`relationships.py` never links half-loaded data. Each ingest script rolls back its own
failed load, so the database still holds the previous load of that source, except with
`--recreate`, where the tables were dropped before the load started (see Load behaviour
in [`ingest_logs.md`](ingest_logs.md), [`ingest_spares.md`](ingest_spares.md),
[`ingest_cmms.md`](ingest_cmms.md) and [`ingest_images.md`](ingest_images.md)).
`ingest_sops.py` also stops, changing nothing, if its `sops` table predates the
`search_text` column; run once with `--recreate` ([`ingest_sops.md`](ingest_sops.md)).
`--keep-going` runs the remaining steps anyway. **Ctrl+C** stops the current step and
still writes the report, marked "interrupted".

## Profiling a new sensor file (`--audit`)

`--audit` runs `ingest/audit_sensor_csv.py` on the sensor CSV, the same file the sensor
load uses (`--logs`), as the very first step. It profiles the whole file without loading
anything: file encoding and line endings, timestamp formats and range, machine ids not in
the CMMS, temperature units per machine, out-of-range values, duplicates, and gaps per
machine, each with CSV line numbers. These are the L-items of the
[trap list](trap_list.md#sensor-log-vibration_temp_2026h1csv).

Use it when a **new sensor file arrives**, before it replaces the loaded data, best with a
dry run first:

```bash
python ingest/pipeline.py --dry-run --audit --save-log
```

It isn't on by default: a file that's already been audited gains nothing from being profiled
on every load, and it adds about 20–40 seconds. It reads the file only, so it gets no
`--dry-run` or `--env-file`, and writes nothing to `ingest_runs`.

**Read its findings; its status doesn't judge the file.** The audit reports, it doesn't
fail: "ok" means it ran, not that the file is clean. Its findings are in the report under
Details, **in full**, unlike the other steps, because the sensor file holds no personal data.
Compare them with the trap list's L-items: a new unit, a machine id the CMMS doesn't know,
longer gaps or a new timestamp format is a new trap to record. In the Steps table, Read is
the rows it profiled; Loaded and Rejected stay empty. The CSV appears once under Input
files, under the sensor load, so its hash can still be checked against the load.

`--only audit` runs just the audit; `--only audit,logs` the audit and the sensor load.

## Options

| Option | Default | What it does |
|---|---|---|
| `--dry-run` | off | Every script in dry-run mode; nothing is written to the database |
| `--recreate` | off | Passed to the ingest scripts: drop and recreate their tables |
| `--foreign-keys` | off | Drop `relationships.py`'s foreign keys before loading, add them back after ([why](relationships.md#foreign-keys)) |
| `--audit` | off | First profile the sensor CSV with `audit_sensor_csv.py` ([see above](#profiling-a-new-sensor-file---audit)) |
| `--image-engine tesseract\|llm\|both` | `IMAGE_ENGINE` in `.env`, else `tesseract` | Passed to `ingest_images.py` as `--engine` |
| `--agent check` / `--agent setup` | off | Also run `agent_user.py --check`, or create/update the app user and check it ([`agent_user.md`](agent_user.md#the-check)) |
| `--only STEPS` | all | Comma-separated: `audit`, `logs`, `spares`, `cmms`, `images`, `sops`, `relationships`, `agent_user`. Run in pipeline order whatever order you type |
| `--logs` / `--spares` / `--cmms` / `--images` / `--sops PATH` | the paths above | Inputs, e.g. `--logs data/logs/vibration_temp_sample.csv`; `--images` and `--sops` are folders. The audit uses `--logs` |
| `--mermaid PATH` / `--no-mermaid` | `docs/er_diagram.md` | Where `relationships.py` writes the ER diagram, or don't |
| `--keep-going` | off | Run the remaining steps after a failure |
| `--report PATH` | `reports/pipeline_<date>-<time>.md` | Report file (`_dry-run` added for dry runs) |
| `--save-log` | off | Also save everything shown on screen to `logs/<report name>.log` next to the report, i.e. `reports/logs/` by default ([see below](#the-full-log)) |
| `--env-file PATH` | nearest `.env` | Passed to every script |

Exit code: 0 if every step succeeded, 1 otherwise, so it can run in CI.

## The report

| Section | What's in it |
|---|---|
| Header | result, start and finish time, mode and options, database (`user@host:port/db`, password hidden), git commit, a warning if the code has uncommitted changes, the **image engine** the images step used (from its `OCR engine:` line, e.g. `both:tesseract-v3+llm-v3:gpt-4o-mini=42`), and with `--save-log` a link to the full log |
| Steps | one row per step: status, time, `run_id`, rows read / loaded / rejected (for the images, one row = one image; for the SOPs, one file, with the GM's email the expected reject; for the audit, Read only: rows profiled) |
| Input files | each input's size and SHA-256, and for a load whether it is **the same file** `ingest_runs` recorded. A folder gets `lineage.folder_sha256` over the files its script reads (images for `ingest_images.py`, `.txt` and `.md` for `ingest_sops.py`), the same hash that script records, so a stray file of another type doesn't make the check say NO. A file two steps read (the audit and the sensor load) is listed once |
| Links between sources | after a load: `table_relationships`, i.e. each link's rows, matched, orphans and orphan keys, including `sop_machine` and `sop_doc` ([`relationships.md`](relationships.md#relationships)) |
| Details | each step's command, its summary lines (breakdowns by category, rejects, warnings, the severity rule used; for the images the photos stored for the app and any form with no photo; for the SOPs one line per SOP with its revision, step count and machines), and the last 20 lines of error output if it failed. The audit's findings appear in full. What each line means: Run summary in [`ingest_logs.md`](ingest_logs.md#run-summary), [`ingest_spares.md`](ingest_spares.md#run-summary), [`ingest_cmms.md`](ingest_cmms.md#run-summary), [`ingest_images.md`](ingest_images.md#run-summary), [`ingest_sops.md`](ingest_sops.md#run-summary) |

**Where the numbers come from:** for a load, from each run's own row in `ingest_runs`,
so the report matches the database's record of that load. For a dry run, from the
scripts' printed summaries. For `relationships.py`, the three counts are rows checked,
matched and orphans.

**Reproducibility:** the header's git commit and the input files' SHA-256 are what you
need to re-run a load later and compare (see
[Lineage](ingestion.md#lineage-traceability-and-reproducibility) in `ingestion.md`). If the header
says "with uncommitted changes", commit and run again before a load that matters.

## The full log

Without `--save-log`, the scripts' full output only goes to the terminal. With it, the same
output is also written to `reports/logs/pipeline_<date>-<time>.log`, with the same name as
the report, and the report's header links to it. Use it to debug a failed step when the
report's last 20 lines of error output aren't enough, or to compare two runs line by line.

- **Everything, in order:** a header line per step (`=== [2/6] Critical spares: python
  ingest/ingest_spares.py ...`), every line the step printed, then its outcome
  (`--- failed, exit code 1, 0.4s`). Lines from the error stream start with `[stderr]`.
- **Written as it goes**, not at the end: if a step hangs or you press Ctrl+C, the log
  shows how far it got.
- **Kept out of git:** the script puts a `.gitignore` in `reports/logs/` that ignores
  everything there, so logs can't be committed by accident. The file is readable only by
  you (permissions `600`, on Linux and macOS).

**Names in the log are masked.** A dry run of `ingest_cmms.py` prints sample employee
rows, but the script masks names before printing them: only the last 4 characters show
(`**** Hui`); see [Personal data on screen](ingest_cmms.md#personal-data-on-screen). The log
still holds employee codes with trade and shift, which in a 32-person shop can point to a
person, so keep it private: don't attach a log to the data quality report or share it;
share the report instead. Delete logs you no longer need.

## Personal data (PDPA)

The report never copies a script's full output: only summary lines that match a fixed
list of patterns (counts and categories). The one exception is the sensor audit, whose
findings are copied in full: the sensor file has machine readings only, no personal data.
The full output of every step is only kept with `--save-log`, in a git-ignored log file
([The full log](#the-full-log)). Employee names never appear in full in either, or on
screen: `ingest_cmms.py` masks them before printing, to their last 4 characters
([Personal data on screen](ingest_cmms.md#personal-data-on-screen)), and `ingest_images.py`
prints only employee codes. `ingest_sops.py` prints a rejected file only by name and
reason, and the report keeps neither: the GM's email appears in it only as
`not_an_sop=1`. Only the last lines of a failed step's error output go into the report
verbatim; database errors don't include row values (`hide_parameters` in `db_config.py`).
How the app is kept away from names in the database is in [`agent_user.md`](agent_user.md).

Reports hold row counts, file hashes, orphan keys (machine ids, employee codes) and the
database host. That's fine to commit as audit evidence; leave out reports from a hosted
database if you'd rather not publish its host name.

## Tests

`tests/test_pipeline.py` (44 tests, no database, a fake runner stands in for the scripts):
the plan's order and flags (foreign keys dropped first and added last, never in a dry run;
`--only`; images after the CMMS; the SOPs after the CMMS and before the linking), summary
lines kept and sample rows with names dropped (for the images, and for the SOPs: one line
per SOP and the counts kept, the step table and the rejected email's name dropped),
counts read from each script's summary format, the images and SOP folders hashed the same
way their loads record them (a stray photo among the SOPs doesn't count), `run_id` found
in normal and error output, a failure stopping the rest (a failed SOP load stops before
the linking), `--keep-going`, Ctrl+C, the report's tables (hash match, uncommitted code,
escaped pipes), `main` writing the report, passing a SOP folder and returning 1 on
failure, `--save-log` (both streams logged as they arrive, a header and outcome per step,
the log git-ignored and readable only by you, linked from the report, with names in the
log but not in the report), and `--audit` (off by default, first and before any write,
given only the file, its findings kept in full with the rows it profiled, the shared CSV
listed once under the sensor load), and the images: `--image-engine` passed to the images
step only, the photo lines kept, the engine in the report header. Two tests run a real process to check that both
output streams are captured and logged.

**Not covered:** the real scripts and database. The first `pipeline.py --dry-run` on your
data, then a real load, are the real test.