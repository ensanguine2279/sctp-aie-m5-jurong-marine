#!/usr/bin/env python3
"""An HTTP API over the agent: the same `agent_app.ask()` the Streamlit page and the eval
suite use, for a client that isn't the page (the Java backend, a script, a test harness).

It adds no agent logic. Everything the checklist enforces in code (grounding, citations,
the guardrail on both sides of the model, honest degradation, relative dates, the short-year
and "completed" rules) happens inside `ask()`, exactly as for the page. What this layer adds
is only what HTTP needs:

    - an API key on every route but /health (`X-API-Key`, compared in constant time). The
      app won't start without AGENT_API_KEY set: fail closed, like the guardrail's name list;
    - conversations by id (13 Nov 2026: conversations.py, Postgres): the agent's state is
      kept by LangGraph's checkpointer, one thread per conversation, saved when a turn
      finishes; a threads table keeps each one's title, question count and the display copy
      the page draws (agent_ui.display_copy, so a refused question is only its placeholder),
      and gives the recent list and the 7-day retention. The page still uses sessions.py
      (MySQL) until it calls this API (phase 3), so the two don't share conversations yet;
    - one question at a time per conversation in this process (a lock per session id), so
      two requests can't both append to the same conversation and lose one on save;
    - errors as JSON with a code, never a stack trace;
    - /ask/stream (11 Nov 2026): the same question as /ask, as server-sent events, one per
      step of the turn as it happens (the graph's guard_in, model, tools, guard_out), then
      the answer itself. Steps carry no question or answer text, only what kind of step it
      was and which tools ran.

The agent is the LangGraph graph in agent_graph.py (the only engine since 12 Nov 2026).

Design (11 Nov 2026): the routes are a thin shell over `AgentService`, whose methods take and
return plain Python (a `Reply`: status code and JSON body). So the tests need neither
FastAPI nor a server, the same way the page keeps its logic in plain functions; the FastAPI
tests in tests/test_agent_api.py run only where FastAPI is installed.

Routes
    GET  /health                  no key. Is the guard loaded, are conversations saved, model
    POST /ask                     {"question": "...", "session_id": optional}
    POST /ask/stream              the same body; text/event-stream: `step` events, then one
                                  `answer` (the /ask body) or `error` ({"status", "error"})
    GET  /sessions?limit=8        the newest saved conversations
    GET  /sessions/{session_id}   one conversation, as the page would draw it

Usage (personal repo, uv)
    uv add fastapi uvicorn langgraph langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool
    uv run uvicorn agent_api:create_app --factory --app-dir scripts --port 8000

Team repo (pip)
    pip install -r requirements-api.txt
    uvicorn agent_api:create_app --factory --app-dir scripts --port 8000

Needs the page's .env (CMMS_DB_*, OPENAI_API_KEY, OPENAI_BASE_URL) plus AGENT_API_KEY, and
for saved conversations CONV_DB_URL (docs/conversations.md). Without CONV_DB_URL it still
answers, with conversations off.
"""

import hmac
import json
import logging
import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_app as app  # noqa: E402
import agent_graph  # noqa: E402
import agent_ui as ui  # noqa: E402
import conversations as conv  # noqa: E402

API_TITLE = "JME maintenance assistant API"
API_VERSION = "0.1.0"
KEY_HEADER = "X-API-Key"
MAX_QUESTION = 2000           # characters; a question longer than this is refused, unasked
STREAM_TIMEOUT = 300          # seconds /ask/stream waits for the next step before giving up
RECENT_DEFAULT = ui.RECENT    # the page's sidebar shows the same number

log = logging.getLogger("agent_api")
log.addHandler(logging.NullHandler())


@dataclass
class Reply:
    """What a route sends back: an HTTP status and a JSON body."""
    status: int
    body: dict = field(default_factory=dict)


def error(status: int, code: str, message: str) -> Reply:
    return Reply(status, {"error": {"code": code, "message": message}})


