#!/usr/bin/env python3
"""Conversations that survive a restart (HARNESS_CHECKLIST.md section 5).

The agent's conversation is a list of messages (agent_app.ask() appends to it). This keeps
that list in MySQL, one row per conversation in `agent_sessions`, saved after every answer
and loaded again when the page starts, so restarting the app, or Cloud Run replacing the
container, loses nothing: the next question is asked with the whole earlier conversation,
and "and what about CNC-03?" still resolves.

Who can touch it (PDPA, the same idea as agent_user.py):

    agent_sessions   a separate MySQL user that can SELECT, INSERT, UPDATE and DELETE on
                     the agent_sessions table and nothing else: no shop data, no employee
                     names, no other table, no schema changes
    agent_ro         unchanged, and never granted agent_sessions: the model's tools,
                     query_sql included, can't read anyone's conversation

What is stored: the messages without the system prompt (always loaded fresh from code), and
a light copy of what the page shows under each answer (text, sources, caveats, cost; no
photos). A question the guardrail refused is already only a placeholder in the messages,
and an answer it blocked is already replaced. The page puts the same placeholder in its
display copy (agent_ui.kept_question), so a refused question is never stored as typed.
Conversations not touched for AGENT_SESSION_DAYS days (default 7) are deleted.

The page's sidebar lists the newest conversations (SessionStore.recent). Each is titled by
its first question as the MESSAGES keep it, so a refused question shows only as its
placeholder there too.

Settings (the same .env as the app):

    DB_HOST, DB_PORT, DB_NAME          as for the app
    AGENT_SESSION_DB_USERNAME          default agent_sessions
    AGENT_SESSION_DB_PASSWORD          required for saving
    AGENT_SESSION_DAYS                 retention in days (default 7)
    DB_ADMIN_USERNAME / DB_ADMIN_PASSWORD   for `setup` only (as agent_user.py)

Usage
    python scripts/sessions.py setup --dry-run   # the table, the user and the grants; no database
    python scripts/sessions.py setup             # create them (as the admin), then check
    python scripts/sessions.py check             # prove what the session user can and can't do
    python scripts/sessions.py purge             # delete conversations older than the retention

With uv (personal repo): `uv run python scripts/sessions.py ...`.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

TABLE = "agent_sessions"
DEFAULT_USER = "agent_sessions"
DEFAULT_DAYS = 7
SESSION_ID = re.compile(r"[0-9a-f]{32}")
ROLES = {"user", "assistant", "tool"}          # what may come back from the store

MYSQL_DDL = f"""CREATE TABLE IF NOT EXISTS `{TABLE}` (
  session_id  CHAR(32)    NOT NULL PRIMARY KEY,
  messages    MEDIUMTEXT  NOT NULL,
  display     MEDIUMTEXT  NOT NULL,
  turns       INT         NOT NULL,
  created_at  DATETIME    NOT NULL,
  updated_at  DATETIME    NOT NULL,
  KEY ix_agent_sessions_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

# run(sql, params) -> (rows as dicts, rowcount). MySQL through SQLAlchemy in the app,
# sqlite3 in the tests: the same SQL and :name parameters run on both.
Run = Callable[[str, dict], "tuple[list[dict], int]"]


def new_session_id() -> str:
    return secrets.token_hex(16)


def valid_id(sid) -> bool:
    return isinstance(sid, str) and bool(SESSION_ID.fullmatch(sid))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)   # DATETIME, UTC


def messages_ok(messages) -> bool:
    """A stored message list the app may use: a list of dicts, each user, assistant or tool.
    A stored system message makes it unusable (the prompt only ever comes from code)."""
    return isinstance(messages, list) and all(
        isinstance(m, dict) and m.get("role") in ROLES for m in messages)


TITLE_CHARS = 60
RECENT_MAX = 10


def title_of(messages: list[dict]) -> str:
    """A conversation's title: its first question as the messages keep it. A question the
    guardrail refused is only its placeholder there, so a name typed into it never shows."""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            t = " ".join(str(m.get("content") or "").split())
            if t:
                return t if len(t) <= TITLE_CHARS else t[:TITLE_CHARS - 1].rstrip() + "…"
    return "(no question yet)"


# ============================================================== the store ===

