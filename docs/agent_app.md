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

Three of the [Harness Checklist](HARNESS_CHECKLIST.md) items are enforced here, not
just asked for in the prompt:

| # | What | How |
|---|---|---|
| 1 (grounding) | Every fact comes from a tool call made this turn | The model only ever picks a tool and fills in arguments; `agent_tools.py` validates them and runs the fixed SQL. There is no path from "the model says X" to "the answer says X" that skips a tool |
| 2 (citations) | Every answer can be checked | Every tool result's `sources` are collected across the whole turn (every tool call, not just the last) and shown under the answer as `sources[].label` |
| C1 (severity scale) | The model can't pick a severity scale for the user | `count_incidents` / `list_incidents` take a `scale_quote`: the user's own words naming the scale. `scale_guessed()` checks the quote is really in a user message and names a scale ("5 is worst", "the current scale", "the old scale"; "severity 4 or worse" names none). If not, the scale is removed before the tool runs, so the tool returns `needs_clarification` and the user is asked. Found on the real run, 5 Nov 2026: gpt-4o-mini sent `"severity_scale":"5_is_worst"` on its own. Logged as `scale_guess_blocked` |
| 4 (honest degradation) | A failure never becomes an invented value | A tool that asks for a clarification (`needs_clarification`, `ambiguous_machine`, e.g. a severity number with no scale) has its message returned to the user **in code**; the model is not called again, so it can't answer over the question (found on the real run, 5 Nov 2026: gpt-4o-mini invented a count after the tool refused). A failed chat-API call (budget cap, network, timeout) is caught as `ChatError` and gives a plain "I couldn't reach the language model" — never a guess. A tool error (`needs_clarification`, `no_data`, `tool_failed`, ...) goes back to the model as the tool's own message, and the system prompt tells it to say so, not paper over it. A model that keeps calling tools past `MAX_TOOL_ROUNDS` (6) gets stopped with an honest "try a narrower question" instead of looping forever |

What it does **not** yet do, by design (see the capstone task list, not an oversight):

