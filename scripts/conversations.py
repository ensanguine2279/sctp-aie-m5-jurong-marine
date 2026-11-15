#!/usr/bin/env python3
"""Conversations kept in Postgres: LangGraph's checkpointer for the agent's state, and a
small threads table for everything a front end lists (13 Nov 2026, replacing sessions.py for
the API; the page follows in phase 3, when it calls the API).

Two parts, both in one Postgres database, in a schema of their own (`agent`):

    checkpoints     LangGraph's PostgresSaver (langgraph-checkpoint-postgres, the official
                    one LangChain recommends for production): the graph's state per thread,
                    so a follow-up has the whole conversation. The graph saves only when a
                    turn finishes (durability="exit"), and no unchecked text is ever in the
                    state: the model's answer passes the output guardrail inside the `model`
                    node, and a refused question is only a placeholder (agent_graph.py).
    agent_threads   one row per conversation: its title (the first question, a refused one
                    only as its placeholder), how many questions, the display copy a front
                    end draws (the answer, its sources, caveats and cost, as the page keeps
                    it), and when it was created and last used. The recent list, retention
                    and deletion work from this table: open-source checkpointers have no TTL
                    and no cheap "newest conversations" query.

A conversation's id is its LangGraph thread_id: 32 hex characters, random, never anything
else (valid_id). It is the `session_id` of the API and the page's link.

Its own database user, as sessions.py had (PDPA): `agent_threads` can read and write the
tables in the `agent` schema and nothing else: no CREATE, no other schema. Setup runs as the
database's admin (`setup`, which also runs PostgresSaver.setup() to create the checkpoint
tables), and `check` proves the limits by connecting as the agent user. The shop's data stays
in MySQL; this database holds only conversations.

Retention: conversations unused for AGENT_CONVERSATION_DAYS (7) are deleted, checkpoints and
row (`purge`, run at the API's start and by hand).

Settings (.env)
    CONV_DB_URL        the agent user's URL: postgresql://agent_threads:PW@HOST:PORT/DB
                     (add ?sslmode=require for Aiven)
    CONV_DB_ADMIN_URL  the admin's URL, for setup only (Aiven: the service URI)
    AGENT_CONVERSATION_DAYS   retention in days (default 7)

Usage
    python scripts/conversations.py setup --dry-run   # the SQL, password hidden
    python scripts/conversations.py setup             # as the admin: schema, user, tables
    python scripts/conversations.py check             # as the agent user: limits proven
    python scripts/conversations.py purge             # retention now

Needs (API): langgraph-checkpoint-postgres, psycopg[binary], psycopg-pool.
"""
import argparse
import json
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA = "agent"
TABLE = "agent_threads"
DEFAULT_USER = "agent_threads"
DEFAULT_DAYS = 7
RECENT_MAX = 10
TITLE_CHARS = 80
NO_TITLE = "(no question yet)"
_ID = re.compile(r"[0-9a-f]{32}")
_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# what the agent user may do (all inside the agent schema) and what it must be refused
DDL = (f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (\n"
       "  thread_id CHAR(32) PRIMARY KEY,\n"
       "  title TEXT NOT NULL,\n"
       "  turns INTEGER NOT NULL,\n"
       "  display TEXT NOT NULL,\n"
       "  created_at TIMESTAMPTZ NOT NULL,\n"
       "  updated_at TIMESTAMPTZ NOT NULL)",
       f"CREATE INDEX IF NOT EXISTS {TABLE}_updated ON {SCHEMA}.{TABLE} (updated_at)")


# ======================================================================= ids ===

def new_id() -> str:
    """A new conversation id (the LangGraph thread_id): 32 random hex characters."""
    return secrets.token_hex(16)


def valid_id(sid) -> bool:
    """Only an id new_id() could have made reaches the database or the checkpointer."""
    return isinstance(sid, str) and _ID.fullmatch(sid) is not None


def title_of(messages) -> str:
    """A conversation's title: its first question, spaces collapsed, cut to TITLE_CHARS.
    From the graph's messages, so a refused question is only its placeholder."""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            t = " ".join(m["content"].split())
            return t if len(t) <= TITLE_CHARS else t[:TITLE_CHARS - 1].rstrip() + "…"
    return NO_TITLE


def turns_of(messages) -> int:
    return sum(1 for m in messages or [] if isinstance(m, dict) and m.get("role") == "user")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(t: datetime) -> str:
    """A time as text both Postgres (TIMESTAMPTZ) and SQLite (the tests) compare correctly."""
    return t.astimezone(timezone.utc).isoformat(sep=" ", timespec="microseconds")


# ================================================================= the table ===

@dataclass
class Thread:
    """One conversation's row, as get() reads it. ok False: the store couldn't be read."""
    ok: bool
    found: bool = False
    title: str = NO_TITLE
    turns: int = 0
    display: list = field(default_factory=list)
    error: str | None = None


@dataclass
class Recent:
    """One line of the recent list (the same fields as sessions.Recent, for the page)."""
    sid: str
    title: str
    turns: int
    updated_at: Any


def _why(action: str, e: Exception) -> str:
    """An error for the user: what failed and the error's type, never its text (which can
    hold a host, a user or SQL)."""
    return f"couldn't {action} ({type(e).__name__})"


class ThreadStore:
    """The agent_threads table, through run(sql, params) -> (rows as dicts, rowcount): psycopg
    on Postgres (pg_run), sqlite3 in the tests (sqlite_run). SQL uses :name parameters.
    Never raises: every method says what went wrong instead."""

    def __init__(self, run: Callable, *, days: int = DEFAULT_DAYS, table: str = TABLE):
        if not re.fullmatch(r"[a-z_][a-z0-9_.]{0,126}", table):
            raise ValueError(f"not a table name: {table!r}")
        self.run, self.days, self.table = run, max(1, int(days)), table

    def get(self, sid) -> Thread:
        if not valid_id(sid):
            return Thread(True, error="not a conversation id")
        try:
            rows, _ = self.run(f"SELECT title, turns, display FROM {self.table} "
                               "WHERE thread_id = :sid", {"sid": sid})
        except Exception as e:
            return Thread(False, error=_why("read the conversation", e))
        if not rows:
            return Thread(True)
        r = rows[0]
        try:
            display = json.loads(r["display"])
            if not isinstance(display, list):
                raise ValueError("display is not a list")
        except (TypeError, ValueError):
            return Thread(True, error="the saved conversation can't be read; starting afresh")
        return Thread(True, True, r["title"], int(r["turns"]), display)

    def touch(self, sid, title: str, turns: int, display: list) -> tuple[bool, str | None]:
        """Create the row, or update it: title, turns, display and updated_at. created_at is
        kept. Called before a conversation's first question (so its checkpoints are always
        covered by retention, even if the turn fails) and after every answer."""
        if not valid_id(sid):
            return False, "not a conversation id"
        now = _stamp(_now())
        try:
            self.run(f"INSERT INTO {self.table} "
                     "(thread_id, title, turns, display, created_at, updated_at) "
                     "VALUES (:sid, :title, :turns, :display, :now, :now) "
                     "ON CONFLICT (thread_id) DO UPDATE SET title = excluded.title, "
                     "turns = excluded.turns, display = excluded.display, "
                     "updated_at = excluded.updated_at",
                     {"sid": sid, "title": title, "turns": int(turns), "now": now,
                      "display": json.dumps(display, ensure_ascii=False, default=str)})
        except Exception as e:
            return False, _why("save the conversation", e)
        return True, None

    def recent(self, n: int, now: datetime | None = None) -> list[Recent] | None:
        """The newest conversations, at most RECENT_MAX, none older than the retention and
        none without a question yet. None when the store can't be read."""
        n = max(0, min(int(n), RECENT_MAX))
        if not n:
            return []
        cutoff = _stamp((now or _now()) - timedelta(days=self.days))
        try:
            rows, _ = self.run(f"SELECT thread_id, title, turns, updated_at FROM {self.table} "
                               "WHERE updated_at >= :cutoff AND turns > 0 "
                               f"ORDER BY updated_at DESC LIMIT {n}", {"cutoff": cutoff})
        except Exception:
            return None
        return [Recent(r["thread_id"], r["title"], int(r["turns"]), r["updated_at"])
                for r in rows if valid_id(r["thread_id"])]

    def delete(self, sid, checkpointer=None) -> tuple[bool, str | None]:
        """Delete one conversation: its checkpoints, then its row. One that isn't there
        counts as deleted."""
        if not valid_id(sid):
            return False, "not a conversation id"
        try:
            if checkpointer is not None:
                checkpointer.delete_thread(sid)
            self.run(f"DELETE FROM {self.table} WHERE thread_id = :sid", {"sid": sid})
        except Exception as e:
            return False, _why("delete the conversation", e)
        return True, None

    def purge(self, checkpointer=None, now: datetime | None = None) -> int | None:
        """Delete every conversation unused for longer than the retention, checkpoints and
        row. How many, or None when it couldn't."""
        cutoff = _stamp((now or _now()) - timedelta(days=self.days))
        try:
            rows, _ = self.run(f"SELECT thread_id FROM {self.table} WHERE updated_at < :cutoff",
                               {"cutoff": cutoff})
            done = 0
            for r in rows:
                if valid_id(r["thread_id"]):
                    ok, _ = self.delete(r["thread_id"], checkpointer)
                    done += ok
            return done
        except Exception:
            return None


# ============================================================ the conversations ===

class Conversations:
    """The checkpointed agent graph and its threads table, together: what the API holds.
    `ask` runs one question on a thread; the state comes from, and goes back to, the
    checkpointer."""

    def __init__(self, threads: ThreadStore, checkpointer):
        import agent_graph
        self.threads, self.checkpointer = threads, checkpointer
        self.graph = agent_graph.build(checkpointer)      # compiled once, for every thread

    @staticmethod
    def config(sid: str) -> dict:
        return {"configurable": {"thread_id": sid}}

    def messages(self, sid) -> list:
        """The thread's conversation as the checkpointer holds it ([] for none)."""
        if not valid_id(sid):
            return []
        state = self.graph.get_state(self.config(sid))
        return list((getattr(state, "values", None) or {}).get("messages") or [])

    def ask(self, sid, question: str, context, on_step=None) -> dict:
        """One question on thread `sid`: the graph runs from the thread's saved state and
        saves its final state when the turn finishes (durability="exit"). Returns the final
        state."""
        import agent_graph
        if not valid_id(sid):
            raise ValueError("not a conversation id")
        return agent_graph.run(None, question, context, on_step=on_step, compiled=self.graph,
                               config=self.config(sid), durability="exit")

    def delete(self, sid) -> tuple[bool, str | None]:
        return self.threads.delete(sid, self.checkpointer)

    def purge(self, now: datetime | None = None) -> int | None:
        return self.threads.purge(self.checkpointer, now)


# ================================================================== running SQL ===

def _named(sql: str) -> str:
    """:name -> %(name)s, for psycopg. The SQL here never uses '::' casts."""
    return re.sub(r"(?<!:):([a-z_][a-z0-9_]*)", r"%(\1)s", sql)


def pg_run(pool) -> Callable:
    """run(sql, params) on a psycopg ConnectionPool (autocommit, dict rows)."""
    def run(sql, params=None):
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_named(sql), params or {})
            rows = cur.fetchall() if cur.description else []
            return [dict(r) for r in rows], cur.rowcount
    return run


