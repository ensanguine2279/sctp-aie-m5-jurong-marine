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
OPENAI_API_KEY=...                                        # your class proxy member key
OPENAI_BASE_URL=https://35-240-177-226.sslip.io           # the class proxy (llm-budget-proxy-setup.md)
```

## What it guarantees, in code

Three of the [Harness Checklist](HARNESS_CHECKLIST.md) items are enforced here, not
just asked for in the prompt:

| # | What | How |
|---|---|---|
| 1 (grounding) | Every fact comes from a tool call made this turn | The model only ever picks a tool and fills in arguments; `agent_tools.py` validates them and runs the fixed SQL. There is no path from "the model says X" to "the answer says X" that skips a tool |
| 2 (citations) | Every answer can be checked | Every tool result's `sources` are collected across the whole turn (every tool call, not just the last) and shown under the answer as `sources[].label` |
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
tracking cumulative spend against the team budget from this log (the class proxy's
own `spend_report.py` / dashboard covers this today; a running total from the app's
own logs would close the gap for weekly reporting).

## Why this loop, and not LangGraph yet

A plain function-calling loop was built first because it's the smallest thing that
answers a question end to end, grounded and cited — nothing else in the checklist
(evals, guardrails) has anything to run against until this exists. LangGraph adds
real value on top (a checkpointer for #5, explicit guardrail nodes for #3), but none
of that is needed to get a working demo; it's the next layer, not a prerequisite for
this one. See the task list for when it gets built.

## Tests

`tests/test_agent_app.py` (14 tests, no database, no real LLM call): the loop's own
logic only — `agent_tools.run_tool` and the chat call are both stubbed, since tool
correctness is already covered by `tests/test_agent_tools.py`. Covers: a plain
text answer needing no tool; one tool call round-tripping into the next model call;
several tool calls in one round all running and all being cited; a failed chat call
degrading to a plain message rather than raising; too many tool-call rounds stopping
with an honest message rather than looping forever; the assistant-message shape
matching what the API expects, with and without tool calls; source label
deduplication, keeping first-seen order; a new conversation starting with only the
system prompt; and `ask()` appending the question, returning formatted sources, and
a clarification going straight to the user with no further model call; logging one `turn` summary line; every log line carrying a timestamp and each tool call logging its arguments and refusal code. One test documents, rather than hides, that a bug
in `run_tool` itself (not a normal tool error) still propagates today — a deliberate
choice to surface in a test, not a gap to silently paper over.

**Not covered:** a real call to the class proxy. Before the demo, run
`python scripts/agent_app.py --debug "<question>"` against the real database and
proxy for each of the trap-list kill-shot questions, same as the real-data
verification pass for `agent_tools.py` ([`agent_tools.md`](agent_tools.md#checked-against-the-real-database-5-nov-2026)).