- **No guardrail layer (#3).** Policy still lives in the system prompt, not in code
  between the model and the user. Next step.
- **No cross-restart state (#5).** `messages` lives in one Python process; closing the
  app loses the conversation. A checkpointer (SQLite, the NB 03 pattern) is a
  LangGraph-shaped problem, not this loop's.
- **No human-in-the-loop gate (#6).** Not needed: every tool is read-only.
- **No eval suite yet (#8).** `evals/cases.yaml`, separate piece of work.

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

`run_turn(db, client, messages, ...)` is the part doing the actual looping: call the
model (`complete`), append its message, and if it asked for a tool, run every tool
call it made (there can be more than one in a round), append each tool's result as a
`role: "tool"` message, and go again — up to `MAX_TOOL_ROUNDS` times. It returns the
final plain-text answer, the sources gathered along the way, and which tools ran (for
`--debug` and the log). It never raises: a chat failure or an exhausted round budget
both return a message instead.

## The demo front end

`scripts/agent_ui.py` is a Streamlit chat page over the same `ask()`; it adds no logic.

```bash
pip install streamlit          # team repo; with uv (personal repo): uv add streamlit
streamlit run scripts/agent_ui.py
```

Under each answer it shows the sources the tools cited, and a **What happened** table:
every model call (with seconds) and every tool call (the arguments the model sent, ok or
refused, seconds), in order. The sidebar has the kill-shot questions as buttons and a
New conversation button. Its trace comes from the same events as the log, collected per
question, and the page also appends them to `logs/agent_app.jsonl` (run from the folder
you start Streamlit in), so the file has the same lines as the table. `tests/test_agent_ui.py`
(4 tests, no Streamlit needed) covers the trace table; the page itself is checked by
running it. Setting `APP_PASSWORD` puts a password box in front of the page, and
`AGENT_LOG_FILE=off` stops it writing the file (it still logs to stderr). To host it, see
[`agent_ui.md`](agent_ui.md#deploying-on-google-cloud-run).

## Logging (toward #7, observability)

Every call to `run_turn` logs, as one JSON line per event, on the `agent_app` logger.
Every line carries a UTC `ts` (ISO 8601, milliseconds):

- `tool_call`: one per tool the model called: tool name, the **arguments the model sent**
  (truncated to 500 characters), `ok`, the `error_code` when the tool refused (e.g.
  `needs_clarification`), number of sources, and how long the tool took. This is how you
  see what the model actually asked for.
- `chat_call`: round number, model, prompt/completion tokens, which tools were
  requested, and how long the call took.
- `chat_error`: a failed chat call and why.
- `scale_guess_blocked`: the model gave a severity scale the user never stated; it was removed before the tool ran.
- `clarification_returned`: a tool asked a question and the loop passed it on.
- `max_rounds_exceeded`: the loop was stopped rather than left to run forever.
- `turn`: one line per question — the question (truncated), every tool called, how
  many sources came back.

By default this goes to `logs/agent_app.jsonl` (`--log-file ''` sends it to stderr
instead). Per-tool detail (the actual SQL, row counts, timing) is already logged
separately by `agent_tools.run_tool` on the `agent_tools` logger — the two logs
together give the full per-request trace the checklist asks for. **Logs stay in a file, not the database:** `agent_ro` is read-only by design, and logging
to the DB would need a second writable account and a retention decision for the user's
questions. **Not yet done:**
tracking cumulative spend against the team budget from this log (the llm proxy's
own `spend_report.py` / dashboard covers this today; a running total from the app's
own logs would close the gap for weekly reporting).

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

```bash
# save the script below as read_log.py, then:
python read_log.py < logs/agent_app.jsonl                    # the local file
tail -n 50 logs/agent_app.jsonl | python read_log.py          # only the latest
```

```python
import json, sys
from datetime import datetime

def events(text):
    """JSON lines from agent_app.jsonl, or the output of `gcloud logging read --format=json`."""
    text = text.strip()
    if text.startswith("["):                                   # gcloud JSON: a list of entries
        for entry in reversed(json.loads(text)):               # gcloud lists newest first
            e = entry.get("jsonPayload") or json.loads(entry.get("textPayload", "{}"))
            if "event" in e:
                yield e
    else:
        for line in text.splitlines():
            if line.startswith("{"):
                yield json.loads(line)

def turns(evts):
    """A `turn` event closes a question: everything since the previous one belongs to it."""
    cur = []
    for e in evts:
        cur.append(e)
        if e["event"] == "turn":
            yield cur
            cur = []

def when(e):
    return datetime.fromisoformat(e["ts"])

for t in turns(events(sys.stdin.read())):
    last = t[-1]
    start = when(t[0]).timestamp() - t[0].get("ms", 0) / 1000     # events are stamped at their END
    print(f'\nQ: {last["question"]}   ({when(last).strftime("%H:%M:%S")} UTC, '
          f'{when(last).timestamp() - start:.1f} s)')
    for e in t[:-1]:
        k = e["event"]
        if k == "chat_call":
            print(f'  model  r{e["round"]}  {e["ms"]/1000:5.1f} s  {e["prompt_tokens"]}+{e["completion_tokens"]} tokens'
                  f'  asked for: {", ".join(e["tool_calls"]) or "(answered)"}')
        elif k == "tool_call":
            state = "ok" if e["ok"] else "REFUSED " + str(e["error_code"])
            print(f'  tool   r{e["round"]}  {e["ms"]/1000:5.2f} s  {e["tool"]}  {state}  sources={e["n_sources"]}')
            print(f'         args: {e["arguments"]}')
        else:
            print(f'  **     {k}: {e.get("arguments", "")}')
    print(f'  => tools: {last["tools_called"]}, sources: {last["n_sources"]}')
```

It prints one block per question, and the example above comes out as:

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
| `args:` | The arguments the model sent, truncated at 500 characters. Here it sent `severity_scale: 5_is_worst` although the user never said which scale: the guess the guard now blocks |
| `REFUSED needs_clarification` | The tool declined and the user was asked instead of getting a number |
| `** scale_guess_blocked` | The model's scale had no quote from the user, so it was removed before the tool ran. Its arguments are shown on that line, the arguments the tool actually got are on the `tool` line below it |
| `** clarification_returned` | The loop passed the tool's question straight to the user, with no second model call |
| `sources=0` after a number is given | A number with no source: investigate |

**On the hosted page** the same lines go to Cloud Logging (stderr). Read the latest ones into
the same script (the exact filter has not been run against the live service; if the fields
come back as `textPayload` instead of `jsonPayload`, the script reads both):

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="jme-agent"'     --freshness=1h --limit=200 --format=json | python read_log.py
```

Or open **Cloud Run, jme-agent, Logs** in the console and search for `turn`.

**Limits:** lines carry no session id. If two people use the hosted page at the same moment,
their events interleave and the grouping above can mix two questions together. For a one-person
demo or a local run this doesn't matter; adding a turn id to every event would fix it.

## Why this loop, and not LangGraph yet

A plain function-calling loop was built first because it's the smallest thing that
answers a question end to end, grounded and cited — nothing else in the checklist
(evals, guardrails) has anything to run against until this exists. LangGraph adds
real value on top (a checkpointer for #5, explicit guardrail nodes for #3), but none
of that is needed to get a working demo; it's the next layer, not a prerequisite for
this one. See the task list for when it gets built.

## Tests

`tests/test_agent_app.py` (18 tests, no database, no real LLM call): the loop's own
logic only — `agent_tools.run_tool` and the chat call are both stubbed, since tool
correctness is already covered by `tests/test_agent_tools.py`. Covers: a plain
text answer needing no tool; one tool call round-tripping into the next model call;
several tool calls in one round all running and all being cited; a failed chat call
degrading to a plain message rather than raising; too many tool-call rounds stopping
with an honest message rather than looping forever; the assistant-message shape
matching what the API expects, with and without tool calls; source label
deduplication, keeping first-seen order; a new conversation starting with only the
system prompt; and `ask()` appending the question, returning formatted sources, and
a severity scale the user never named (no quote, an invented quote, a quote naming no scale) being stripped so the tool asks, and one the user did name passing; a clarification going straight to the user with no further model call; logging one `turn` summary line; every log line carrying a timestamp and each tool call logging its arguments and refusal code. One test documents, rather than hides, that a bug
in `run_tool` itself (not a normal tool error) still propagates today — a deliberate
choice to surface in a test, not a gap to silently paper over.

**Not covered:** a real call to the llm proxy. Before the demo, run
`python scripts/agent_app.py --debug "<question>"` against the real database and
proxy for each of the trap-list kill-shot questions, same as the real-data
verification pass for `agent_tools.py` ([`agent_tools.md`](agent_tools.md#checked-against-the-real-database-5-nov-2026)).