def _emit(event: str, **fields) -> None:
    """One JSON line per API event, like agent_app's (#7). Never a question's text, and only
    the start of a session id: the full id opens the conversation, so it stays out of logs."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
           "event": event, **fields}
    log.info(json.dumps(rec, default=str))


def _short(sid: str | None) -> str | None:
    return sid[:8] if sid else None


def _drop_logger(lg: logging.Logger) -> None:
    """Forget a per-question logger from ui.trace_logger. The page makes one per question
    and keeps it; a long-running API would keep every one, so it is removed once read."""
    for h in list(lg.handlers):
        if isinstance(h, ui.TraceHandler):     # the shared file and stderr handlers stay open
            lg.removeHandler(h)
    lg.handlers.clear()
    logging.Logger.manager.loggerDict.pop(lg.name, None)


def outcome(events: list[dict]) -> tuple[str | None, str | None]:
    """(refused, replaced) from one question's events: the guardrail's rule when the
    question was refused in code (or "not_ready" when its name list is missing), and its
    rule when the answer was replaced before it was shown. None for each otherwise."""
    refused = replaced = None
    for e in events:
        kind = e.get("event")
        if kind == "input_refused" and refused is None:
            refused = e.get("rule")
        elif kind == "guard_not_ready":
            refused = "not_ready"
        elif kind == "output_blocked":
            replaced = e.get("rule")
    return refused, replaced


def answer_entry(answer: str, sources: list[str], notes: list[str], usage: dict) -> dict:
    """The answer as the page keeps it in `shown` (agent_ui.main), so the saved display copy
    is the same whichever front end asked the question."""
    return {"role": "assistant", "text": answer, "sources": sources, "notes": notes,
            "cost": ui.cost_line(usage), "cost_usd": float(usage.get("cost_usd") or 0.0),
            "cost_complete": bool(usage.get("cost_complete", True))}


def _usage_out(usage: dict) -> dict:
    cost = usage.get("cost_usd")
    return {"model_calls": int(usage.get("model_calls") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cost_usd": round(float(cost), 6) if cost is not None else None,
            "cost_complete": bool(usage.get("cost_complete", True))}


def _when(v) -> str | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
    return str(v) if v is not None else None


class AgentService:
    """The API's behaviour, in plain Python. Each method returns a Reply and never raises.

    convs: the conversations (conversations.Conversations: the checkpointed graph and the
    threads table), or None to answer without saving (convs_why says why). ask_fn, for the
    tests: (thread id or None, question, AgentContext, on_step) -> the graph's final state."""

    def __init__(self, db, client, convs: conv.Conversations | None = None,
                 convs_why: str | None = None, *, api_key: str, model: str = app.MODEL,
                 ask_fn: Callable | None = None, log_to_file: bool = True):
        if not api_key:
            raise ValueError("an API key is required (AGENT_API_KEY)")
        self.db, self.client, self.model = db, client, model
        self.convs, self.convs_why = convs, convs_why
        self._key = api_key.encode()
        self._ask = ask_fn or self._run
        self._log_to_file = log_to_file
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _run(self, sid, question, context, on_step=None) -> dict:
        """One question through the agent graph: on the conversation's thread (the
        checkpointer has its state), or, without saved conversations, on a new one."""
        if sid and self.convs:
            return self.convs.ask(sid, question, context, on_step=on_step)
        return agent_graph.run(app.new_conversation(), question, context, on_step=on_step)

    # ---------------------------------------------------------------- access ---
    def authorised(self, given: str | None) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self._key)

    def _lock(self, sid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.Lock())

    def _no_store(self) -> Reply:
        return error(503, "sessions_off",
                     f"Conversations aren't being saved here ({self.convs_why or 'no store'}).")

    def _load(self, sid) -> tuple[Reply | None, list | None]:
        """(an error reply, None) or (None, the display copy) for a saved conversation."""
        if not conv.valid_id(sid):
            return error(422, "bad_session_id", "session_id must be 32 hex characters."), None
        if self.convs is None:
            return self._no_store(), None
        t = self.convs.threads.get(sid)
        if not t.ok:
            return error(503, "store_down", t.error or "saved conversations can't be read"), None
        if not t.found:
            why = t.error or "no conversation with that id (it may be older than the retention)"
            return error(404, "unknown_session", why), None
        drawable = [m for m in t.display if isinstance(m, dict) and m.get("role") in
                    ("user", "assistant") and isinstance(m.get("text"), str)]
        return None, ui.display_copy(drawable)        # a refused question: its placeholder

    # ---------------------------------------------------------------- routes ---
    def health(self) -> Reply:
        guard = app.guardrail.names_loaded() > 0
        body = {"status": "ok" if guard else "guard_not_ready", "guard_ready": guard,
                "sessions": "on" if self.convs else f"off ({self.convs_why or 'no store'})",
                "store": "postgres" if self.convs else None,
                "model": self.model, "agent": "langgraph", "version": API_VERSION}
        return Reply(200 if guard else 503, body)

    def ask(self, question, session_id: str | None = None, on_step=None) -> Reply:
        if not isinstance(question, str) or not question.strip():
            return error(422, "bad_question", "question must be non-empty text.")
        question = question.strip()
        if len(question) > MAX_QUESTION:
            return error(422, "question_too_long",
                         f"question is longer than {MAX_QUESTION} characters.")
        try:
            if session_id is None:
                if self.convs is None:
                    return self._ask_in(None, question, [], on_step)
                sid = conv.new_id()
                # The row first: a thread's checkpoints are always covered by retention,
                # even if this turn fails before its answer is saved.
                ok, why = self.convs.threads.touch(sid, conv.NO_TITLE, 0, [])
                if not ok:
                    return self._ask_in(None, question, [], on_step, save_error=why)
                with self._lock(sid):
                    return self._ask_in(sid, question, [], on_step)
            if not conv.valid_id(session_id):
                return error(422, "bad_session_id", "session_id must be 32 hex characters.")
            with self._lock(session_id):        # read, asked and saved as one step
                err, shown = self._load(session_id)
                if err:
                    return err
                return self._ask_in(session_id, question, shown, on_step)
        except Exception as e:                   # a bug, not a normal failure: no trace out
            _emit("api_error", route="ask", session=_short(session_id), error=type(e).__name__)
            return error(500, "internal", "Something went wrong answering that. "
                                          "It has been logged.")

    def _ask_in(self, sid, question, shown, on_step=None, save_error=None) -> Reply:
        lg, handler = ui.trace_logger(log_to_file=self._log_to_file)
        ctx = agent_graph.AgentContext(db=self.db, client=self.client, model=self.model,
                                       logger=lg)
        try:
            final = self._ask(sid, question, ctx, on_step)
        finally:
            events = list(handler.events)
            _drop_logger(lg)
        answer = final["answer"]
        raw = list(final.get("sources") or [])
        sources = app.format_sources(raw)
        notes = app.format_notes(final.get("notes") or [])
        usage = dict(final.get("usage") or {})
        refused, replaced = outcome(events)
        entry = answer_entry(answer, sources, notes, usage)
        shown = list(shown) + [{"role": "user", "text": question}, entry]
        saved = False
        if sid and self.convs:
            msgs = final.get("messages") or []
            saved, save_error = self.convs.threads.touch(
                sid, conv.title_of(msgs), conv.turns_of(msgs), ui.display_copy(shown))
        _emit("api_ask", session=_short(sid), refused=refused, replaced=replaced,
              saved=saved, model_calls=usage.get("model_calls", 0))
        body = {"session_id": sid if saved else None, "answer": answer, "sources": sources,
                "notes": notes, "refused": refused, "answer_replaced": replaced,
                "usage": _usage_out(usage), "cost_line": entry["cost"],
                "trace": ui.trace_rows(events),
                "turns": sum(1 for m in shown if m.get("role") == "user"),
                "saved": saved, "save_error": save_error}
        if (sid or self.convs) and not saved:
            body["save_error"] = save_error or "not saved"
        return Reply(200, body)

    def ask_stream(self, question, session_id: str | None = None, *,
                   timeout: float = STREAM_TIMEOUT):
        """The same question as ask(), as events: {"event": "step", "data": {...}} for each
        step as it happens, then {"event": "answer", "data": <ask()'s body>} or
        {"event": "error", "data": {"status": ..., **error body}}. The question runs in its
        own thread, so it finishes and is saved even if the client stops reading."""
        events: queue.Queue = queue.Queue()

        def work():
            try:
                r = self.ask(question, session_id, on_step=lambda s: events.put(("step", s)))
            except Exception as e:               # ask() doesn't raise; this is belt and braces
                _emit("api_error", route="ask_stream", error=type(e).__name__)
                r = error(500, "internal", "Something went wrong answering that.")
            events.put(("done", r))

        threading.Thread(target=work, name="ask_stream", daemon=True).start()
        while True:
            try:
                kind, item = events.get(timeout=timeout)
            except queue.Empty:
                yield {"event": "error", "data": {"status": 504, **error(
                    504, "timeout", f"No progress for {timeout:.0f} s; the question may still "
                                    f"finish and be saved.").body}}
                return
            if kind == "step":
                yield {"event": "step", "data": item}
                continue
            if item.status == 200:
                yield {"event": "answer", "data": item.body}
            else:
                yield {"event": "error", "data": {"status": item.status, **item.body}}
            return

    def session(self, session_id) -> Reply:
        try:
            err, shown = self._load(session_id)
            if err:
                return err
            cost, questions, complete = ui.conversation_spend(shown)
            return Reply(200, {"session_id": session_id, "turns": questions, "messages": shown,
                               "spend": {"cost_usd": round(cost, 6), "complete": complete,
                                         "line": ui.conversation_line(cost, questions, complete)}})
        except Exception as e:
            _emit("api_error", route="session", error=type(e).__name__)
            return error(500, "internal", "Something went wrong reading that conversation.")

    def recent(self, limit=RECENT_DEFAULT) -> Reply:
        if self.convs is None:
            return self._no_store()
        try:
            n = max(1, min(int(limit), conv.RECENT_MAX))
        except (TypeError, ValueError):
            return error(422, "bad_limit", f"limit must be a whole number, 1 to {conv.RECENT_MAX}.")
        rows = self.convs.threads.recent(n)
        if rows is None:
            return error(503, "store_down", "saved conversations can't be listed right now.")
        return Reply(200, {"sessions": [{"session_id": r.sid, "title": r.title, "turns": r.turns,
                                         "updated_at": _when(r.updated_at)} for r in rows]})