def sqlite_run(conn) -> Callable:
    """run(sql, params) on a sqlite3 connection (the tests)."""
    def run(sql, params=None):
        cur = conn.execute(sql.replace(f"{SCHEMA}.", ""), params or {})
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description] if cur.description else []
        conn.commit()
        return [dict(zip(cols, r)) for r in rows], cur.rowcount
    return run


SQLITE_DDL = (f"CREATE TABLE {TABLE} (thread_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
              "turns INTEGER NOT NULL, display TEXT NOT NULL, created_at TEXT NOT NULL, "
              "updated_at TEXT NOT NULL)")


# ================================================================ settings ===

def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    v = v.strip().rstrip("\r") if v is not None else None
    return v or default


def days_setting() -> int:
    try:
        return max(1, int(_env("AGENT_CONVERSATION_DAYS", str(DEFAULT_DAYS))))
    except ValueError:
        return DEFAULT_DAYS


@dataclass
class PgUrl:
    user: str
    password: str | None
    host: str
    port: int
    dbname: str
    raw: str

    def hidden(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.dbname} (password hidden)"


def parse_url(url: str | None) -> PgUrl | None:
    """A postgresql:// URL's parts, or None if it isn't one."""
    if not url:
        return None
    u = urlsplit(url)
    if u.scheme not in ("postgresql", "postgres") or not u.hostname or not u.username:
        return None
    return PgUrl(unquote(u.username), unquote(u.password) if u.password else None,
                 u.hostname, u.port or 5432, (u.path or "/").lstrip("/") or "postgres", url)


