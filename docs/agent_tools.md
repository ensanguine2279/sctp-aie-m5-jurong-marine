# agent_tools.py: the agent's tools

`scripts/agent_tools.py` is the layer between the LLM and the cleaned data. The model never
writes SQL for a question these tools cover: it picks a tool and fills in its arguments,
and the code validates them, runs a fixed, parameterised query as the read-only app user
(`agent_ro`, [`agent_user.md`](agent_user.md)) and returns the answer with its sources.
Same question, same tool and arguments, same SQL, same answer.

```bash
pip install pydantic sqlglot             # add both to requirements.txt (team repo)
python scripts/agent_tools.py schemas    # the tool definitions the LLM gets (~3,000 tokens)
python scripts/agent_tools.py call find_sop '{"question": "entering the blasting chamber"}'
python scripts/agent_tools.py call compare_sensor_windows '{"machine": "CNC-02"}' --trace
```

With uv (personal repo): `uv add pydantic sqlglot`, then `uv run python scripts/agent_tools.py ...`.
`call` connects with `AGENT_DB_USERNAME` / `AGENT_DB_PASSWORD` from `.env`, and refuses to
run if that is the owner account (`DB_USERNAME`). `--trace` also prints the SQL that ran.

## The tools

| Tool | Answers | Reads (all granted to `agent_ro`) |
|---|---|---|
| `list_machines` | Machine ids and names, and what data each has | `machine_overview` |
| `get_nameplate` | What the plate photo says, field by field, with an OCR confidence per field | `machine_nameplate`, `img_nameplates_audit` |
| `machine_lubrication` | "What oil does X take / grease interval": plate and spares sheet side by side, whether the grades agree, which to trust and why | the two above + `machine_spares`, `critical_spares_audit` |
| `get_spares` | Spares for a machine or part, stock vs minimum, lead time in days and as written, size in mm and inches | `machine_spares`, `critical_spares_audit` |
| `count_incidents` | "How many", "which machine has most", grouped by machine, year, month, severity, description or `scale_period` | `incident_detail`, `cmms_safety_incidents_audit` |
| `list_incidents` | The incidents behind a count (up to 50), by date, with both severities and their dump line | the same |
| `get_incident` | One incident: CMMS record vs its scanned form, field by field | + `incident_report_vs_cmms`, `img_incident_reports_audit` |
| `list_work_orders` | Work orders: counts, hours, closed and not-closed costs apart, possible double entries | `work_order_detail`, `cmms_work_orders_audit` |
| `sensor_trend` | Vibration and temperature per day, week or month, with coverage of the 12-minute schedule | `vibration_temp_audit` |
| `compare_sensor_windows` | Ask 2: is a machine running differently now, and is it the machine or the whole shop | `vibration_temp_audit` |
| `find_sop` | The SOP for a question, quoted whole; "no SOP covers this" when none does | `sops`, `sop_machines`, `machine_sops`, `sops_audit`, `sop_steps_audit` |
| `query_sql` | Last resort: one checked SELECT for a question nothing else fits | any granted object |

Note `agent_ro` can't read `vibration_temp` itself, only `vibration_temp_audit`, so the
sensor tools aggregate per day in SQL (`DATE(sampled_at)`, one row per day) and roll up
to weeks and months in pandas.

## What every tool returns

`run_tool(db, name, arguments)` never raises. It returns a `ToolResult`:

| Field | What it holds |
|---|---|
| `data` | What the answer may say. Every number in the answer comes from here |
| `sources` | One entry per file the answer used: `kind` (`file`, `image`, `sop`, `table`), the audit view, file name, lines or rows, SHA-256, and a ready `label`, e.g. `CMMS incident 914: jurongmarine_cmms.sql, line 302 (cmms_safety_incidents_audit), SHA-256 <first 12>…` (line number illustrative) |
| `notes` | Caveats the answer must carry: OCR uncertain, scale converted, open costs are estimates, the trap-list item behind it |
| `error` | `{code, message}` when the tool couldn't answer (below) |
| `queries` | The trace: each SQL statement, its parameters, row count and time. Logged, not sent to the model (`for_model()` leaves it out) |

Every call is also logged as one JSON line on the `agent_tools` logger: tool, arguments,
ok, error, time, and each query's first 200 characters, rows and time. That is the
per-request trace for [HARNESS_CHECKLIST](HARNESS_CHECKLIST.md) section 7.

| Error code | Meaning | What the app does |
|---|---|---|
| `needs_clarification` | A severity number came without its scale | Ask the user which scale |
| `ambiguous_machine` | "crane", "the lathe", "welder": more than one match | Ask which one; the message lists them |
| `unknown_machine` | No CMMS machine matches | Say so; the message lists the 12 |
| `no_data` | E.g. sensors on a crane, an incident id that doesn't exist | Say the data doesn't have it |
| `bad_arguments` | The model sent something invalid | The model retries with the message |
| `not_allowed` | `query_sql` refused the SQL | The model uses a tool or says it can't |
| `tool_failed` | The database is down, a timeout, a bug | "I couldn't check X", never an estimate |

