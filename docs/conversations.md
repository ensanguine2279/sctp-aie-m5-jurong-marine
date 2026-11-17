# conversations.py: conversations in Postgres, with LangGraph's checkpointer

Since 13 Nov 2026 the API keeps conversations the way production LangGraph apps do: the
agent's state is saved by LangGraph's **checkpointer**, one **thread** per conversation, in
Postgres. A small **threads table** next to it holds what a front end lists and what retention
needs. Since 15 Nov 2026 (phase 3) this is the only store of conversations: the page calls the
API for everything, and [`sessions.py`](sessions.md), the page's own MySQL store, is retired.

```mermaid
flowchart LR
    API["agent_api.py<br/>session_id = thread_id"] --> C["Conversations<br/>(conversations.py)"]
    C --> G["agent graph<br/>compiled once with the checkpointer"]
    G <-->|"state per thread<br/>saved when a turn finishes"| CP[("agent.checkpoints, ...<br/>PostgresSaver")]
    C <-->|"title, turns, display copy,<br/>updated_at"| T[("agent.agent_threads")]
    P["purge (7 days)"] --> T
    P -->|delete_thread| CP
```

| Part | What it holds | Why |
|---|---|---|
| Checkpoints (`PostgresSaver`, [langgraph-checkpoint-postgres](https://pypi.org/project/langgraph-checkpoint-postgres/)) | The graph's state per thread: the conversation in the chat API's format, and the last turn's results | A follow-up runs from the thread's saved state: no front end passes the history back. LangChain's official checkpointer for production |
| `agent_threads` | Per conversation: `title` (its first question), `turns`, `display` (what the page draws: each answer with its sources, caveats, cost and the scans it cites, `answer_view.display_copy`), `created_at`, `updated_at` | The recent list, retention and deletion. Open-source checkpointers have no expiry (TTL), and no cheap "newest conversations" query |

Both live in one Postgres database, in a schema of their own, `agent`. The shop's data stays
in MySQL; this database holds only conversations.

## What is never saved (PDPA)

The checkpointer saves whatever is in the graph's state, so nothing that mustn't be kept is
ever put there. Each rule below is tested, by reading the state the checkpointer holds:

- **A refused question.** The question as typed travels in the run's context
  (`AgentContext.question`), which LangGraph never saves, like the database connection.
  `guard_in` writes to the state only what may be kept: the question if the guardrail allows
  it, otherwise only its placeholder, `[question withheld by the guardrail: pdpa]`. *Found
  while writing the tests: the first version kept the question as typed in the state's
  `question` key.*
- **An answer with personal data.** The `model` node runs the output guardrail on the
  answer before putting it in the state. So no snapshot holds it, even part way through a
  turn.
- **Tool results.** These come from the `agent_ro` views, which can't read names
  ([database access](../README.md#database-access-for-the-app-pdpa)).

**When a turn is saved.** The graph runs with `durability="exit"`, so a thread is saved
once, when the turn finishes ([LangGraph docs](https://docs.langchain.com/oss/python/langgraph/checkpointers)).
The docs don't say what is kept of a turn that raises half way. The agent doesn't rely on it
either way: `guard_in` mends the history at the start of every turn (`answered_only`: a tool
call with no result is dropped), and the rules above keep anything unsafe out of the state at
every step.

**The system prompt comes from the code, every turn.** A thread that was saved keeps its
conversation, but `guard_in` replaces its first message with the current prompt and today's
dates. A prompt change applies to old conversations too.

## Setup

### Locally: Postgres in Docker

Add the service in `claude/docker-compose.postgres.yml` (in the project) to
`docker-compose.yml`, next to `mysql`, with its `pg-data` volume. Then add to `.env`:

```bash
CONV_DB_NAME=jme_agent
CONV_DB_ADMIN_USERNAME=postgres
CONV_DB_ADMIN_PASSWORD=...                  # the container's admin
CONV_DB_ADMIN_URL=postgresql://postgres:ADMINPW@localhost:5433/jme_agent
CONV_DB_URL=postgresql://agent_threads:AGENTPW@localhost:5433/jme_agent
AGENT_CONVERSATION_DAYS=7                   # optional
```

The `CONV_DB_` prefix is this database's, as `CMMS_DB_` is the shop's (14 Nov 2026,
[Settings](../README.md#settings-one-prefix-per-database)). Until then these were `PG_DB`,
`PG_ADMIN_USER`, `PG_ADMIN_PASSWORD`, `PG_ADMIN_URL` and `AGENT_PG_URL`. Only the new
names are read: with an old one alone, conversations are off and `/health` says
`CONV_DB_URL isn't set`.

A password with `@`, `:` or `/` in it must be URL-encoded in the two URLs (`@` is `%40`).

```bash
docker compose up -d postgres
uv add fastapi uvicorn langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool   # personal repo
pip install -r requirements-api.txt                                                  # team repo
python scripts/conversations.py setup --dry-run   # the SQL, password hidden
python scripts/conversations.py setup             # as the admin
python scripts/conversations.py check             # as the agent user: must say "All good."
```

(With uv: `uv run python scripts/conversations.py ...`.)

### Hosted: Aiven Postgres

Create a Postgres service on Aiven. Check the plan's terms before you do. Copy its
**Service URI** (user `avnadmin`, `?sslmode=require`) into `CONV_DB_ADMIN_URL`. Set
`CONV_DB_URL` to the same host, port and database with `agent_threads` and its own password,
also ending in `?sslmode=require`. Then run `setup` and `check` exactly as above. For the
hosted API, `CONV_DB_URL` goes in Secret Manager like the other passwords, and the admin URL
never leaves your machine.

### What setup does

As the admin, `setup`:
1. creates the `agent` schema;
2. creates the `agent_threads` user if it doesn't exist (a login with no superuser, no
   `CREATEDB` and no `CREATEROLE`), then sets its password, quoted by Postgres' rules, and its
   `search_path` to `agent`. The attributes are set only when the user is created: Aiven's
   `avnadmin` isn't a superuser, and Postgres refuses a non-superuser any `ALTER ROLE` that
   names `SUPERUSER`, even `NOSUPERUSER` (found on Aiven, 15 Nov 2026);
3. creates the threads table;
4. runs `PostgresSaver.setup()` in the `agent` schema, which makes LangGraph's checkpoint
   tables and runs their migrations;
5. grants the agent user `SELECT, INSERT, UPDATE, DELETE` on the tables in `agent`, and
   nothing else.

The agent user can't create, drop or alter anything, so the app never runs migrations
itself. After upgrading `langgraph-checkpoint-postgres`, run `setup` again: it's safe to repeat,
and it applies any new migrations and grants.

`check` connects **as the agent user** and must show:
- saving, reading and deleting a conversation, and reading checkpoints: `ok`;
- making a table (in `agent` or in `public`), dropping one, and making a role: `ok … refused`.

Anything else is a `FAIL`, and `check` exits 1. It's the evidence for the PDPA question about
the conversation store, the way `agent_user.py --check` is for the shop's data.

## Retention

Conversations unused for `AGENT_CONVERSATION_DAYS` (7) are deleted: their checkpoints
(`delete_thread`) and their row. This runs once at every API start, and by hand with
`python scripts/conversations.py purge`. A new conversation's row is written **before** its
first question runs, so its checkpoints are always covered by retention, even if that turn
fails. A row whose first question never finished isn't listed (`turns = 0`).

## In the API

`session_id` is the thread's id: 32 random hex characters, and never anything else
(`valid_id`) reaches the database or the checkpointer. For each question, the API:
1. takes the conversation's lock;
2. runs the graph on its thread (`Conversations.ask`), with no history passed in;
3. writes the row: title, turns, and the display copy with the new answer.

`GET /sessions` and `GET /sessions/{id}` read the threads table; `DELETE /sessions/{id}`
deletes a conversation's checkpoints and its row (15 Nov 2026). Without `CONV_DB_URL`, or
with the packages or the database missing, the API still answers, with conversations off, and
`/health` says why. Details: [`agent_api.md`](agent_api.md).

## Tests

`tests/test_conversations.py` (45 tests; no Postgres: the table on sqlite3 through the same
`run(sql, params)` shape, LangGraph's in-memory checkpointer, and a stand-in that answers with
Postgres' error codes):
- **ids:** random, 32 hex, nothing else accepted; titles shortened.
- **the table:**
  - a row made, updated (created_at kept) and read back;
  - an unknown or bad id not found and never sent;
  - an unreadable row starting afresh;
  - a down database never raising and naming only the error type;
  - the recent list (newest first, within the retention, only rows with a question, at most
    ten);
  - delete (checkpoints and row; the row kept if the checkpoints couldn't be deleted);
  - purge by age only; the retention setting; named parameters for psycopg.
- **the checkpointed agent:**
  - a thread keeping its conversation, with the system prompt from the code;
  - an old prompt replaced;
  - threads kept apart;
  - **no checkpoint holding a blocked answer or a refused question**;
  - a turn cut off half way leaving the thread working, with no tool call without a result
    reaching the model;
  - delete and purge reaching the checkpointer;
  - a bad id never reaching the graph.
- **settings:** URLs read and shown without the password; off without `CONV_DB_URL`, saying
  why; the old name `AGENT_PG_URL` alone not enough, the message holding no password.
- **setup:** one schema, one user, grants only inside it, the checkpoint tables before the
  grants; a password with a quote kept as a safe literal; odd names refused; the dry run
  without the password; the CLI saying what to set.
- **check:** passing only when its own tables work; failing (and cleaning up) if the agent
  user can change the schema; an odd error counted as a failure.

**Run on real Postgres** (written in a sandbox without it): `setup` and `check` passed
locally (Docker) and on Aiven Postgres (14-15 Nov 2026), and the API's follow-up and restart
were checked on both. Run `check` again after any change to the agent user or the schema.