def from_env():
    """(Conversations, None), or (None, why not). Never raises: without CONV_DB_URL, or with
    the packages or the database missing, the API runs with conversations off and says why."""
    url = parse_url(_env("CONV_DB_URL"))
    if url is None:
        return None, "CONV_DB_URL isn't set"
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as e:
        return None, f"a package is missing ({e.name}): langgraph-checkpoint-postgres, psycopg, psycopg-pool"
    try:
        pool = ConnectionPool(url.raw, min_size=1, max_size=5, open=True, timeout=10,
                              kwargs={"autocommit": True, "row_factory": dict_row})
        saver = PostgresSaver(pool)
        convs = Conversations(ThreadStore(pg_run(pool), days=days_setting(),
                                          table=f"{SCHEMA}.{TABLE}"), saver)
        if convs.threads.recent(1) is None:          # reachable and set up?
            return None, "the conversations database can't be read (run `conversations.py check`)"
        return convs, None
    except Exception as e:
        return None, _why("open the conversations database", e)


# =================================================================== setup ===

def setup_statements(dbname: str, user: str, password: str, hide: bool = False) -> list[str]:
    """The admin's SQL: the schema, the agent user (its password set, quoted as a literal by
    Postgres' own rules), the threads table and the grants. Only this schema; no CREATE."""
    for what, v in (("database", dbname), ("user", user)):
        if not _NAME.fullmatch(v or ""):
            raise ValueError(f"not a plain {what} name: {v!r}")
    pw = "****" if hide else "'" + password.replace("'", "''") + "'"
    return [
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        # The role's attributes are set when it's created: a non-superuser admin (Aiven's
        # avnadmin) may create a role NOSUPERUSER, but not name SUPERUSER in an ALTER ROLE,
        # even to say NO (Postgres 16: "Only roles with the SUPERUSER attribute may change
        # the SUPERUSER attribute"; found on Aiven, 15 Nov 2026). The ALTER, which runs
        # every time, sets only the login and the password.
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{user}') "
        f"THEN CREATE ROLE {user} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE; END IF; END $$",
        f"ALTER ROLE {user} WITH LOGIN PASSWORD {pw}",
        f"ALTER ROLE {user} SET search_path = {SCHEMA}",
        *DDL,
        "-- PostgresSaver.setup(): LangGraph's checkpoint tables, in the same schema",
        f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC",
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {user}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {SCHEMA} TO {user}",
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {user}",
        f"GRANT CONNECT ON DATABASE {dbname} TO {user}",
    ]


