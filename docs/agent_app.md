# agent_app.py: the agent's loop

`scripts/agent_app.py` is the thing a person actually talks to. It's a plain
OpenAI-style function-calling loop with no framework underneath: send the
conversation and `agent_tools.tool_schemas()` to the model, run whatever tool it
picks through `agent_tools.run_tool`, feed the result back, repeat until the model
answers in plain text. No vector store, no LangGraph, no memory beyond this process —
see [Why this loop, and not LangGraph yet](#why-this-loop-and-not-langgraph-yet).

```bash
pip install openai             # add to requirements.txt (team repo)
python scripts/agent_app.py "is CNC-02 running differently now?"
python scripts/agent_app.py                              # interactive session
python scripts/agent_app.py --debug "what oil does CNC-03 take?"
```

With uv (personal repo): `uv add openai`, then `uv run python scripts/agent_app.py ...`.

Needs, in `.env` (same file as the ingest scripts):

```bash
AGENT_DB_USERNAME=agent_ro
AGENT_DB_PASSWORD=...                                    # from agent_user.py
OPENAI_API_KEY=...                                        # your llm proxy member key
OPENAI_BASE_URL=https://35-240-177-226.sslip.io           # the llm proxy (https://github.com/ensanguine2279/sctp-aie-m5-jurong-marine-llm-proxy)
```

## What it guarantees, in code

Four of the [Harness Checklist](HARNESS_CHECKLIST.md) items are enforced here, not
just asked for in the prompt:

| # | What | How |
|---|---|---|
| 1 (grounding) | Every fact comes from a tool call made this turn | The model only ever picks a tool and fills in arguments; `agent_tools.py` validates them and runs the fixed SQL. There is no path from "the model says X" to "the answer says X" that skips a tool |
| 2 (citations) | Every answer can be checked | Every tool result's `sources` are collected across the whole turn (every tool call, not just the last) and shown under the answer as `sources[].label` |
| 3 (guardrails) | Policy is enforced between the user and the model, and between the model and the user | `ask()` runs [`guardrail.py`](guardrail.md) on both sides. A prompt injection, a request for a person's name or personal details (PDPA, trap C16) or a plainly out-of-scope request is refused in code with its own message, **before any model call** (`input_refused`); only a placeholder goes into `messages`, so the question never reaches a later prompt. An answer carrying a staff name, an NRIC, a phone number or an e-mail address is replaced before it is shown, in `messages` too (`output_blocked`). Found on the eval run, 8 Nov 2026: "What is the name of employee EMP015?" got "I could not check that", which read like an outage |
| C1 (severity scale) | The model can't pick a severity scale for the user | `count_incidents` / `list_incidents` take a `scale_quote`: the user's own words naming the scale. `scale_guessed()` checks the quote is really in a user message and names a scale ("5 is worst", "the current scale", "the old scale"; "severity 4 or worse" names none). If not, the scale is removed before the tool runs, so the tool returns `needs_clarification` and the user is asked. Found on the real run, 5 Nov 2026: gpt-4o-mini sent `"severity_scale":"5_is_worst"` on its own. Logged as `scale_guess_blocked` |
| 4 (honest degradation) | A failure never becomes an invented value | A tool that asks for a clarification (`needs_clarification`, `ambiguous_machine`, e.g. a severity number with no scale) has its message returned to the user **in code**; the model is not called again, so it can't answer over the question (found on the real run, 5 Nov 2026: gpt-4o-mini invented a count after the tool refused). A failed chat-API call (budget cap, network, timeout) is caught as `ChatError` and gives a plain "I couldn't reach the language model" — never a guess. A tool error (`needs_clarification`, `no_data`, `tool_failed`, ...) goes back to the model as the tool's own message, and the system prompt tells it to say so, not paper over it. A model that keeps calling tools past `MAX_TOOL_ROUNDS` (6) gets stopped with an honest "try a narrower question" instead of looping forever |
| 5 (state) | A follow-up resolves against the earlier turns, and "last month" is the calendar's last month, never the model's guess | The whole conversation (questions, answers, tool results) goes back to the model with every follow-up. Relative periods are resolved in code: `ask()` puts today's date (Singapore) and where the data ends, read from the database, into the system prompt; a period with no data is said to have none, then answered for the latest full month the data has ([below](#relative-dates-and-follow-ups-5)) |

Cross-restart state is the page's job, not the loop's: `ask()` works on a plain list of
messages, and the page saves that list after every answer and loads it back from the link
(`scripts/sessions.py`, [`sessions.md`](sessions.md)). The loop itself keeps nothing between
processes.

What it does **not** do, by design (see the capstone task list, not an oversight):

- **No human-in-the-loop gate (#6).** Not needed: every tool is read-only.

The eval suite (#8) runs this loop against the real model and database:
`scripts/run_evals.py` over `evals/cases.yaml` ([`evals.md`](evals.md)).

## The loop

```python
import agent_app as app, agent_tools as at

db = at.connect()                 # as agent_ro
client = app.build_client()       # OpenAI SDK pointed at the class proxy
messages = app.new_conversation() # just the system prompt

answer, sources = app.ask(db, client, messages, "is CNC-02 running differently now?")
print(answer)
for s in sources:
    print(" -", s)

# the conversation keeps growing: a follow-up reuses `messages`
answer2, sources2 = app.ask(db, client, messages, "and what about CNC-03?")
```

`ask()` is the entry point every caller uses: it runs the input guardrail, refreshes the
dates in the system prompt, then `run_turn`, then the output guardrail
([`guardrail.md`](guardrail.md)). `run_turn(db, client, messages, ...)`
is the part doing the actual looping: call the
model (`complete`), append its message, and if it asked for a tool, run every tool
call it made (there can be more than one in a round), append each tool's result as a
`role: "tool"` message, and go again — up to `MAX_TOOL_ROUNDS` times. It returns the
final plain-text answer, the sources gathered along the way, and which tools ran (for
`--debug` and the log). It never raises: a chat failure or an exhausted round budget
both return a message instead.

## Relative dates and follow-ups (#5)

The checklist's test for state is "and what about last month?" resolving correctly. Two
things have to hold.

**The earlier turns reach the model.** `messages` keeps every question, answer and tool
result, and `ask()` sends the whole list with the follow-up. So "And CNC-01?" after "What oil
does CNC-03 take?" is asked as an oil question, "Which of those are still open?" knows which
work orders, and a severity scale the user named two turns ago still counts: `scale_guessed()`
looks for the user's words in every earlier user message, not only the latest. A refused
question is only a placeholder in `messages`, so the conversation carries on after it.

**"Last month" means the calendar's last month.** The model has no today of its own: asked
for "last month", it guesses a date, often from its training. So the code gives it one, the
way an assistant on live data works: today's date in Singapore (10 Nov 2026 chat, "follow
convention"). On 10 Nov 2026 "last month" is October 2026.

The client's data is a snapshot that ends earlier, on 30 Jun 2026, so on most days the period
asked about has no data. The model is told where the data ends and what to do then: say so
plainly, with the dates, and answer the same question for the latest full month the data has
(June 2026), saying that's what it did. "And what about last month?" therefore gets "there's
no data for October 2026; the data runs to 30 Jun 2026. In June 2026, ...". On live data, where
the data runs to today, that fallback never comes up.

| Piece | What it does |
|---|---|
| `today()` | Today in Singapore (UTC+8, no daylight saving, so no time-zone database is needed in the container). `AGENT_TODAY=YYYY-MM-DD` freezes it: the eval suite does, so its expected dates don't move with the calendar |
| `relative_periods(today)` | This month, last month, this year, last year, by the calendar |
| `data_dates(db)` | The last day each source has data for, from the views `agent_ro` already reads: `MAX(incident_date)` on `incident_detail`, `MAX(wo_date)` on `work_order_detail`, `MAX(last_reading)` on `machine_overview`. Read once and cached on the `Db`. `None` without a database (the unit tests) or when it can't be read; a failure isn't cached, so the next question tries again |
| `latest_full_month(end)` | The latest calendar month the data covers to its last day: June 2026 for data ending 30 Jun 2026; for data ending mid-month, the month before |
| `dates_note(dates, today)` | The section added to the system prompt, in words |
| `ask()` | Sets the system message to `SYSTEM_PROMPT` plus that section before every question that reaches the model, so a conversation reloaded the next day gets the new date. A refused question reads nothing |
| `period_check()` | In `run_turn`, after every tool call with a `date_from`: a period that **starts** after the data ends is run again by the loop for the latest full month, and the model gets both results and a note (`period_after_data`); a period that **runs past** the data's end gets a note that it covers only up to that day (`period_past_data`); anything else is passed through unchanged |

On 10 Nov 2026, with the client's data, the model is told:

```
Dates:
- Today is 10 Nov 2026 (Singapore). So "this month" is 1 Nov 2026 to 30 Nov 2026, "last month"
  is 1 Oct 2026 to 31 Oct 2026, "this year" is 2026, "last year" is 2025, and "the past N days,
  weeks or months" counts back from today.
- The shop's data runs to 30 Jun 2026 (incidents to 28 May 2026, work orders to 30 Jun 2026,
  sensor readings to 30 Jun 2026). Nothing after that is in it.
- A period that starts after the data ends has no data. Say that plainly, with the period's
  dates and where the data ends, then answer the same question for the latest full month the
  data has, 1 Jun 2026 to 30 Jun 2026 (June 2026), and say that's what you did. Never present
  it as the period asked about.
- A period that runs past the data's end is answered for the part with data, saying where the
  data stops.
- When a question uses a relative period, put the exact dates in the tool call and say them in
  the answer.
- A follow-up keeps everything from the earlier question that it doesn't change: the machine,
  the period, the severity scale and the filters. "And what about last month?" asks the same
  question again for last month.
```

(The incident date shown is illustrative: it is whatever `MAX(incident_date)` returns.) When
the data runs to today, the two fallback lines are left out.

**The fallback is run in code, not left to the model.** The first version only told the
model, in the prompt, to fall back to June. On the first real eval run (10 Nov 2026) it didn't:
asked "and what about last month?", it queried October 2026 (right), got nothing, and answered
"CNC-02 had no work orders in October 2026"; asked "this month", it said "I could not check
that". So `period_check()` now does it, as the severity-scale guard does for scales: when a
tool call's `date_from` is after the data's last day, the loop runs the same tool with the
latest full month's dates and sends the model one tool message holding

```json
{"no_data_for_period_asked": true, "period_asked": {"from": "2026-10-01", "to": "2026-10-31"},
 "data_ends": "2026-06-30", "result_for_period_asked": {...},
 "latest_full_month": {"month": "June 2026", "from": "2026-06-01", "to": "2026-06-30",
                       "arguments": {...}, "result": {...}},
 "notes": ["Nothing in the data for the period asked, 1 Oct 2026 to 31 Oct 2026: the shop's
            data runs to 30 Jun 2026. In the answer, say that first, with those dates. Then
            give the result for June 2026 (...), the latest full month with data, ... Never
            present it as the period asked about."]}
```

Both results are cited, the note is shown under the answer with the other caveats, and the
log has one `tool_call` (the model's own, with October) plus a `period_after_data` line with
the fallback's arguments. What the model must still do is *say* it: the eval cases check the
answer names October, says there's no data, and gives June's count.

**If the data's dates can't be read** (the database is down when the question arrives), the
model still gets today's date, is told it doesn't know where the data ends, and is told to ask
the user for exact dates rather than assume they're covered; `dates_unknown` is logged (#4:
honest degradation, not a guessed month).

**Why the system prompt, and not a tool.** The dates are needed to *choose* a tool's arguments,
before any tool runs, and they are the same for every question. A tool would cost a model
round per question for a fact the code already has.

**Why not the data's end as "today".** That was the first version (9-10 Nov 2026): "last month"
was June 2026, so the demo's questions always landed on data. It was dropped for the
convention: a skeptical user who knows it's November would read "last month (June 2026)" as
the app being confused, while "no data for October; here's June" is plainly honest, and on
live data both give the same answer anyway.

**In the eval suite**, the system prompt counts as grounding evidence (`run_evals.evidence_for`):
its only numbers are these dates, so "no data for October 2026; the data runs to 30 Jun 2026"
is grounded and a month nobody gave the model is not. The suite freezes today at the date in
`cases.yaml` (`today:`), so "last month" is October 2026 in every run. The multi-turn cases are
in [`evals.md`](evals.md).

**Limits.**
- One fallback month for all sources: the latest full month of any of them. Incidents stop on
  28 May, so "last month's incidents" falls back to June, which has none; the note lists each
  source's last day, so the model can say so.
- The whole conversation is resent with every follow-up, tool results included. A long
  conversation costs more tokens per question; there is no trimming yet.
- Whether a follow-up *keeps* the earlier context, and how the answer words the fallback, is
  the model's job, told by the prompt and the tool's note and checked by the eval cases. The
  fallback query itself is code.
- Only tools with a `date_from` argument are checked: `list_work_orders`, `list_incidents`,
  `count_incidents` and `sensor_trend`. `compare_sensor_windows` names its windows differently,
  and `sensor_trend` already clips a period to the readings it has.

## Words the data can't take as written

Found 10 Nov 2026: asked "how many work orders were completed in nov 202", the app read "202"
as 2026 and "completed" as closed, and said neither. Both are now handled in code, like the
severity-scale guard.

**A year written short is asked about, never guessed.** In `ask()`, after the guardrail and
before any model call, `incomplete_year()` looks for a month (or the word "year") followed by
three digits that start a year: "nov 202", "November 202", "Nov. 202", "year 202". A day after
a month is at most two digits and a year four, so "nov 20" and "nov 2025" are left alone. The
user gets a question back (`year_question()`), with the two latest years the data has that
month in when the month and the data's end are known:

```
"nov 202" isn't a full year, so I won't guess it. Which did you mean: November 2025 or
November 2024? The shop's data runs to 30 Jun 2026.
```

No model call (`year_unclear` in the log, an `app` row on the page). The question and the reply
both go into `messages`, so the user's "2025" reaches the model with the question it completes.
A month with no year at all ("how many in November?") is left to the system prompt, which tells
the model to ask which year when the data has that month in more than one.

**"Completed" is read as CLOSED, and the answer says so.** The CMMS has no completion date and
no "completed" status: its statuses are `CLOSED`, `OPEN` and `AWAITING PARTS`, and each work
order is dated when it was raised. So "completed" (or "finished") can only mean `CLOSED`,
counted by the date raised. Asking the user every time would add a round trip for the one
reading there is; instead, when the question says "completed" or "finished",
`completion_note()` adds a note to every `list_work_orders` result telling the model to say
both: that "completed" was read as status `CLOSED`, and that the date is when each work order
was raised, not when it was finished. The note also shows under the answer with the other
caveats (`completed_read_as_closed` in the log, an `app` row on the page). The system prompt
says the same. The user's questions are read newest first: a follow-up that names no status
("November 2025", after the year was asked about; "and in 2024?") keeps the earlier
"completed", and one that names a status itself ("and open?") ends the look-back.

Two eval cases check both on the real model (tag `clarify`): `wo-completed-short-year` (the
year asked about with no model call, then "November 2025" counted as CLOSED and said) and
`wo-completed-read-as-closed` ([evals.md](evals.md)).

## The demo front end

`scripts/agent_ui.py` is a Streamlit chat page over the same `ask()`; it adds no logic.

```bash
pip install -r requirements-app.txt   # team repo: streamlit 1.36 or later; with uv (personal repo): uv pip install -r requirements-app.txt
streamlit run scripts/agent_ui.py
```

Under each answer it shows one line with what the answer cost, then tabs for what it rests
on: **Sources** (the sources the tools cited), **Caveats** (the tools' notes), **Photos**
(the scans behind it) and **What happened**: every model call (with seconds and cost) and
every tool call (the arguments the model sent, ok or refused, seconds), in order, and a
`guardrail` row when a question was refused in code (no model row then) or an answer was
replaced. On the newest answer What happened is the first tab, so it is open. The example
questions, including the PDPA one, are a grid on a new conversation and a lightbulb icon in the
top bar after that; the sidebar holds New conversation, the saved conversations and a toggle
that folds earlier questions into one line. The layout is described in
[`agent_ui.md`](agent_ui.md#what-the-page-shows). Its trace comes from the same events as the
log, collected per question, and the page also appends them to `logs/agent_app.jsonl` (run from
the folder you start Streamlit in), so the file has the same lines as the table.
`tests/test_agent_ui.py` (no Streamlit needed) covers the trace table and the page's other
helpers; how the page looks is checked by running it. Setting `APP_PASSWORD` puts a password
box in front of the page, and `AGENT_LOG_FILE=off` stops it writing the file (it still logs to
stderr). To host it, see [`agent_ui.md`](agent_ui.md#deploying-on-google-cloud-run).

## Logging (toward #7, observability)

Every call to `run_turn` logs, as one JSON line per event, on the `agent_app` logger.
Every line carries a UTC `ts` (ISO 8601, milliseconds):

- `tool_call`: one per tool the model called: tool name, the **arguments the model sent**
  (truncated to 500 characters), `ok`, the `error_code` when the tool refused (e.g.
  `needs_clarification`), number of sources, and how long the tool took. This is how you
  see what the model actually asked for.
- `chat_call`: round number, model, prompt/completion tokens, which tools were
  requested, how long the call took, and `cost_usd`, what the proxy charged for it.
- `chat_error`: a failed chat call and why.
- `scale_guess_blocked`: the model gave a severity scale the user never stated; it was removed before the tool ran.
- `clarification_returned`: a tool asked a question and the loop passed it on.
- `max_rounds_exceeded`: the loop was stopped rather than left to run forever.
- `input_refused`: the guardrail refused the question in code: `rule` (`injection`, `pdpa`,
  `out_of_scope`) and `reason` (which family of pattern fired). No model call follows.
- `output_blocked`: the guardrail replaced the answer: `rule` (`pii`) and `reason`
  (`staff_name`, `nric`, `phone`, `email`). Never the blocked text.
- `guard_not_ready`: the guardrail's staff name list isn't loaded, so the question was
  refused without a model call (fail closed; the CLI and the page don't start in that state).
- `dates_unknown`: where the data ends couldn't be read, so the model was told to ask for
  exact dates instead of assuming the period asked about is covered.
- `period_after_data`: a tool was asked for a period that starts after the data ends; the
  loop ran it again for the latest full month (`fallback`, `fallback_arguments`).
- `period_past_data`: a tool was asked for a period that runs past the data's end; the result
  carries a note that it covers only up to that day.
- `year_unclear`: the question had a year written short (`written`, e.g. "nov 202"); the user
  was asked which year, with no model call.
- `completed_read_as_closed`: the question asked about completed or finished work orders; the
  work-order result carries the note that they were read as status `CLOSED`.
- `turn`: one line per question — the question (truncated), every tool called, how
  many sources came back, `model_calls` and the question's `cost_usd`. A refused question has `refused` set, and a PDPA one is logged as
  `[withheld: pdpa]`, never its text (it may hold a name).
- `eval_case`: written by `run_evals.py` before each eval turn (case id and turn), so an
  eval run can be replayed from the same file.

By default this goes to `logs/agent_app.jsonl` (`--log-file ''` sends it to stderr
instead). Per-tool detail (the actual SQL, row counts, timing) is already logged
separately by `agent_tools.run_tool` on the `agent_tools` logger — the two logs
together give the full per-request trace the checklist asks for. **Logs stay in a file, not the database:** `agent_ro` is read-only by design, and logging
to the DB would need a second writable account and a retention decision for the user's
questions.

### Spend: what each question cost, and the budget (#7)

The LiteLLM proxy already records every call and its cost, so the app reads the proxy's own
figures rather than estimating from tokens:

| What | Where it comes from | How current |
|---|---|---|
| **Cost of each model call** | The `x-litellm-response-cost` header the proxy puts on every response. `complete()` reads it through the OpenAI SDK's raw-response API and logs it on the `chat_call` line as `cost_usd` | Immediate, exact as charged |
| **Cost of each question** | The sum, on the `turn` line (`model_calls`, `cost_usd`), and in the `usage` dict `ask()` fills for the caller. `cost_usd` is null if any call came back without a cost; a refused question costs 0, with 0 model calls | Immediate |
| **The key's spend against its cap** | `GET /key/info` on the proxy, asked with the app's own key: `scripts/spend.py`. The same figure the proxy blocks the key on. No admin key is used | About a minute behind (the proxy writes spend in batches) |

```bash
python scripts/spend.py          # Key capstone: $3.42 of $10.00 (34%), resets 2026-12-01
python scripts/spend.py --json   # with uv: uv run python scripts/spend.py
```

On the page, each answer has a line such as "2 model calls, 9,936 tokens, $0.0012", each
model row in **What happened** shows its cost, and the usage popover (the icon at the right of
the top bar; its tooltip shows the conversation's total) shows the conversation's total and the
key's spend against its budget, re-read from the proxy after every answer. From 80% of the
budget a warning shows across the page, outside the popover, so a nearly spent key is seen
before the demo rather than as a budget error during it. Team-wide spend stays with the proxy's own tools
(`spend_report.py`, `daily_report.py`, the `/ui` dashboard; see
[llm-budget-proxy-setup.md](llm-budget-proxy-setup.md#4-watch-spending-admin)); the app never
holds an admin key. `spend.py` never raises: a down proxy or a refused request comes back as
"Spend not available: ...".

### Reading the log of one question: a working example

Each line is one event; a `turn` line closes a question, so everything since the previous
`turn` line belongs to it. This is a real run (5 Nov 2026, before the severity-scale guard
existed), as it sits in `logs/agent_app.jsonl`:

```jsonl
{"ts": "2026-11-05T09:55:06.189+00:00", "event": "chat_call", "round": 1, "model": "gpt-4o-mini", "prompt_tokens": 2555, "completion_tokens": 49, "tool_calls": ["count_incidents"], "ms": 36849.2}
{"ts": "2026-11-05T09:55:06.274+00:00", "event": "tool_call", "round": 1, "tool": "count_incidents", "arguments": "{\"date_from\":\"2025-01-01\",\"date_to\":\"2025-12-31\",\"at_least_as_severe_as\":4,\"severity_scale\":\"5_is_worst\"}", "ok": true, "error_code": null, "n_sources": 1, "ms": 85.2}
{"ts": "2026-11-05T09:55:10.856+00:00", "event": "chat_call", "round": 2, "model": "gpt-4o-mini", "prompt_tokens": 3067, "completion_tokens": 47, "tool_calls": [], "ms": 4581.7}
{"ts": "2026-11-05T09:55:10.856+00:00", "event": "turn", "question": "how many incidents in 2025 were severity 4 or worse?", "tools_called": ["count_incidents"], "n_sources": 1}
```

`scripts/read_log.py` (standard library only) prints it one question at a time:

```bash
python scripts/read_log.py logs/agent_app.jsonl                 # the whole file
tail -n 50 logs/agent_app.jsonl | python scripts/read_log.py    # only the latest
python scripts/read_log.py --last 3 logs/agent_app.jsonl        # the last 3 questions
```

With uv (personal repo): `uv run python scripts/read_log.py ...`. Lines that aren't the
agent's events are skipped; events after the last `turn` (a question still running, or
one that crashed) are shown as `(unfinished)`. The example above comes out as:

```
Q: how many incidents in 2025 were severity 4 or worse?   (09:55:10 UTC, 41.5 s)
  model  r1   36.8 s  2555+49 tokens  asked for: count_incidents
  tool   r1   0.09 s  count_incidents  ok  sources=1
         args: {"date_from":"2025-01-01","date_to":"2025-12-31","at_least_as_severe_as":4,"severity_scale":"5_is_worst"}
  model  r2    4.6 s  3067+47 tokens  asked for: (answered)
  => tools: ['count_incidents'], sources: 1
```

How to read it:

| What you see | What it means |
|---|---|
| `model rN` seconds | Time the model call took. Here 36.8 s of the 41.5 s total went on the first model call, so the wait was the model or proxy, not the database |
| `tool rN` seconds | Time the tool took (the SQL). 0.09 s here |
| `args:` | The arguments the model sent, truncated at 500 characters. Here it sent `severity_scale: 5_is_worst` although the user never said which scale: the guess the guard now blocks. For a relative period, the dates here show how the model resolved it (October, then June as the fallback) |
| `REFUSED needs_clarification` | The tool declined and the user was asked instead of getting a number |
| `** scale_guess_blocked` | The model's scale had no quote from the user, so it was removed before the tool ran. Its arguments are shown on that line, the arguments the tool actually got are on the `tool` line below it |
| `** clarification_returned` | The loop passed the tool's question straight to the user, with no second model call |
| `** chat_error` | The model call failed (budget cap, network, timeout), with the reason |
| `eval case …, turn N` | The question was asked by `run_evals.py`; the case id is in `evals/cases.yaml` |
| `sources=0` after a number is given | A number with no source: investigate |
| `** input_refused` and no `model` line | The guardrail refused the question in code; the model was never called. A PDPA question shows as `Q: [withheld: pdpa]`. `read_log.py` prints the event but not its `rule` / `reason`: `grep input_refused logs/agent_app.jsonl` has them |
| `** output_blocked` | The guardrail replaced the model's answer before it was shown (rule and kind in the JSON line, never the text) |

**On the hosted page** the same lines go to Cloud Logging (stderr). Read the latest ones into
the same script (the exact filter has not been run against the live service; if the fields
come back as `textPayload` instead of `jsonPayload`, the script reads both):

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="jme-agent"'     --freshness=1h --limit=200 --format=json | python scripts/read_log.py
```

Or open **Cloud Run, jme-agent, Logs** in the console and search for `turn`.

**Limits:** lines carry no session id. If two people use the hosted page at the same moment,
their events interleave and the grouping above can mix two questions together. For a one-person
demo or a local run this doesn't matter; adding a turn id to every event would fix it.

## Why this loop, and not LangGraph yet

A plain function-calling loop was built first because it's the smallest thing that
answers a question end to end, grounded and cited — nothing else in the checklist
(evals, guardrails) has anything to run against until this exists. LangGraph adds
real value on top (a checkpointer for #5, which `sessions.py` now does in a table, explicit guardrail nodes for #3), but none
of that is needed to get a working demo; it's the next layer, not a prerequisite for
this one. The guardrail is two function calls in `ask()`, so it would become two nodes
unchanged. See the task list for when it gets built.

## Tests

`tests/test_agent_app.py` (33 tests, no database, no real LLM call): the loop's own
logic only — `agent_tools.run_tool` and the chat call are both stubbed, since tool
correctness is already covered by `tests/test_agent_tools.py`. Covers: a plain
text answer needing no tool; one tool call round-tripping into the next model call;
several tool calls in one round all running and all being cited; a failed chat call
degrading to a plain message rather than raising; too many tool-call rounds stopping
with an honest message rather than looping forever; the assistant-message shape
matching what the API expects, with and without tool calls; source label
deduplication, keeping first-seen order; a new conversation starting with only the
system prompt; and `ask()` appending the question, returning formatted sources, and
a severity scale the user never named (no quote, an invented quote, a quote naming no scale) being stripped so the tool asks, and one the user did name passing; a clarification going straight to the user with no further model call; logging one `turn` summary line; every log line carrying a timestamp and each tool call logging its arguments and refusal code. **The guardrail in `ask()`:** a refused question never reaching the model; the question kept out of `messages` and a PDPA one out of the log; the conversation carrying on after a refusal with nothing of the refused question in the next prompt; an answer with personal data replaced, in `messages` too, and absent from the log; an ordinary answer passing unchanged; without the staff name list, every question refused with no model call and the app refusing to start. **Spend:** the proxy's cost header read on each call, an unknown cost kept as unknown (never 0), each call's and question's cost logged and handed out of `ask()`, a call without a cost making the question total unknown, a refused question costing nothing. `tests/test_spend.py` (10 tests, no network, and no settings from your shell) covers `spend.py`: the proxy's reply as returned for the app's key on 9 Nov 2026, the percentage and the 80% warning, a key with no cap, the request going to `/key/info` with the app's key, and a down proxy, a refused key or a bad reply never raising. The guard's own rules are tested in `tests/test_guardrail.py` ([`guardrail.md`](guardrail.md#tests)). One test documents, rather than hides, that a bug
in `run_tool` itself (not a normal tool error) still propagates today — a deliberate
choice to surface in a test, not a gap to silently paper over.

`tests/test_multiturn.py` (29 tests, no database, no real LLM call; a stand-in `Db` and a
scripted chat call): **where the data ends**: read from each source (a DATE, date text and
DATETIME text all understood) and only once; no database, a down one or an empty one giving no
dates, a failure not cached. **What "today" means**: the calendar's date in Singapore, unless
`AGENT_TODAY` freezes it (a value that isn't a date ignored); last month by the calendar, with
the month and year boundaries (December into January, a leap February, January into February);
the latest full month the data has (ending on a month's last day or mid-month). **The note**:
today, last month, where each source ends and the June fallback, in words; on live data no
fallback to explain; without the data's dates, today and an instruction to ask, with nothing
claimed about the data. **The dates reaching the model**: on every question, refreshed rather
than stacked on a follow-up, after a reload too; unreadable dates logged as `dates_unknown`; no
database leaving the prompt unchanged; a refused question reading nothing. **A follow-up**
reaching the model with the earlier question and answer and one system prompt. **The eval
suite**: the dates given to the model counting as grounding evidence and a month nobody gave it
not; real multi-turn cases in `cases.yaml`; every follow-up refused by the guard exactly when its
case says so; the frozen today in `cases.yaml` and the October / June dates the cases expect
following from it; the runner reading `today:` (a bad value refused) and `--check` showing it.
**The fallback in code** (found on the first real eval run): a period after the data ends run
again for the latest full month through the real `run_turn`, both results and the note in one
tool message, `period_after_data` logged once beside one `tool_call`, both cited, the note
among the caveats; a period inside the data passed through untouched; one running past the
data's end noted (`period_past_data`) with no second query; no change without the data's dates
(no database, or a down one).

`tests/test_clarify.py` (11 tests, no database, no real LLM call): a year written short found
in five spellings, and full years, days, ids and plain questions left alone; the question back
offering the two latest years the data has that month in (November 2025 or 2024 for data
ending 30 Jun 2026), and a plain question when the month or the data's end isn't known; the
model never called for a short year, the question and reply both kept, and the user's "2025"
reaching the model with the question it completes; the completed note for "completed",
"complete" and "finished", only on work-order tools; carried into a follow-up that names no
status and ended by one that does; the note reaching the model in the tool result and the page in the notes.

`tests/test_read_log.py` (5 tests, standard library only): the documented example prints
exactly as shown above; other lines skipped and an unfinished question shown; gcloud output
read oldest first from `jsonPayload` or `textPayload`; the guard, eval and chat-error events
shown; the command line reading a file and keeping the last N questions.

**Not covered:** a real call to the llm proxy. That is what the eval suite is for
(`python scripts/run_evals.py`, [`evals.md`](evals.md)): it asks the kill-shot questions, the
trap-list questions and the multi-turn follow-ups through this loop against the real database
and proxy.