"""Tests for scripts/conversations.py: conversations in Postgres, LangGraph's checkpointer
for the agent's state and a threads table for the list, the display copy and retention
(13 Nov 2026). No Postgres here: the table runs on sqlite3 through the same run(sql, params)
shape the app uses with psycopg, the checkpointer is LangGraph's in-memory one, and setup and
check are tested on their SQL and on a stand-in that answers with Postgres' error codes. The
real thing: `python scripts/conversations.py setup`, then `check` (docs/conversations.md)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import agent_app as app
import agent_graph as graph
import conversations as conv
from test_agent_graph import capture, model_says, result

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    c = sqlite3.connect(":memory:")
    c.execute(conv.SQLITE_DDL)
    yield c
    c.close()


@pytest.fixture
def threads(db):
    return conv.ThreadStore(conv.sqlite_run(db))


def at(db, sid, when):
    db.execute(f"UPDATE {conv.TABLE} SET updated_at = ? WHERE thread_id = ?",
               (conv._stamp(when), sid))
    db.commit()


class Saver:
    """Records delete_thread calls (stands in for the checkpointer where only that matters)."""

    def __init__(self, fail=False):
        self.deleted, self.fail = [], fail

    def delete_thread(self, sid):
        if self.fail:
            raise ConnectionError("can't reach Postgres at db.internal")
        self.deleted.append(sid)


# --------------------------------------------------------------------- ids ---

def test_an_id_is_32_hex_and_random():
    a, b = conv.new_id(), conv.new_id()
    assert conv.valid_id(a) and conv.valid_id(b) and a != b


@pytest.mark.parametrize("bad", ["", "abc", "0" * 31, "g" * 32, "A" * 32, "0" * 33,
                                 "' OR 1=1 --" + "0" * 21, None, 123])
def test_anything_else_is_not_an_id(bad):
    assert not conv.valid_id(bad)


def test_a_title_is_the_first_question_shortened():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "how   many " + "incidents " * 20},
            {"role": "assistant", "content": "x"}, {"role": "user", "content": "later"}]
    t = conv.title_of(msgs)
    assert len(t) <= conv.TITLE_CHARS and t.endswith("…") and t.startswith("how many incidents")
    assert conv.title_of([]) == conv.NO_TITLE and conv.turns_of(msgs) == 2


# --------------------------------------------------------- the threads table ---

def test_a_row_is_made_updated_and_read_back(threads, db):
    sid = conv.new_id()
    assert threads.touch(sid, conv.NO_TITLE, 0, []) == (True, None)
    created = db.execute(f"SELECT created_at FROM {conv.TABLE}").fetchone()[0]
    display = [{"role": "user", "text": "q"}, {"role": "assistant", "text": "a", "cost_usd": 0.1}]
    assert threads.touch(sid, "q", 1, display)[0]
    t = threads.get(sid)
    assert (t.ok, t.found, t.title, t.turns, t.display) == (True, True, "q", 1, display)
    assert db.execute(f"SELECT created_at, COUNT(*) FROM {conv.TABLE}").fetchone() == (created, 1)


def test_an_unknown_or_bad_id_is_not_found_and_never_sent(threads):
    t = threads.get(conv.new_id())
    assert t.ok and not t.found
    seen = []
    s = conv.ThreadStore(lambda sql, p: seen.append(sql) or ([], 0))
    assert not s.get("'; DROP TABLE agent_threads; --").found
    assert s.touch("nope", "t", 0, []) == (False, "not a conversation id")
    assert s.delete("nope") == (False, "not a conversation id")
    assert seen == []


def test_a_row_that_cant_be_read_starts_afresh(threads, db):
    sid = conv.new_id()
    db.execute(f"INSERT INTO {conv.TABLE} VALUES (?, 't', 1, 'not json', 'x', 'x')", (sid,))
    t = threads.get(sid)
    assert t.ok and not t.found and "starting afresh" in t.error


def test_a_down_database_never_raises_and_names_only_the_error_type():
    def down(sql, params):
        raise ConnectionError("can't reach Postgres at db.internal:5432 as agent_threads")
    s = conv.ThreadStore(down)
    t = s.get(conv.new_id())
    assert not t.ok and t.error == "couldn't read the conversation (ConnectionError)"
    ok, err = s.touch(conv.new_id(), "t", 1, [])
    assert not ok and "db.internal" not in err
    assert s.recent(3) is None and s.purge() is None
    assert s.delete(conv.new_id())[0] is False


def test_recent_lists_the_newest_with_a_question_and_within_the_retention(threads, db):
    sids = [conv.new_id() for _ in range(5)]
    for i, sid in enumerate(sids):
        threads.touch(sid, f"question {i}", 1, [])
        at(db, sid, NOW - timedelta(hours=5 - i))
    at(db, sids[0], NOW - timedelta(days=8))                  # past the retention
    threads.touch(sids[4], conv.NO_TITLE, 0, [])               # no question yet
    at(db, sids[4], NOW)
    got = threads.recent(3, now=NOW)
    assert [r.title for r in got] == ["question 3", "question 2", "question 1"]
    assert threads.recent(0) == []


def test_recent_asks_for_at_most_ten():
    seen = []
    conv.ThreadStore(lambda sql, p: seen.append(sql) or ([], 0)).recent(500)
    assert seen[0].endswith("LIMIT 10")


def test_delete_removes_the_checkpoints_and_the_row(threads):
    keep, gone = conv.new_id(), conv.new_id()
    for sid in (keep, gone):
        threads.touch(sid, "q", 1, [])
    saver = Saver()
    assert threads.delete(gone, saver) == (True, None)
    assert saver.deleted == [gone] and not threads.get(gone).found and threads.get(keep).found
    assert threads.delete(conv.new_id(), saver)[0]             # not there: deleted either way
    ok, err = threads.delete(keep, Saver(fail=True))
    assert not ok and "db.internal" not in err and threads.get(keep).found   # row kept


def test_purge_deletes_only_conversations_past_the_retention(threads, db):
    old, new = conv.new_id(), conv.new_id()
    for sid in (old, new):
        threads.touch(sid, "q", 1, [])
    at(db, old, NOW - timedelta(days=8))
    at(db, new, NOW - timedelta(days=6))
    saver = Saver()
    assert threads.purge(saver, now=NOW) == 1
    assert saver.deleted == [old] and not threads.get(old).found and threads.get(new).found


def test_the_retention_is_at_least_a_day_and_read_from_the_settings(monkeypatch):
    assert conv.ThreadStore(lambda s, p: ([], 0), days=0).days == 1
    monkeypatch.setenv("AGENT_CONVERSATION_DAYS", "30")
    assert conv.days_setting() == 30
    monkeypatch.setenv("AGENT_CONVERSATION_DAYS", "thirty")
    assert conv.days_setting() == conv.DEFAULT_DAYS


def test_a_bad_table_name_is_refused():
    with pytest.raises(ValueError):
        conv.ThreadStore(lambda s, p: ([], 0), table="x; DROP TABLE y")


def test_named_parameters_become_psycopgs():
    assert conv._named("WHERE a = :sid AND b < :cutoff") == "WHERE a = %(sid)s AND b < %(cutoff)s"


# ------------------------------------------------- the checkpointed agent ---

@pytest.fixture
def convs(db):
    from langgraph.checkpoint.memory import InMemorySaver
    return conv.Conversations(conv.ThreadStore(conv.sqlite_run(db)), InMemorySaver())


def ask(convs, sid, question):
    lg, h = capture()
    final = convs.ask(sid, question, graph.AgentContext(logger=lg))
    return final, h.events


def test_a_thread_keeps_its_conversation_and_gets_the_system_prompt_from_the_code(
        convs, monkeypatch):
    seen = []

    def model(client, m, messages, tools):
        seen.append([dict(x) for x in messages])
        return model_says("It is running.")(client, m, messages, tools)
    monkeypatch.setattr(app, "complete", model)
    sid = conv.new_id()
    ask(convs, sid, "is the lathe running?")
    ask(convs, sid, "and the mill?")
    assert [m["content"] for m in seen[1][1:]] == ["is the lathe running?", "It is running.",
                                                   "and the mill?"]
    assert seen[1][0] == {"role": "system", "content": app.SYSTEM_PROMPT}
    assert conv.turns_of(convs.messages(sid)) == 2
    assert convs.messages(conv.new_id()) == [] and convs.messages("bad") == []


def test_an_old_system_prompt_in_a_thread_is_replaced(convs, monkeypatch):
    seen = []

    def model(client, m, messages, tools):
        seen.append(messages[0]["content"])
        return model_says("Fine.")(client, m, messages, tools)
    monkeypatch.setattr(app, "complete", model)
    sid = conv.new_id()
    lg, _ = capture()
    graph.run([{"role": "system", "content": "an old prompt"}], "hello",
              graph.AgentContext(logger=lg), compiled=convs.graph, config=convs.config(sid))
    assert seen == [app.SYSTEM_PROMPT]


def test_threads_are_kept_apart(convs, monkeypatch):
    monkeypatch.setattr(app, "complete", model_says("Fine."))
    a, b = conv.new_id(), conv.new_id()
    ask(convs, a, "about the lathe")
    ask(convs, b, "about the mill")
    assert "about the mill" not in json.dumps(convs.messages(a))


def test_no_checkpoint_ever_holds_a_blocked_answer_or_a_refused_question(convs, monkeypatch):
    monkeypatch.setattr(app, "complete", model_says("Ring the storeman on 9123 4567."))
    sid = conv.new_id()
    final, events = ask(convs, sid, "who do I ask about bearings?")
    assert "output_blocked" in [e["event"] for e in events]
    ask(convs, sid, "What is the name of employee EMP015?")
    state = convs.graph.get_state(convs.config(sid)).values
    held = json.dumps(state, default=str)
    assert "9123" not in held and "EMP015" not in held


def test_a_turn_cut_off_half_way_leaves_the_thread_working(convs, monkeypatch):
    """Whatever the checkpointer kept of a turn that raised, the next one works: guard_in
    mends the history, and the model never gets a tool call with no result."""
    monkeypatch.setattr(app, "complete",
                        model_says([("c1", "list_machines", "{}")], KeyboardInterrupt()))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: result())
    sid = conv.new_id()
    with pytest.raises(KeyboardInterrupt):
        ask(convs, sid, "how many machines?")
    sent = []

    def model(client, m, messages, tools):
        sent.append([dict(x) for x in messages])
        return model_says("Ready.")(client, m, messages, tools)
    monkeypatch.setattr(app, "complete", model)
    final, _ = ask(convs, sid, "hello again")
    assert final["answer"] == "Ready."
    calls = {c["id"] for m in sent[0] for c in m.get("tool_calls") or []}
    answered = {m["tool_call_id"] for m in sent[0] if m.get("role") == "tool"}
    assert calls == answered                                   # no call without a result


def test_delete_and_purge_reach_the_checkpointer(convs, db, monkeypatch):
    monkeypatch.setattr(app, "complete", model_says("Fine."))
    old, gone = conv.new_id(), conv.new_id()
    for sid in (old, gone):
        convs.threads.touch(sid, "q", 1, [])
        ask(convs, sid, "hello")
    assert convs.delete(gone) == (True, None) and convs.messages(gone) == []
    at(db, old, NOW - timedelta(days=8))
    assert convs.purge(now=NOW) == 1 and convs.messages(old) == []


def test_a_bad_id_never_reaches_the_graph(convs):
    with pytest.raises(ValueError):
        convs.ask("../x", "hello", graph.AgentContext())


# ---------------------------------------------------------------- settings ---

def test_a_postgres_url_is_read_and_shown_without_its_password():
    u = conv.parse_url("postgresql://agent_threads:p%40ss@db.example.com:16543/defaultdb"
                       "?sslmode=require")
    assert (u.user, u.password, u.host, u.port, u.dbname) == (
        "agent_threads", "p@ss", "db.example.com", 16543, "defaultdb")
    assert "p@ss" not in u.hidden() and "16543/defaultdb" in u.hidden()
    for bad in (None, "", "mysql://u:p@h/db", "postgresql://h/db", "not a url"):
        assert conv.parse_url(bad) is None, bad


def test_without_a_url_conversations_are_off_and_say_why(monkeypatch):
    assert conv.from_env() == (None, "CONV_DB_URL isn't set")      # conftest: no settings


def test_the_old_url_name_alone_is_not_enough(monkeypatch, capsys):
    """Until 14 Nov 2026: AGENT_PG_URL. Only CONV_DB_URL is read now."""
    monkeypatch.setenv("AGENT_PG_URL", "postgresql://agent_threads:s3cret@localhost:5433/jme_agent")
    assert conv.from_env() == (None, "CONV_DB_URL isn't set")
    assert conv.main(["check", "--env-file", "/dev/null"]) == 2
    err = capsys.readouterr().err
    assert "CONV_DB_URL" in err and "s3cret" not in err


# ------------------------------------------------------------------- setup ---

def test_setup_makes_one_schema_one_user_and_grants_only_inside_it():
    stmts = conv.setup_statements("jme_agent", "agent_threads", "pw")
    text = "\n".join(stmts)
    assert stmts[0] == "CREATE SCHEMA IF NOT EXISTS agent"
    assert "CREATE ROLE agent_threads LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;" in text
    assert "ALTER ROLE agent_threads WITH LOGIN PASSWORD 'pw'" in text
    assert "ALTER ROLE agent_threads SET search_path = agent" in text
    grants = [s for s in stmts if s.startswith("GRANT")]
    assert all(" agent " in s or "SCHEMA agent" in s or "DATABASE jme_agent" in s for s in grants)
    assert not any("CREATE ON" in s or "ALL PRIVILEGES" in s or "public" in s.lower()
                   for s in grants)
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS agent.agent_threads") for s in stmts)
    assert any(s.startswith("-- PostgresSaver.setup()") for s in stmts)   # before the grants
    assert [i for i, s in enumerate(stmts) if s.startswith("-- Postgres")][0] < \
        [i for i, s in enumerate(stmts) if s.startswith("GRANT")][0]


def test_setup_works_for_an_admin_that_isnt_a_superuser():
    """Aiven's avnadmin isn't a superuser, and Postgres 16 refuses it any ALTER ROLE that names
    SUPERUSER, even NOSUPERUSER (15 Nov 2026). The attributes go on CREATE ROLE only."""
    for s in conv.setup_statements("defaultdb", "agent_threads", "pw"):
        if s.startswith("ALTER ROLE"):
            assert "SUPERUSER" not in s and "CREATEDB" not in s and "CREATEROLE" not in s, s


def test_a_password_with_a_quote_is_a_safe_literal_and_hidden_in_the_dry_run():
    text = "\n".join(conv.setup_statements("jme_agent", "agent_threads", "it's"))
    assert "PASSWORD 'it''s'" in text
    shown = "\n".join(conv.setup_statements("jme_agent", "agent_threads", "s3cret", hide=True))
    assert "s3cret" not in shown and "PASSWORD ****" in shown


@pytest.mark.parametrize("dbname,user", [("jme; DROP", "u"), ("jme", "u'; --"), ("jme", ""),
                                         ("Jme", "u"), ("jme", "1user")])
def test_setup_refuses_odd_names(dbname, user):
    with pytest.raises(ValueError):
        conv.setup_statements(dbname, user, "pw")


def test_the_dry_run_prints_the_sql_without_the_password(monkeypatch, capsys):
    monkeypatch.setenv("CONV_DB_URL", "postgresql://agent_threads:s3cret@localhost:5433/jme_agent")
    assert conv.main(["setup", "--dry-run", "--env-file", "/dev/null"]) == 0
    out = capsys.readouterr().out
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent" in out
    assert "s3cret" not in out and "PASSWORD ****" in out


def test_the_cli_says_what_to_set(monkeypatch, capsys):
    assert conv.main(["check", "--env-file", "/dev/null"]) == 2
    assert "CONV_DB_URL" in capsys.readouterr().err


# ------------------------------------------------------------------- check ---

class PgError(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def server(allow_all=False, code="42501"):
    """A stand-in for Postgres as the agent user: its own tables work, the rest is refused."""
    calls = []

    def run(sql, params):
        calls.append(sql)
        if allow_all or not any(w in sql for w in ("CREATE", "DROP")):
            return [], 1
        raise PgError(code)
    run.calls = calls
    return run


def test_check_passes_when_only_its_own_tables_work():
    lines = []
    assert conv.check(server(), out=lines.append)
    text = "\n".join(lines)
    assert text.count("ok ") == len(conv.ALLOWED_PROBES) + len(conv.FORBIDDEN_PROBES)
    assert "FAIL" not in text


def test_check_fails_if_the_agent_user_can_change_the_schema_and_cleans_up():
    lines = []
    run = server(allow_all=True)
    assert not conv.check(run, out=lines.append)
    assert "making a table: the agent user CAN do this" in "\n".join(lines)
    assert "DROP TABLE agent.agent_threads_probe" in run.calls


def test_check_counts_an_odd_error_as_a_failure():
    assert not conv.check(server(code="08006"), out=lambda s: None)     # connection failure

    def refused_everything(sql, params):
        raise PgError("42501")
    lines = []
    assert not conv.check(refused_everything, out=lines.append)
    assert "FAIL    save a conversation" in "\n".join(lines)