def setup(admin_url: PgUrl, agent_url: PgUrl, out=print) -> bool:
    """Run the setup as the admin. The checkpoint tables are made by PostgresSaver.setup(),
    on a connection whose search_path is the agent schema, before the grants."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    stmts = setup_statements(agent_url.dbname, agent_url.user, agent_url.password or "")
    with psycopg.connect(admin_url.raw, autocommit=True, row_factory=dict_row,
                         options=f"-c search_path={SCHEMA}") as conn:
        grants_at = next(i for i, s in enumerate(stmts) if s.startswith("-- PostgresSaver"))
        for s in stmts[:grants_at]:
            conn.execute(s)
        PostgresSaver(conn).setup()
        out("ok      LangGraph checkpoint tables (PostgresSaver.setup)")
        for s in stmts[grants_at + 1:]:
            conn.execute(s)
    out(f"ok      schema {SCHEMA}, user {agent_url.user}, {TABLE}, grants")
    return True


# =================================================================== check ===

ALLOWED_PROBES = [
    ("save a conversation", f"INSERT INTO {SCHEMA}.{TABLE} (thread_id, title, turns, display, "
                            f"created_at, updated_at) VALUES ('{'0' * 32}', 'probe', 0, '[]', "
                            "now(), now()) ON CONFLICT (thread_id) DO NOTHING"),
    ("read conversations", f"SELECT thread_id FROM {SCHEMA}.{TABLE} LIMIT 1"),
    ("delete a conversation", f"DELETE FROM {SCHEMA}.{TABLE} WHERE thread_id = '{'0' * 32}'"),
    ("read checkpoints", f"SELECT thread_id FROM {SCHEMA}.checkpoints LIMIT 1"),
]
FORBIDDEN_PROBES = [
    ("making a table", f"CREATE TABLE {SCHEMA}.agent_threads_probe (x int)"),
    ("dropping a table", f"DROP TABLE {SCHEMA}.{TABLE}"),
    ("making a table elsewhere", "CREATE TABLE public.agent_threads_probe (x int)"),
    ("making a role", "CREATE ROLE agent_threads_probe"),
]
DENIED = {"42501", "3F000"}      # insufficient_privilege; a schema it can't see


def _sqlstate(e: Exception) -> str | None:
    return getattr(e, "sqlstate", None) or getattr(getattr(e, "orig", None), "sqlstate", None)


def check(run: Callable, out=print) -> bool:
    """As the agent user: its own tables work, anything else is refused. True when all good."""
    good = True
    for what, sql in ALLOWED_PROBES:
        try:
            run(sql, {})
            out(f"ok      {what}")
        except Exception as e:
            good = False
            out(f"FAIL    {what}: {type(e).__name__} ({_sqlstate(e) or 'no code'})")
    for what, sql in FORBIDDEN_PROBES:
        try:
            run(sql, {})
        except Exception as e:
            if _sqlstate(e) in DENIED:
                out(f"ok      {what}: refused")
            else:
                good = False
                out(f"FAIL    {what}: an unexpected error, {type(e).__name__} "
                    f"({_sqlstate(e) or 'no code'})")
            continue
        good = False
        out(f"FAIL    {what}: the agent user CAN do this")
        if "CREATE TABLE" in sql:                  # undo what it should never have done
            try:
                run("DROP TABLE " + sql.split("CREATE TABLE ", 1)[1].split(" ", 1)[0], {})
            except Exception:
                pass
    return good


# ===================================================================== CLI ===

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "check", "purge"])
    ap.add_argument("--dry-run", action="store_true", help="setup: print the SQL only")
    ap.add_argument("--env-file", default=None)
    a = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(a.env_file) if a.env_file else load_dotenv()
    except ImportError:
        pass
    agent = parse_url(_env("CONV_DB_URL"))
    if agent is None:
        print("Set CONV_DB_URL in .env: postgresql://agent_threads:PASSWORD@HOST:PORT/DB "
              "(see docs/conversations.md).", file=sys.stderr)
        return 2
    if a.command == "setup":
        if not agent.password:
            print("CONV_DB_URL has no password: the agent user needs one.", file=sys.stderr)
            return 2
        if a.dry_run:
            print(f"-- as the admin, on {agent.dbname}\n")
            for s in setup_statements(agent.dbname, agent.user, agent.password, hide=True):
                print(s if s.startswith("--") else s + ";")
            return 0
        admin = parse_url(_env("CONV_DB_ADMIN_URL"))
        if admin is None:
            print("Set CONV_DB_ADMIN_URL in .env (the admin's URL; setup only).", file=sys.stderr)
            return 2
        print(f"Setting up as {admin.hidden()}")
        return 0 if setup(admin, agent) else 1
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        print("psycopg is needed: uv add 'psycopg[binary]' psycopg-pool "
              "langgraph-checkpoint-postgres", file=sys.stderr)
        return 2
    if a.command == "check":
        print(f"Checking as {agent.hidden()}")
        with psycopg.connect(agent.raw, autocommit=True, row_factory=dict_row) as conn:
            def run(sql, params):
                conn.execute(sql, params or None)
                return [], 0
            ok = check(run)
        print("All good." if ok else "Something is wrong: see FAIL above.")
        return 0 if ok else 1
    convs, why = from_env()
    if convs is None:
        print(f"Conversations are off: {why}", file=sys.stderr)
        return 1
    n = convs.purge()
    print(f"Deleted {n} conversation(s) unused for {convs.threads.days} days."
          if n is not None else "Couldn't purge (the database can't be read).")
    return 0 if n is not None else 1


if __name__ == "__main__":
    sys.exit(main())