Every query runs with `MAX_EXECUTION_TIME` 10 s on MySQL.

## The traps the tools handle in code

| Trap | How |
|---|---|
| **Severity scales (C1, the auditor's test)** | Incident tools filter and group on `severity_5_worst` only. A user's severity number must come with `severity_scale` (`5_is_worst` or `1_is_worst`), converted in code (6 − n); without it the tool returns `needs_clarification`. Tested: "sev 1, old scale" and "sev 5, current scale" give the same incidents across the July 2025 switch, and a naive "logged ≥ 4" count is never produced |
| July 2025 (C2) | Incidents whose severity can't be placed are left out of severity counts and listed under `unplaced`, with a note |
| Incident ids not in date order (C5) | Listings are ordered by `incident_date` |
| PDPA (C16) | No tool returns employee codes, trades or shifts; `agent_ro` can't read names anyway |
| OCR on nameplates (I14-I21) | Each field gets `clear`, `read_by_majority`, `uncertain` (no two passes agreed: I15) or `unreadable` (failed its check: I18), from the image's warnings. Uncertain or unreadable values come with a note to say so and cite the photo |
| Plate lines that don't fit the machine (I2, I3, I19, I20) | `KNOWN_CONCERNS` in the code attaches the trap-list finding to the field (BLAST-01's capacity and oil, crane capacities point to SOP-CRANE-02, the welders' and compressor's oil) |
| Two sources for the oil (I3) | `machine_lubrication` returns both and states the trust rule: the plate is the machine's spec when read clearly. Grades are compared only with spares listed for **that machine**; a shop-wide (`ALL`) item such as hydraulic oil gets a note that it doesn't contradict the plate (on the real data, CNC-03's plate VG68 was wrongly reported as disagreeing with the shop's VG32 hydraulic oil) |
| Plate values checked by eye (I15) | `KNOWN_CONCERNS` carries the eye-checked value next to the OCR one (CNC-03 grease 590 → 500 h, weight 8892 → 8092 kg; CRN-02 grease 509 → 500 h, capacity 2600 → 2000 kg). The stored value isn't changed; the answer gives both. The lasting fix is a reviewed-corrections table at ingest |
| Open WO costs (C10), double entries (C9) | Costs closed vs not closed reported apart; same machine, day and type flagged |
| Spares units (S2, S5, S7) | Sizes in mm and inches; notes on belt part numbers, units of issue and the unstated currency |
| Machines without sensors (L5) | `no_data`, naming the 5 machines that have them |
| Gaps, noise (L4, L6) | Coverage per bucket against the 12-minute grid; means, never single readings |
| No SOP for most incident types (D3) | `find_sop` uses `ingest_sops.search_sops`, the same gate: below the minimum score the answer is `found: false`. It never stretches another SOP; if the SOP found doesn't name the machine asked about, a note says so |
| SOP steps quoted alone | `find_sop` always returns the SOP whole with its lines; steps pointing to documents not in the data get a note (D8) |

## Machine names

