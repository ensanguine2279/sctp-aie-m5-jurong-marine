# Evals: `evals/cases.yaml` and `scripts/run_evals.py`

The eval suite for [HARNESS_CHECKLIST.md](HARNESS_CHECKLIST.md) section 8 (and the
kill-shot tests of section 3). Unlike the unit tests, it runs the **real model** (gpt-4o-mini
through the llm proxy) through the **real loop** (`agent_app.ask`, guard included) over the
**real database** (as `agent_ro`), and checks each answer against what actually happened:
the tools that ran, their results and the log events, not what the model says it did.

```bash
pip install pyyaml               # team repo: add to requirements-dev.txt
python scripts/run_evals.py --check                  # validate cases.yaml: no calls, free
python scripts/run_evals.py --list                   # the cases and their tags
python scripts/run_evals.py                          # the whole suite
python scripts/run_evals.py --only severity-current-scale
python scripts/run_evals.py --tag killshot           # one group (killshot, sop, oil, refusal, ...)
python scripts/run_evals.py --repeat 3 --tag severity    # same cases 3 times: is it flaky?
python scripts/run_evals.py --max-usd 0.10           # stop starting cases after $0.10 (default 0.50)
```

With uv (personal repo): `uv add --dev pyyaml`, then `uv run python scripts/run_evals.py ...`.

Needs the same `.env` as the app (`AGENT_DB_USERNAME`/`AGENT_DB_PASSWORD`, `OPENAI_API_KEY`,
`OPENAI_BASE_URL`). Exit code 0 when every case passed, 1 when any failed (2: bad
`cases.yaml` or no database), so it can gate a commit or CI.

## What a case checks

A case is one conversation of one or more turns. For each turn, `expect` can check:

| Check | Keys | What it catches |
|---|---|---|
| **Which tool ran** | `tools_all`, `tools_any`, `tools_none`, `tools_ok`, `tools_not_ok`, `tool_error` | The model skipping a tool, using `query_sql` for something a tool covers, a severity count that should have been refused (`tools_not_ok: [count_incidents]`) |
| **How it ran** | `tool_args` (arguments **as run**, after the scale guard), `tool_data` (a value in the tool's result, by path, e.g. `data.unplaced[*].incident_id`) | A guessed scale, the wrong machine, a verdict or count that changed |
| **Key facts** | `answer_contains`, `answer_not_contains`, `answer_matches`, `answer_not_matches` | The number, id or caveat missing; an adapted SOP; a doubtful plate value stated as fact |
| **Oracle** | `oracle: [{tool, args, must_appear, equals}]` | The runner calls the tool itself (no model, milliseconds, free). Every value at `must_appear` must be in the answer; `equals` pins a value from a real-data check and fails loudly if the data changes |
| **Sources** | `sources_min` (default 1), `sources_match` (regex on the labels) | An answer with nothing cited, or cited from the wrong place |
| **Grounding** | `grounded` (default on), `allow_numbers` | Anything stated that no tool returned (below) |
| **Log events** | `events_present`, `events_absent`, `clarification` | `scale_guess_blocked` when the user did name the scale; an answer where a question was due |

A failed model call (`chat_error`: budget cap, network) makes the case **ERROR**, not FAIL:
the model was never asked, so there is no verdict. Hitting `MAX_TOOL_ROUNDS` is a FAIL.

### Grounding: "nothing stated that no tool returned"

Evidence is every tool result of the conversation (as the model saw it), plus the user's
own messages. In the answer, the runner checks:

- **numbers**: each must be a number in the evidence (`10,000` = `10000`). A tool's number
  rounded (51.93 said as 51.9) is a **warning**, not a failure. The digits of dates and ids
  in the evidence don't count (`07` of `2025-07-11`, `02` of `CNC-02`), so a small invented
  count isn't grounded by accident;
- **dates**: `2024-01-24`, `24 Jan 2024`, `January 24, 2024`, `24/01/2024` (day first, trap
  I8) are the same date and must be in the evidence; `Jul 2025` must be a month in it;
- **ids**: any token with letters and digits (`CNC-02`, `BRG-6310`, `SOP-CONFINED-03`, `VG68`)
  must appear in the evidence.

Words aren't checked; that's what `answer_not_matches` is for. **Limit:** small numbers
(1, 2, 4, 5) appear in almost every tool result, so grounding alone won't catch an invented
small count. The `oracle` and `tool_data` checks are what pin the right number.

## The cases (23 cases, 24 turns)

| Group | Cases | Expected |
|---|---|---|
| **Severity kill-shot** (C1, C2), 5 phrasings | `severity-current-scale` (2 turns), `killshot-severity-4-or-higher`, `killshot-list-severity-4-plus`, `killshot-machine-most-severe`, `killshot-sev-ge-4` | Turn 1: the scale question, no count (no successful `count_incidents`/`list_incidents`). After "the current scale": `count_incidents` with `5_is_worst`, `scale_guess_blocked` not logged, the count the oracle gets, 906 and 907 named as unplaced |
| Severity, scale named | `severity-5-is-worst-named`, `severity-old-scale-same-incidents` | 1 incident (978) either way: severity 5 on the current scale = 1 on the old (real data, 5 Nov 2026) |
| SOPs (D3, D7, D8) | `sop-welding-none`, `sop-grinding-none`, `sop-blasting-chamber`, `sop-crane-max-load` | "No SOP covers this", with no line of another SOP; SOP-CONFINED-03 whole (permit, gas test, attendant, MOM); CRN-01 10,000 kg from SOP-CRANE-02 |
| Ask 2 | `cnc02-running-differently` | `machine_specific`; vibration +91.4 %, temperature 51.93 → 55.97 °C, all four in the answer |
| Sensors (L5) | `crane-no-sensors` | No readings for the crane; no temperature given |
| Oil (I3, I15, I20) | `oil-cnc03`, `oil-blast01-two-sources` | CNC-03 VG68, no false conflict with the shop's hydraulic oil, never 590 h; BLAST-01 Tonna S3 M68 with the doubt carried |
| Incidents (C5, I7, I12) | `incident-earliest-917`, `incident-900-root-cause`, `incident-911-read-by-eye` | 917, not 900; the 5-Why isn't in the data; 911's severity read by a person, said so |
| Spares (S5) | `spares-below-minimum`, `spares-belt-length` | BRG-6310, SEAL-KIT-KSB, LS-CXT-11; 85 in, never 850 mm |
| Nameplate (I19) | `nameplate-cnc01-weight-doubt` | 9257 kg with the doubt |
| **Refusals** | `sop-welding-none`, `sop-grinding-none`, `crane-no-sensors`, `incident-900-root-cause`, `pdpa-employee-name`, `wo-11570-parts-not-recorded` (tag `refusal`) | The right answer is "there isn't one / I can't": no name for EMP015, no part for WO 11570 as fact (C11) |

Pinned values come from the project's own checks on the real data ([trap_list.md](trap_list.md),
[agent_tools.md](agent_tools.md#checked-against-the-real-database-5-nov-2026), the CNC-02 run).
The severity ≥ 4 count for 2025 hasn't been pinned by any check yet, so the oracle supplies
it at run time and the first run prints it; pin it with `equals` once you've confirmed it.

## Cost and time

Each turn is one to three model calls (5–40 s each). A full run is about 40 model calls:
expect roughly 5–20 minutes. The kill-shot cases are cheapest: the guard returns the question
in code, so one model call each.

Every case prints its calls, tokens and cost, and the run ends with a total:

```
Spend: $0.0312 as charged by the proxy (x-litellm-response-cost); $0.0309 at list price
```

The proxy's own figure comes from the `x-litellm-response-cost` header it returns on every
call ([llm-setup.md](llm-setup.md#how-accurate-is-the-cost-tracking));
the list-price estimate is tokens × $0.15 / $0.60 per million (gpt-4o-mini, checked 7 Nov
2026; `PRICES` in the script). `--max-usd` (default $0.50) stops **starting** new cases once
the run has spent that much; skipped cases show as SKIPPED. `spend_report.py` on the proxy is
still the authority for the key's remaining budget.

## Output and logs

- The console: one block per case, with the answer and every failing check when it fails.
- `evals/results/latest.txt`: the same text. **Commit it** after a run (the checklist's
  evidence: "the eval script + its latest pass/fail output, committed").
- `evals/results/<UTC time>.json`: every turn in full: question, answer, sources, notes,
  each tool with the arguments it ran with and its result, the log events, tokens, cost,
  oracle values, failures and warnings.
- `logs/agent_app.jsonl` (`--log-file`): the agent's own events, as the app writes them, with
  an `eval_case` line before each turn, so `read_log.py` ([agent_app.md](agent_app.md#reading-the-log-of-one-question-a-working-example))
  replays any case.

## Adding a case

1. Ask the question in the app (`python scripts/agent_app.py --debug "..."`) and read the log.
2. Add it to `evals/cases.yaml` with the right `tags` (a trap id, `refusal` if the right
   answer is "can't / none"). Check what *must* hold, not the model's wording: a tool and
   its data, a number via `oracle` or `tool_data`, a caveat by regex, forbidden values.
3. `python scripts/run_evals.py --check`, then `--only <id>`.
4. A failure that's a real bug: fix it, rerun, commit both (the checklist wants the
   fail → fix history). A failure that's a too-strict check: loosen the check, and say why
   in the case's `note`.

## Tests

`tests/test_evals.py` (19 tests, no database, no real model; model and tools stubbed): the
suite meets the checklist (20+ cases, 2+ refusals, 5 kill-shot phrasings, each asking and
never counting); a malformed `cases.yaml` refused before any call; selection by id and tag;
grounding (an invented number, a rounded one as a warning, ids, dates in four formats,
thousands separators, digits inside dates and ids not counting); paths into a tool result;
the severity case end to end through the real `agent_app` loop and guard: passing when the
guard strips the guess, **failing on the 5 Nov behaviour** (a guessed scale accepted and a
count given), failing on an invented count; a failed model call as ERROR; a changed pinned
value reported; the proxy's cost header read per call; the list-price fallback.

**Not covered by the tests:** the real run itself. That is what `run_evals.py` is for.