@dataclass
class Loaded:
    ok: bool                                   # False: the store couldn't be read
    found: bool = False                        # True: a saved conversation came back
    messages: list[dict] = field(default_factory=list)   # without the system prompt
    display: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class Recent:
    """One saved conversation in the sidebar's list."""
    sid: str
    title: str
    turns: int
    updated_at: datetime | str | None          # UTC; a string on sqlite (the tests)


class SessionStore:
    """Load and save conversations. Never raises: a down database or a bad row comes back
    as ok=False / found=False with a reason, and the page carries on in memory (#4)."""

    def __init__(self, run: Run, days: int = DEFAULT_DAYS, table: str = TABLE):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", table):
            raise ValueError(f"Invalid table name: {table!r}")
        self._run, self.days, self.table = run, days, table

    def load(self, sid: str) -> Loaded:
        if not valid_id(sid):
            return Loaded(True, error="not a session id")
        try:
            rows, _ = self._run(f"SELECT messages, display FROM {self.table} "
                                f"WHERE session_id = :sid", {"sid": sid})
        except Exception as e:                 # down, refused, timed out
            return Loaded(False, error=f"couldn't read saved conversations ({type(e).__name__})")
        if not rows:
            return Loaded(True)
        try:
            messages = json.loads(rows[0]["messages"])
            display = json.loads(rows[0]["display"])
            if not messages_ok(messages):
                raise ValueError("bad messages")
            if not isinstance(display, list):
                raise ValueError("bad display")
        except (ValueError, TypeError, KeyError):
            return Loaded(True, error="the saved conversation couldn't be read; starting afresh")
        return Loaded(True, True, messages, display)

    def save(self, sid: str, messages: list[dict], display: list[dict]) -> tuple[bool, str | None]:
        """Store the conversation (the system prompt left out). (ok, error)."""
        if not valid_id(sid):
            return False, "not a session id"
        body = [m for m in messages if m.get("role") != "system"]
        params = {"sid": sid, "messages": json.dumps(body, default=str, ensure_ascii=False),
                  "display": json.dumps(display, default=str, ensure_ascii=False),
                  "turns": sum(1 for m in body if m.get("role") == "user"), "now": _now()}
        try:
            _, n = self._run(f"UPDATE {self.table} SET messages = :messages, display = :display, "
                             f"turns = :turns, updated_at = :now WHERE session_id = :sid", params)
            if not n:
                self._run(f"INSERT INTO {self.table} (session_id, messages, display, turns, "
                          f"created_at, updated_at) VALUES (:sid, :messages, :display, :turns, "
                          f":now, :now)", params)
        except Exception as e:
            return False, f"couldn't save the conversation ({type(e).__name__})"
        return True, None

    def recent(self, n: int = 3, now: datetime | None = None) -> list[Recent] | None:
        """The n conversations answered most recently (at most RECENT_MAX), newest first,
        within the retention; None if the store can't be read. A row that couldn't be
        loaded (corrupt or tampered) isn't listed. Never raises."""
        n = max(0, min(int(n), RECENT_MAX))
        if not n:
            return []
        cutoff = (now or _now()) - timedelta(days=self.days)
        try:
            rows, _ = self._run(f"SELECT session_id, messages, turns, updated_at "
                                f"FROM {self.table} WHERE updated_at >= :cutoff "
                                f"ORDER BY updated_at DESC, session_id LIMIT {n}",
                                {"cutoff": cutoff})
        except Exception:
            return None
        out = []
        for r in rows:
            try:
                messages = json.loads(r["messages"])
            except (ValueError, TypeError, KeyError):
                continue
            if not (valid_id(r.get("session_id")) and messages_ok(messages)):
                continue
            out.append(Recent(r["session_id"], title_of(messages), int(r.get("turns") or 0),
                              r.get("updated_at")))
        return out

    def purge(self, now: datetime | None = None) -> int | None:
        """Delete conversations not touched for `days` days. How many, or None on error."""
        cutoff = (now or _now()) - timedelta(days=self.days)
        try:
            _, n = self._run(f"DELETE FROM {self.table} WHERE updated_at < :cutoff",
                             {"cutoff": cutoff})
            return n
        except Exception:
            return None