# ======================================================================= wiring ===

def service_from_env(env_file: str | None = None) -> AgentService:
    """The service with everything the page uses, from .env. Stops with a readable message
    when the guard's name list or AGENT_API_KEY is missing (fail closed)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file) if env_file else load_dotenv()
    except ImportError:
        pass
    try:
        app.guardrail.require_names()
    except app.guardrail.GuardNotReady as e:
        raise SystemExit(str(e)) from None
    key = os.environ.get("AGENT_API_KEY", "")
    if len(key) < 16:
        raise SystemExit("Set AGENT_API_KEY (16 characters or more) in .env: the API won't "
                         "start without one. For example: python -c \"import secrets; "
                         "print(secrets.token_urlsafe(32))\"")
    try:
        db = app.at.connect(env_file)
    except Exception as e:
        raise SystemExit(f"Couldn't connect to the database as the agent user: {e}") from None
    client = app.build_client()
    convs, why = conv.from_env()                 # never raises: off, with the reason
    if convs:
        convs.purge()                            # retention, once per process start
    log.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)
    return AgentService(db, client, convs, why, api_key=key)


def sse(evt: dict) -> str:
    """One server-sent event, as the browser's EventSource (and any SSE client) reads it."""
    return f"event: {evt['event']}\ndata: {json.dumps(evt['data'], default=str)}\n\n"