`resolve_machine` accepts `CNC-02`, `cnc 2`, `CNC2`, `pmp-t1`, an id inside text
(`CNC-02 spindle`), words from the CMMS name or model (`the compressor`, `overhead crane`),
`crane 2` (name plus the id's number), and a short `MACHINE_ALIASES` list
(`sandblasting` → BLAST-01, `test pump sump` → PMP-T01). Two equally good matches are
`ambiguous_machine`, never a pick. Like `ingest_sops.ALIASES`, the alias list is a
judgement: add an entry, with its question as a test, when a real question misses.

## Ask 2: is CNC-02 off?

`compare_sensor_windows` compares a baseline window (default: the first 90 days) with a
recent one (default: the last 30 days), on daily means. A metric is **elevated** when the
recent mean is at least 3 baseline standard deviations (of daily means) above the baseline
**and** the rise is at least 10% for vibration or 2 °C for temperature. Temperature's minimum
is absolute because a percentage of a Celsius reading means nothing (0 °C isn't "no heat"):
a 10% rule missed CNC-02's +4.0 °C (7.8%, 8 sd) on the real data. The same comparison runs for every other machine with sensors, so the
verdict is `machine_specific` (only this machine), `shop_wide` (others too) or
`not_elevated`. It also returns weekly means for all sensored machines (`chart_weekly`),
ready for the chart for the auditor. Weeks run Monday to Sunday, so the first (from
29 Dec 2025, data from Thu 1 Jan) and last hold fewer days: each row has `days` and
`coverage_pct`. Every machine compared is cited with its file, lines and hash. The rule is in the result, so the answer can state it.

## query_sql: the fallback

For a question no tool fits. `check_sql` (sqlglot, MySQL dialect) allows exactly one
`SELECT` (no `UNION`, `INTO` or locking read) over `ALLOWED_OBJECTS` only, named columns
(`COUNT(*)` is fine, `SELECT *` isn't), none of `severity_as_logged`, `raw_tuple` or any
`*_raw` column, no `SLEEP` / `BENCHMARK` / `LOAD_FILE`, and forces `LIMIT 200`. The database
refuses the rest anyway; this refuses it before the query is sent, and the model gets a
reason it can act on. Without sqlglot installed, `query_sql` is off rather than unchecked.
`ALLOWED_OBJECTS` must match `agent_user.allowed_objects`; a test checks it.

## Wiring it to the LLM

```python
import agent_tools as at

db = at.connect()                                   # as agent_ro
tools = at.tool_schemas()                           # pass as `tools` to the LiteLLM proxy
...
for call in response.choices[0].message.tool_calls:
    result = at.run_tool(db, call.function.name, call.function.arguments)
    messages.append({"role": "tool", "tool_call_id": call.id, "content": result.for_model()})
```

Use `temperature=0`. Show `sources[].label` under the answer; ask the user when the error
code is `needs_clarification` or `ambiguous_machine`.

## Tests

`tests/test_agent_tools.py` (83 tests, no MySQL): the tools run their real SQL on an
in-memory SQLite database whose tables have the names and columns of the granted views,
filled with a small sample shaped like the client's data (machine names and some values are
illustrative) and the client's three SOPs loaded through `ingest_sops.process`. Covers:
every tool's schema; machine names written six ways, ambiguous and unknown machines;
**the severity kill-shot**: four phrasings without a scale asked about on both incident
tools, the same question on either scale giving the same incidents, the naive logged-value
count never produced, July 2025 reported apart; listings by date with no employee data;
form vs CMMS disagreements; OCR confidence per field, known plate problems, no photo; oil
from two sources that disagree, and an unreadable one; spares in both units, shop-wide
only, below minimum once each; open costs apart and double entries; no sensors on a crane;
weekly coverage; CNC-02 `machine_specific` against flat machines; the blasting chamber SOP
quoted whole with lines 3-6, nothing for welding, the missing machine manual; query_sql
refusing writes, two statements, employees, `SELECT *`, the logged severity, `SLEEP` and
`UNION`, and capping rows; a dead database giving `tool_failed`; bad, extra and JSON-string
arguments; and the app refusing the owner account.

**Checked against the real database (5 Nov 2026):** every tool was run once with
`call ... --trace` against the client's real MySQL data, not just the SQLite sample.
Three real bugs were found and fixed this way (none of them caught by the SQLite tests,
because the sample data didn't reproduce the exact numbers):

| Call | What the real data showed | Fix |
|---|---|---|
| `compare_sensor_windows '{"machine": "CNC-02"}'` | `elevated: false` on a +4.04 °C / 8.1σ temperature rise, because the 10%-relative rule is meaningless for Celsius; only the target machine was cited, not the other sensored ones | Absolute 2 °C minimum for temperature (kept 10% relative for vibration); every sensored machine now gets its own citation |
| `machine_lubrication '{"machine": "CNC-03"}'` | CNC-03's own plate (ISO VG68) was reported as disagreeing with the shop-wide `ALL`-listed hydraulic oil (ISO VG32) — a false conflict, since that item isn't this machine's spec | Spares split into `own` vs `shop` (by `listed_for`); grades compared only against `own`; eye-checked trap-list values (I15) surfaced alongside the OCR ones |
| `find_sop '{"question": "before entering the blasting chamber"}'` | Scored exactly `MIN_SCORE` (2.0, the bare minimum); a closer paraphrase, "going into the blast chamber", found nothing | `ENTRY_VERBS` + `SPACE_WORDS` paired-condition rule in `ingest_sops.expand_question` (see [`ingest_sops.md`](ingest_sops.md#entry-needs-a-space)); both questions now score 4.0 or higher |

Re-verified after the fixes, all against the real database:

- `count_incidents '{"at_least_as_severe_as": 5, "severity_scale": "5_is_worst", "date_from": "2025-01-01", "date_to": "2025-12-31"}'`: returns 1 incident (978), correctly filtered on `severity_5_worst` rather than the logged value, with the two ambiguous July 2025 incidents (906, 907) excluded from the count and listed under `unplaced` with a note — the severity kill-shot, confirmed on real data, not just the SQLite sample.
- `find_sop '{"question": "welding hot work PPE for grinding"}'`: `found: false`, all 3 current SOPs searched, none scoring 2 — confirmed the tool doesn't adapt another SOP when none covers the topic.
- The full test suite (`tests/test_agent_tools.py` + `tests/test_sops.py` + the rest of the repo's tests): 423 passed, 0 failed, 0 skipped, with the fixes above applied.

**Still not covered:** a systematic sweep of every tool against every trap-list item on the
real data (the checks above were targeted, not exhaustive). That gap is what the eval
suite (`evals/cases.yaml`, planned) is for — see [`HARNESS_CHECKLIST.md`](HARNESS_CHECKLIST.md#8).