def mysql_run(engine) -> Run:
    from sqlalchemy import text

    def run(sql, params):
        with engine.begin() as conn:
            r = conn.execute(text(sql), params)
            rows = [dict(x._mapping) for x in r] if r.returns_rows else []
            return rows, r.rowcount
    return run


def sqlite_run(conn) -> Run:
    """For the tests: Python's sqlite3, same :name parameters."""
    def run(sql, params):
        params = {k: v.isoformat(" ") if isinstance(v, datetime) else v
                  for k, v in params.items()}         # sqlite3's datetime adapter is deprecated
        cur = conn.execute(sql, params)
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()] \
            if cur.description else []
        conn.commit()
        return rows, cur.rowcount
    return run


def session_settings() -> tuple[str, str | None, int]:
    from db_config import getenv

    try:
        days = int(getenv("AGENT_SESSION_DAYS", str(DEFAULT_DAYS)))
    except ValueError:
        days = DEFAULT_DAYS
    return (getenv("AGENT_SESSION_DB_USERNAME", DEFAULT_USER),
            getenv("AGENT_SESSION_DB_PASSWORD"), max(days, 1))


def store_from_env() -> tuple[SessionStore | None, str | None]:
    """(store, None), or (None, why conversations won't be saved). Never raises."""
    from db_config import DbConfig, getenv

    user, pw, days = session_settings()
    if not pw:
        return None, "AGENT_SESSION_DB_PASSWORD isn't set"
    if not getenv("DB_HOST") or not getenv("DB_NAME"):
        return None, "DB_HOST and DB_NAME must be set"
    try:
        cfg = DbConfig(user=user, password=pw, host=getenv("DB_HOST"),
                       port=int(getenv("DB_PORT", "3306")), name=getenv("DB_NAME"))
        return SessionStore(mysql_run(cfg.engine()), days), None
    except Exception as e:
        return None, f"couldn't set up the session store ({type(e).__name__})"


# ================================================================== setup ===

DENIED_CODES = {1044, 1142, 1143, 1227, 3879}
MISSING_CODES = {1146}
_USER = re.compile(r"[A-Za-z0-9_]{1,32}")
_HOST = re.compile(r"[A-Za-z0-9._%-]{1,255}")
_DB = re.compile(r"[A-Za-z0-9_$]{1,64}")


def account(user: str, host: str) -> str:
    if not _USER.fullmatch(user):
        raise ValueError(f"Invalid user name: {user!r}")
    if not _HOST.fullmatch(host):
        raise ValueError(f"Invalid host: {host!r}")
    return f"'{user}'@'{host}'"