def create_app(service: AgentService | None = None):
    """The FastAPI app. `uvicorn agent_api:create_app --factory --app-dir scripts`; tests
    pass a service built from stand-ins.

    This module must NOT use `from __future__ import annotations`: FastAPI reads the routes'
    type hints to know `body` is the JSON body, and with postponed annotations it can't
    resolve AskIn, which is defined in here (11 Nov 2026: the draft had it, and /ask would
    have failed on a real server)."""
    from fastapi import FastAPI, Header
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel

    svc = service or service_from_env()
    api = FastAPI(title=API_TITLE, version=API_VERSION)

    class AskIn(BaseModel):
        question: str
        session_id: str | None = None

    def send(r: Reply) -> JSONResponse:
        return JSONResponse(r.body, status_code=r.status)

    def denied(key: str | None) -> JSONResponse | None:
        if svc.authorised(key):
            return None
        return send(error(401, "unauthorised", f"Send the API key in the {KEY_HEADER} header."))

    # Plain `def` routes: ask() blocks on the model for seconds, so FastAPI runs each one
    # in its thread pool and other requests carry on meanwhile.
    @api.get("/health")
    def health():
        return send(svc.health())

    @api.post("/ask")
    def ask(body: AskIn, x_api_key: str | None = Header(default=None)):
        return denied(x_api_key) or send(svc.ask(body.question, body.session_id))

    @api.post("/ask/stream")
    def ask_stream(body: AskIn, x_api_key: str | None = Header(default=None)):
        refused = denied(x_api_key)
        if refused:
            return refused
        return StreamingResponse((sse(e) for e in svc.ask_stream(body.question, body.session_id)),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.get("/sessions")
    def recent(limit: int = RECENT_DEFAULT, x_api_key: str | None = Header(default=None)):
        return denied(x_api_key) or send(svc.recent(limit))

    @api.get("/sessions/{session_id}")
    def one(session_id: str, x_api_key: str | None = Header(default=None)):
        return denied(x_api_key) or send(svc.session(session_id))

    api.state.service = svc
    return api