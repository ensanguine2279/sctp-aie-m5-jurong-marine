# agent_user.py: the app's read-only database user

`scripts/agent_user.py` creates the MySQL user the app (and through its tools, the LLM)
connects as, and proves what that user can and can't read. It is the PDPA guardrail in
the database: telling the model "use the views" is not protection, the database has to
refuse.

```bash
python scripts/agent_user.py --dry-run   # the plan and SQL, password hidden; no database needed
python scripts/agent_user.py             # create/update the user, then check it
python scripts/agent_user.py --check     # only the check (exit 1 if anything is wrong)
```

With uv (personal repo): `uv run python scripts/agent_user.py ...`.

Run it after the loads and `relationships.py`: objects that don't exist yet are skipped,
not granted.

## What the app user can read

| Can read (`SELECT` only) | Can't |
|---|---|
| The linking views: `machine_overview`, `machine_spares`, `work_order_detail`, `incident_detail`, `work_order_sensor_window`, `machine_nameplate`, `machine_sops`, `incident_report_vs_cmms` | `cmms_employees`, `cmms_employees_raw`, `cmms_employees_audit` |
| `table_relationships` (join paths, orphan counts) | every `*_raw` table, including `img_documents_raw` (the OCR text of each scan, and a form's "person involved" exactly as read) and `sop_documents_raw` (every file in the SOP folder as read, including the GM's email with names and addresses) |
| The audit views for citations: `cmms_machines_audit`, `cmms_work_orders_audit`, `cmms_safety_incidents_audit`, `vibration_temp_audit`, `critical_spares_audit`, `img_nameplates_audit`, `img_incident_reports_audit` | the clean tables themselves (`vibration_temp`, `cmms_work_orders`, `img_nameplates`, ...) and `ingest_runs`, with two SOP exceptions (left) |
| The SOPs: `sops` (to find an SOP with `MATCH ... AGAINST`), `sops_audit`, `sop_steps_audit` (to quote it whole with its citation), `sop_machines` (which SOPs cover a machine) | `sop_ft_stopwords` (the index's word list, not for the app) |
| | any write, any schema change, granting itself more |

MySQL denies everything not granted, and every run drops the user and creates it again,
so nothing granted earlier survives: the 20 objects above are all the user can read.
(Dropping and recreating, rather than `REVOKE ALL`, also works on hosted MySQL, where
even the admin account is kept out of some databases.)

**Why views work without access to the tables:** MySQL runs a view with its creator's
rights (`SQL SECURITY DEFINER`, the default). The views were created by the ingest user,
so the app user reads `work_order_detail` without any access to `cmms_employees`
underneath. It also means a column added to a table stays invisible to the app until
someone puts it in a view.

**For the CNC-02 chart** (readings over time), use `vibration_temp_audit`: it has every
cleaned reading (`machine_id`, `sampled_at`, `vib_rms_mms`, `temp_c`) plus the source line
for the citation. `vibration_temp` itself is blocked.

**For the scans,** the audit views give the image file and its SHA-256 to cite, not the
OCR text. A form that names a person instead of an `EMP` code keeps that name only in
`img_documents_raw`, which the app can't read.

**For the SOPs,** two tables are granted directly, unlike the other sources. `sops`
because `MATCH ... AGAINST` needs the `FULLTEXT` index, which exists only on the table
(`sops_audit` has no `full_text`). `sop_machines` because it holds only doc numbers,
machine ids and the SOP words that link them. Neither holds personal data: the SOPs name
roles, not people, and `ingest_sops.py` flags a step with an email address or phone
number (`personal_data_in_text`). Rejected files, including the GM's email, stay in
`sop_documents_raw`, which is never granted and is probed by `--check`. See
[`ingest_sops.md`](ingest_sops.md) for how the app finds an SOP and quotes it.

**Names can't leak through a view either:** before granting, the script reads each
object's columns and refuses any that has `emp_name`. A view edited later to include
names is reported as `REFUSED`, not granted.

## Settings

The same `.env` as the other scripts, plus:

```bash
AGENT_DB_USERNAME=agent_ro         # the app connects with these two
AGENT_DB_PASSWORD=...
DB_ADMIN_USERNAME=root             # an account allowed to create users and grant
DB_ADMIN_PASSWORD=...              # falls back to MYSQL_ROOT_PASSWORD
```

- The **app** connects with `AGENT_DB_USERNAME` / `AGENT_DB_PASSWORD`, never `DB_USERNAME`.
- The **admin** account is only for running this script. The ingest user (`DB_USERNAME`)
  can't create users, so it won't do.
- Every run recreates the app user with the password from `.env`, so changing
  `AGENT_DB_PASSWORD` and re-running changes it. Connections the app already has open
  keep working until they close; reconnect the app after a run.
- `--host` (default `%`, any host) is where the app may connect from. Keep `%` with Docker:
  the port mapping makes local connections look remote to MySQL.
- `--cmms-prefix`, `--sensor-table` and `--image-prefix` follow the ingest scripts' table
  names, if you changed them. The SOP tables have no prefix.
- **Values already set in your shell win over `.env`.** If you once ran `source .env`, the
  shell keeps those values until you close it or `unset` them.
- **Windows line endings are handled.** Every setting is read through `db_config.getenv`,
  which removes a trailing `\r`. A `.env` saved on Windows (CRLF) is read correctly by the
  scripts, but `source .env` in a shell leaves `\r` on every value (`agent_ro\r`).
  Only CR/LF are removed, so a password that really starts or ends with a space is kept.

Docker's root password, if you don't remember it:

```bash
docker compose exec mysql sh -c 'echo "$MYSQL_ROOT_PASSWORD"'
```

## The check

The check connects **as the app user** and tries everything: each allowed object must
work, and each probe must be refused. Every probe is harmless even if it ran (the write
is `WHERE 1 = 0`; a table it managed to create would be dropped again).

```
Allowed (must work):
  ok      machine_spares
  ok      work_order_detail
  ...
  ok      sops
  ok      sops_audit
  ok      sop_steps_audit
  ok      sop_machines
Blocked (must fail):
  ok      employee names: denied
  ok      employees table: denied
  ok      employees audit view: denied
  ok      raw employee rows: denied
  ok      raw SOP files (the GM's email): denied
  ok      writing data: denied
  ok      changing the schema: denied

Passed: the app user reads only the allowed views; names, raw data, writes and schema changes are refused.
```

Any `FAIL` line exits with an error. It's also the evidence for the PDPA question on demo
day: run `--check` live.

**"Denied" doesn't say whether the object exists.** MySQL answers `SELECT command denied`
(1142) for any object the app user has no grant on, including one that doesn't exist, so
the app user can never see that something is missing. The check therefore asks the admin
account which allowed objects exist: after a setup run it uses the plan, and with
`--check` alone it connects as admin if `DB_ADMIN_PASSWORD` (or `MYSQL_ROOT_PASSWORD`) is
set. A denied object then shows as one of:

```
  skip    sops: not created yet                                        # load it, then re-run
  FAIL    sops: exists but not granted; run agent_user.py without --check to grant it
  FAIL    sops: denied (not granted, or not created yet; set DB_ADMIN_PASSWORD so --check can tell which)
```

On 4 Nov 2026 a `--check` showed the 8 image and SOP objects denied while the 11 older
ones passed: most likely the user still had the grants from an earlier setup run, made
before those objects were in the allow-list. The fix is a setup run. To see the grants directly:

```sql
SHOW GRANTS FOR 'agent_ro'@'%';   -- USAGE (connect only) + one SELECT line per allowed object
```

## When it doesn't work

| Message | Cause | Fix |
|---|---|---|
| `... is refused access to a database it would need` (MySQL 3879) | hosted MySQL keeps the admin out of some databases; older versions of this script used `REVOKE ALL`, which needs them | use the current script; if it still fails, the `Failed at` and `MySQL said` lines show which step and database |
| `... is not allowed to create users or grant` (MySQL 1227) | the admin account isn't an admin, e.g. `DB_ADMIN_USERNAME` is the ingest user | `DB_ADMIN_USERNAME=root` (Docker) or `avnadmin` (Aiven) with its password |
| `Login refused for ...` (MySQL 1045) | wrong admin user or password | check `DB_ADMIN_USERNAME` / `DB_ADMIN_PASSWORD` |
| `Set AGENT_DB_PASSWORD in .env` | no password for the app user | add it to `.env` |
| `Invalid user name: 'agent_ro\r'` (or another value ending in `\r`) | an older script, and a CRLF `.env` loaded with `source .env`, so the shell's values end in `\r` and win over `.env` | use the current scripts (they strip it); fix the file with `sed -i 's/\r$//' .env`, then open a new shell or `unset` the variables |
| `skip <object>: not created yet` | a source isn't loaded, or `relationships.py` hasn't run | load it, run `relationships.py`, then this script again |
| `FAIL <object>: exists but not granted` | the user's grants are from an older setup run (the allow-list has grown since), or the setup ran with an older script | run `agent_user.py` without `--check` |
| `FAIL <object>: denied (not granted, or not created yet ...)` | `--check` alone with no admin password, so it can't tell which | set `DB_ADMIN_PASSWORD`, or run `SHOW GRANTS FOR 'agent_ro'@'%'` as admin |
| `REFUSED <object>: has personal-data column(s) emp_name` | a view now selects names | take the column out of the view |

**Passwords never appear in errors:** every script's database connection hides query
values in error messages (`hide_parameters` in `db_config.py`), and this script replaces
setup errors with the messages above. If a password was ever shown in a traceback or
pasted somewhere, change it in `.env` and run the script again.

## Hosted MySQL (Aiven)

Use Aiven's admin account in place of `root`: `DB_ADMIN_USERNAME=avnadmin` and its
password from the Aiven console. Take the host and port from the console too (the port
is not 3306; the database is usually `defaultdb`).

**Don't create the app user in the Aiven console:** a user made there gets admin-level
privileges by default. Let this script create it with SQL; even if it already exists,
the script drops it and recreates it with `SELECT` on the allow-list only. Then run
`--check`.

Aiven requires encrypted connections; the scripts don't set up SSL yet, so expect to add
that before connecting to Aiven.

## Removing the user

There is no `--drop` option. As the admin account:

```sql
DROP USER IF EXISTS 'agent_ro'@'%';
```

This removes the user and its grants; views, tables and data are untouched. Connections
already open keep working until they close; new logins fail. Running `agent_user.py`
again recreates the user from `.env`.

## Tests

`tests/test_agent_user.py` (28 tests, no database): the allow-list never includes employees
or raw tables (nor the scans' raw OCR, nor the raw SOP files) and follows the ingest
prefixes, the scans' audit and comparison views granted, the SOP table and views granted,
objects with `emp_name` refused, missing objects skipped, a denied object told apart as
"not created yet" or "exists but not granted" from what the admin sees (and failed when
nobody can tell), `SELECT`-only grants after a full
reset, the password never written into SQL or output, bad user/host/database names
rejected, MySQL error codes read correctly, readable setup errors, connections that hide
query values, and settings with Windows line endings (set in the shell with a trailing
`\r`, or in a CRLF `.env`) read without the `\r`.

The dry-run tests use a `clean_env` fixture: they remove the `AGENT_*`, `DB_ADMIN_*`,
`MYSQL_ROOT_PASSWORD` and `DB_NAME` variables and point `--env-file` at an empty file, so
whatever is exported in your shell or in the repo's `.env` can't change the result.

**Not covered:** anything that needs MySQL, i.e. creating the user and the grants. The
real test is `agent_user.py --check` against your database.