def setup_statements(db: str, user: str, host: str) -> list[str]:
    """The table, then the user with exactly these grants. :pw is bound, never inlined.
    Drop and recreate the user (as agent_user.py does): a clean slate that needs no rights
    over every database, which hosted MySQL (Aiven) doesn't give even its admin."""
    if not _DB.fullmatch(db):
        raise ValueError(f"Invalid database name: {db!r}")
    acct = account(user, host)
    return [MYSQL_DDL,
            f"DROP USER IF EXISTS {acct}",
            f"CREATE USER {acct} IDENTIFIED BY :pw",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{db}`.`{TABLE}` TO {acct}"]


PROBE_ID = "0" * 32
ALLOWED_PROBES = [                 # (what, SQL, params): must work, and leave nothing behind
    ("save a conversation", f"INSERT INTO {TABLE} (session_id, messages, display, turns, "
                            f"created_at, updated_at) VALUES (:sid, '[]', '[]', 0, :now, :now)"),
    ("read it back", f"SELECT session_id FROM {TABLE} WHERE session_id = :sid"),
    ("update it", f"UPDATE {TABLE} SET turns = 1 WHERE session_id = :sid"),
    ("delete it", f"DELETE FROM {TABLE} WHERE session_id = :sid"),
]
FORBIDDEN_PROBES = [               # (what, SQL): must be refused; harmless if they ran
    ("the shop data", "SELECT machine_id FROM machine_overview LIMIT 0"),
    ("employee names", "SELECT emp_name FROM cmms_employees LIMIT 1"),
    ("writing shop data", "UPDATE cmms_machines SET machine_name = machine_name WHERE 1 = 0"),
    ("changing the schema", "CREATE TABLE agent_sessions_probe (x INT)"),
]


def error_code(e) -> int | None:
    args = getattr(getattr(e, "orig", None), "args", None) or getattr(e, "args", None)
    return args[0] if args and isinstance(args[0], int) else None


def check(run: Run, out=print) -> bool:
    """As the session user: its own table must work, everything else must be refused."""
    ok = True
    params = {"sid": PROBE_ID, "now": _now()}
    out("Allowed (must work):")
    for what, sql in ALLOWED_PROBES:
        try:
            run(sql, params)
            out(f"  ok      {what}")
        except Exception as e:
            ok = False
            out(f"  FAIL    {what}: {type(e).__name__} (MySQL error {error_code(e)})")
    out("Blocked (must fail):")
    for what, sql in FORBIDDEN_PROBES:
        try:
            run(sql, {})
            ok = False
            out(f"  FAIL    {what}: the session user CAN do this")
            if sql.startswith("CREATE TABLE"):
                run("DROP TABLE agent_sessions_probe", {})
        except Exception as e:
            code = error_code(e)
            if code in DENIED_CODES:
                out(f"  ok      {what}: denied")
            elif code in MISSING_CODES:
                out(f"  ok      {what}: table not created yet")
            else:
                ok = False
                out(f"  FAIL    {what}: unexpected error (MySQL error {code})")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "check", "purge"])
    ap.add_argument("--host", default="%", help="host the app connects from (default: any)")
    ap.add_argument("--dry-run", action="store_true", help="setup: show the SQL only")
    ap.add_argument("--env-file", default=None)
    a = ap.parse_args(argv)

    from dotenv import load_dotenv

    from db_config import DbConfig, getenv

    load_dotenv(a.env_file) if a.env_file else load_dotenv()
    user, pw, days = session_settings()
    db = getenv("DB_NAME") or "DB_NAME"
    if a.command == "setup" and a.dry_run:
        print(f"Session user {account(user, a.host)} on database `{db}`, retention {days} days\n")
        for sql in setup_statements(db if _DB.fullmatch(db) else "DB_NAME", user, a.host):
            print(sql.replace(":pw", "****") + ";\n")
        print("Then checks it can't: " + ", ".join(w for w, _ in FORBIDDEN_PROBES))
        print("Dry run - nothing written to the database.")
        return 0
    if not pw:
        print("Set AGENT_SESSION_DB_PASSWORD in .env (the session user's password).", file=sys.stderr)
        return 2
    host, port = getenv("DB_HOST"), int(getenv("DB_PORT", "3306"))
    if a.command == "setup":
        admin_pw = getenv("DB_ADMIN_PASSWORD", getenv("MYSQL_ROOT_PASSWORD"))
        if admin_pw is None:
            print("Set DB_ADMIN_PASSWORD (or MYSQL_ROOT_PASSWORD): setup needs an admin account.",
                  file=sys.stderr)
            return 2
        admin = DbConfig(user=getenv("DB_ADMIN_USERNAME", "root"), password=admin_pw,
                         host=host, port=port, name=db)
        print(f"Setting up as {admin.describe()}")
        run = mysql_run(admin.engine())
        for sql in setup_statements(db, user, a.host):
            try:
                run(sql, {"pw": pw} if ":pw" in sql else {})
            except Exception as e:             # never echoes the password: it's bound
                print(f"Failed at: {sql.split(' IDENTIFIED BY')[0].splitlines()[0]} "
                      f"(MySQL error {error_code(e)})", file=sys.stderr)
                return 1
        print(f"Created `{TABLE}` and the user {account(user, a.host)}.")
    me = DbConfig(user=user, password=pw, host=host, port=port, name=db)
    if a.command == "purge":
        n = SessionStore(mysql_run(me.engine()), days).purge()
        print(f"Deleted {n} conversation(s) older than {days} days." if n is not None
              else "Couldn't purge: the session store isn't reachable.")
        return 0 if n is not None else 1
    print(f"\nChecking as {me.describe()}")
    if check(mysql_run(me.engine())):
        print("\nPassed: the session user can keep conversations and do nothing else.")
        return 0
    print("\nFAILED: fix the grants above before the app saves conversations.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())