# pipeline.py: run everything, report the results

`scripts/pipeline.py` runs every ingest script, then `relationships.py` (and optionally
`agent_user.py`), and writes the results to a Markdown report: one table row per step,
with the numbers you'd otherwise collect from four terminal outputs.

```bash
python scripts/pipeline.py --dry-run                      # check everything; nothing written to the database
python scripts/pipeline.py --recreate                     # first load (drops and recreates the tables)
python scripts/pipeline.py                                # later loads
python scripts/pipeline.py --foreign-keys --agent check   # with foreign keys, then check the app user
```

With uv (personal repo): `uv run python scripts/pipeline.py ...`.

## What it runs

| # | Step | Command | When |
|---|---|---|---|
| 0 | Drop foreign keys | `relationships.py --drop-foreign-keys` | only with `--foreign-keys` (real loads) |
| 1 | Sensor logs | `ingest_logs.py data/logs/vibration_temp_2026H1.csv` | always |
| 2 | Critical spares | `ingest_spares.py data/tabular/critical_spares.xlsx` | always |
| 3 | CMMS dump | `ingest_cmms.py data/sql/jurongmarine_cmms.sql` | always |
| 4 | Links between sources | `relationships.py --mermaid docs/er_diagram.md` (`--foreign-keys` with the option) | always |
| 5 | App user (PDPA) | `agent_user.py --check`, or the full setup | only with `--agent check` / `--agent setup` |

Each step runs as its own process, exactly as if you typed it, with the same Python (so
`uv run` and an activated `.venv` both work). Its output streams to the terminal as usual.
`--dry-run`, `--recreate` and `--env-file` are passed on to the scripts that take them.

**The first failed step stops the run.** The steps after it are listed as "not run", so
`relationships.py` never links half-loaded data. Each ingest script rolls back its own
failed load, so the database still holds the previous load of that source, except with
`--recreate`, where the tables were dropped before the load started.
`--keep-going` runs the remaining steps anyway. **Ctrl+C** stops the current step and
still writes the report, marked "interrupted".

## Options

| Option | Default | What it does |
|---|---|---|
| `--dry-run` | off | Every script in dry-run mode; nothing is written to the database |
| `--recreate` | off | Passed to the ingest scripts: drop and recreate their tables |
| `--foreign-keys` | off | Drop `relationships.py`'s foreign keys before loading, add them back after |
| `--agent check` / `--agent setup` | off | Also run `agent_user.py --check`, or create/update the app user and check it |
| `--only STEPS` | all | Comma-separated: `logs`, `spares`, `cmms`, `relationships`, `agent_user`. Run in pipeline order whatever order you type |
| `--logs` / `--spares` / `--cmms PATH` | the paths above | Input files, e.g. `--logs data/logs/vibration_temp_sample.csv` |
| `--mermaid PATH` / `--no-mermaid` | `docs/er_diagram.md` | Where `relationships.py` writes the ER diagram, or don't |
| `--keep-going` | off | Run the remaining steps after a failure |
| `--report PATH` | `reports/pipeline_<date>-<time>.md` | Report file (`_dry-run` added for dry runs) |
| `--env-file PATH` | nearest `.env` | Passed to every script |

Exit code: 0 if every step succeeded, 1 otherwise, so it can run in CI.

## The report

| Section | What's in it |
|---|---|
| Header | result, start and finish time, mode and options, database (`user@host:port/db`, password hidden), git commit, and a warning if the code has uncommitted changes |
| Steps | one row per step: status, time, `run_id`, rows read / loaded / rejected |
| Input files | each input's size and SHA-256, and for a load whether it is **the same file** `ingest_runs` recorded |
| Links between sources | after a load: `table_relationships`, i.e. each link's rows, matched, orphans and orphan keys |
| Details | each step's command, its summary lines (breakdowns by category, rejects, warnings, the severity rule used), and the last 20 lines of error output if it failed |

**Where the numbers come from:** for a load, from each run's own row in `ingest_runs`,
so the report matches the database's record of that load. For a dry run, from the
scripts' printed summaries. For `relationships.py`, the three counts are rows checked,
matched and orphans.

**Reproducibility:** the header's git commit and the input files' SHA-256 are what you
need to re-run a load later and compare (see the README's Lineage section). If the header
says "with uncommitted changes", commit and run again before a load that matters.

## Personal data (PDPA)

The report never copies a script's full output: only summary lines that match a fixed
list of patterns (counts and categories). A dry run of `ingest_cmms.py` prints sample
employee rows, names included; those stay in the terminal. Only the last lines of a
failed step's error output go in verbatim; database errors don't include row values
(`hide_parameters` in `db_config.py`).

Reports hold row counts, file hashes, orphan keys (machine ids, employee codes) and the
database host. That's fine to commit as audit evidence; leave out reports from a hosted
database if you'd rather not publish its host name.

## Tests

`tests/test_pipeline.py` (no database, a fake runner stands in for the scripts): the plan's
order and flags (foreign keys dropped first and added last, never in a dry run; `--only`),
summary lines kept and sample rows with names dropped, counts read from each script's
summary format, `run_id` found in normal and error output, a failure stopping the rest,
`--keep-going`, Ctrl+C, the report's tables (hash match, uncommitted code, escaped pipes),
and `main` writing the report and returning 1 on failure. One test runs a real process to
check that both output streams are captured.

**Not covered:** the real scripts and database. The first `pipeline.py --dry-run` on your
data, then a real load, are the real test.