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
      the page draws (answer_view.display_copy, so a refused question is only its
      placeholder), and gives the recent list and the 7-day retention. Since phase 3
      (15 Nov 2026) the Streamlit page calls this API for everything (scripts/api_client.py),
      so there is one store of conversations, whichever front end asked;
    - one question at a time per conversation in this process (a lock per session id), so
      two requests can't both append to the same conversation and lose one on save;
    - errors as JSON with a code, never a stack trace;
    - /ask/stream (11 Nov 2026): the same question as /ask, as server-sent events, one per
      step of the turn as it happens (the graph's guard_in, model, tools, guard_out), then
      the answer itself. Steps carry no question or answer text, only what kind of step it
      was and which tools ran. The first event is `session` (its id), as soon as the
      conversation is known, so a client that loses the stream can still reopen it;
    - the photos an answer cites (GET /photos/{sha256}) and the key's spend (GET /usage), so
      a front end needs no database login and no LiteLLM key of its own.

The agent is the LangGraph graph in agent_graph.py (the only engine since 12 Nov 2026).

Design (11 Nov 2026): the routes are a thin shell over `AgentService`, whose methods take and
return plain Python (a `Reply`: status code and JSON body). So the tests need neither
FastAPI nor a server, the same way the page keeps its logic in plain functions; the FastAPI
tests in tests/test_agent_api.py run only where FastAPI is installed.

Routes
    GET  /health                  no key. Is the guard loaded, are conversations saved, model
    POST /ask                     {"question": "...", "session_id": optional}
    POST /ask/stream              the same body; text/event-stream: `session` (once the
                                  conversation is known), `step` events, then one `answer`
                                  (the /ask body) or `error` ({"status", "error"})
    GET  /sessions?limit=8        the newest saved conversations
    GET  /sessions/{session_id}   one conversation, as the page would draw it
    DELETE /sessions/{session_id} delete it: its checkpoints and its row
    GET  /photos/{sha256}         a photo an answer cites (image bytes; redacted forms only)
    GET  /usage                   this deployment's LLM spend against its budget (spend.py)

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
import answer_view as av  # noqa: E402
import conversations as conv  # noqa: E402

API_TITLE = "JME maintenance assistant API"
API_VERSION = "0.1.0"
KEY_HEADER = "X-API-Key"
MAX_QUESTION = 2000           # characters; a question longer than this is refused, unasked
STREAM_TIMEOUT = 300          # seconds /ask/stream waits for the next step before giving up
RECENT_DEFAULT = av.RECENT    # the page's sidebar shows the same number

log = logging.getLogger("agent_api")
log.addHandler(logging.NullHandler())


@dataclass
class Reply:
    """What a route sends back: an HTTP status and a JSON body."""
    status: int
    body: dict = field(default_factory=dict)


@dataclass
class Photo:
    """GET /photos: the image itself, not JSON."""
    data: bytes
    mime: str
    variant: str | None = None
    source_file: str | None = None
    status: int = 200


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
    """Forget a per-question logger from answer_view.trace_logger. A long-running API would
    keep every one, so it is removed once read."""
    for h in list(lg.handlers):
        if isinstance(h, av.TraceHandler):     # the shared file and stderr handlers stay open
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


def spend_of(shown: list[dict]) -> dict:
    """A conversation's spend as the API reports it (GET /sessions/{id}, and every answer):
    added up here, from each answer's cost, so a front end never does the sum. For an answer
    that wasn't saved (no conversations), `shown` is that question alone."""
    cost, questions, complete = av.conversation_spend(shown)
    return {"cost_usd": round(cost, 6), "questions": questions, "complete": complete,
            "line": av.conversation_line(cost, questions, complete)}


def answer_entry(answer: str, sources: list[str], notes: list[str], usage: dict,
                 images: list[dict] | None = None) -> dict:
    """The answer as it is drawn and saved, whichever front end asked the question. images:
    the scans it cites (answer_view.image_refs), fetched by the page from GET /photos."""
    return {"role": "assistant", "text": answer, "sources": sources, "notes": notes,
            "images": list(images or []), "cost": av.cost_line(usage), "cost_usd": float(usage.get("cost_usd") or 0.0),
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
                 ask_fn: Callable | None = None, log_to_file: bool = True,
                 spend_fn: Callable | None = None, photo_fn: Callable | None = None):
        if not api_key:
            raise ValueError("an API key is required (AGENT_API_KEY)")
        self.db, self.client, self.model = db, client, model
        self.convs, self.convs_why = convs, convs_why
        self._key = api_key.encode()
        self._ask = ask_fn or self._run
        self._log_to_file = log_to_file
        self._spend = spend_fn          # () -> spend.KeySpend; None: spend.key_spend()
        self._photo = photo_fn or app.at.fetch_image
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
        return None, av.display_copy(drawable)        # a refused question: its placeholder

    # ---------------------------------------------------------------- routes ---
    def health(self) -> Reply:
        guard = app.guardrail.names_loaded() > 0
        body = {"status": "ok" if guard else "guard_not_ready", "guard_ready": guard,
                "sessions": "on" if self.convs else f"off ({self.convs_why or 'no store'})",
                "store": "postgres" if self.convs else None,
                "model": self.model, "agent": "langgraph", "version": API_VERSION}
        return Reply(200 if guard else 503, body)

    def ask(self, question, session_id: str | None = None, on_step=None,
            on_session=None) -> Reply:
        """One question. on_session(sid) is called as soon as the conversation is known (a
        new one's row is written, or a saved one is loaded), before the model is asked."""
        said = on_session or (lambda sid: None)
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
                said(sid)
                with self._lock(sid):
                    return self._ask_in(sid, question, [], on_step)
            if not conv.valid_id(session_id):
                return error(422, "bad_session_id", "session_id must be 32 hex characters.")
            with self._lock(session_id):        # read, asked and saved as one step
                err, shown = self._load(session_id)
                if err:
                    return err
                said(session_id)
                return self._ask_in(session_id, question, shown, on_step)
        except Exception as e:                   # a bug, not a normal failure: no trace out
            _emit("api_error", route="ask", session=_short(session_id), error=type(e).__name__)
            return error(500, "internal", "Something went wrong answering that. "
                                          "It has been logged.")

    def _ask_in(self, sid, question, shown, on_step=None, save_error=None) -> Reply:
        lg, handler = av.trace_logger(log_to_file=self._log_to_file)
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
        images = av.image_refs(raw)
        refused, replaced = outcome(events)
        entry = answer_entry(answer, sources, notes, usage, images)
        shown = list(shown) + [{"role": "user", "text": question}, entry]
        saved = False
        if sid and self.convs:
            msgs = final.get("messages") or []
            saved, save_error = self.convs.threads.touch(
                sid, conv.title_of(msgs), conv.turns_of(msgs), av.display_copy(shown))
        _emit("api_ask", session=_short(sid), refused=refused, replaced=replaced,
              saved=saved, model_calls=usage.get("model_calls", 0))
        body = {"session_id": sid if saved else None, "answer": answer, "sources": sources,
                "notes": notes, "images": images, "refused": refused,
                "answer_replaced": replaced,
                "usage": _usage_out(usage), "cost_line": entry["cost"],
                "trace": av.trace_rows(events),
                "turns": sum(1 for m in shown if m.get("role") == "user"),
                "spend": spend_of(shown),
                "saved": saved, "save_error": save_error}
        if (sid or self.convs) and not saved:
            body["save_error"] = save_error or "not saved"
        return Reply(200, body)

    def ask_stream(self, question, session_id: str | None = None, *,
                   timeout: float = STREAM_TIMEOUT):
        """The same question as ask(), as events: {"event": "step", "data": {...}} for each
        step as it happens, then {"event": "answer", "data": <ask()'s body>} or
        {"event": "error", "data": {"status": ..., **error body}}. Before the steps,
        {"event": "session", "data": {"session_id": ...}} once the conversation is known (not
        sent when conversations are off). The question runs in its own thread, so it
        finishes and is saved even if the client stops reading."""
        events: queue.Queue = queue.Queue()

        def work():
            try:
                r = self.ask(question, session_id, on_step=lambda s: events.put(("step", s)),
                             on_session=lambda sid: events.put(("session", sid)))
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
            if kind == "session":
                yield {"event": "session", "data": {"session_id": item}}
                continue
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
            spent = spend_of(shown)
            return Reply(200, {"session_id": session_id, "turns": spent["questions"],
                               "messages": shown, "spend": spent})
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

    def delete_session(self, session_id) -> Reply:
        """Delete one conversation: its checkpoints and its row. One that isn't there (never
        was, or past the retention) counts as deleted, so deleting twice is fine."""
        if not conv.valid_id(session_id):
            return error(422, "bad_session_id", "session_id must be 32 hex characters.")
        if self.convs is None:
            return self._no_store()
        try:
            with self._lock(session_id):        # not while a question is being answered in it
                ok, why = self.convs.delete(session_id)
        except Exception as e:
            _emit("api_error", route="delete", error=type(e).__name__)
            return error(500, "internal", "Something went wrong deleting that conversation.")
        _emit("api_delete", session=_short(session_id), ok=ok)
        if not ok:
            return error(503, "store_down", why or "the conversation couldn't be deleted")
        with self._locks_guard:
            self._locks.pop(session_id, None)
        return Reply(200, {"session_id": session_id, "deleted": True})

    def photo(self, sha256) -> "Photo | Reply":
        """A photo an answer cites, by its SHA-256: from img_files_public, so an incident form
        is only ever the redacted copy, and only if its bytes still match the hash recorded at
        ingest (agent_tools.fetch_image). The page never connects to the database."""
        if not isinstance(sha256, str) or not av._SHA256.fullmatch(sha256):
            return error(422, "bad_sha256", "sha256 must be 64 lower-case hex characters.")
        try:
            got = self._photo(self.db, sha256)
        except Exception as e:
            _emit("api_error", route="photo", error=type(e).__name__)
            return error(503, "db_down", "The photo can't be read right now.")
        if not got:
            return error(404, "no_photo", "No stored photo with that SHA-256 (or its bytes no "
                                          "longer match the hash recorded at ingest).")
        return Photo(bytes(got["bytes"]), got.get("mime") or "application/octet-stream",
                     got.get("variant"), got.get("source_file"))

    def usage(self) -> Reply:
        """This deployment's spend against its budget, from the LiteLLM proxy with the API's
        own key (spend.py). Always 200: a figure that isn't available says why (ok false)."""
        try:
            import spend
            s = self._spend() if self._spend is not None else spend.key_spend()
            line = spend.budget_line(s)
        except Exception as e:
            _emit("api_error", route="usage", error=type(e).__name__)
            return Reply(200, {"ok": False, "error": "spend couldn't be read",
                               "line": "Spend not available.", "model": self.model})
        return Reply(200, {"ok": bool(s.ok), "spend": s.spend, "max_budget": s.max_budget,
                           "used": s.used, "warn": bool(s.warn),
                           "budget_reset_at": s.budget_reset_at, "error": s.error,
                           "line": line, "model": self.model})


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
    from fastapi.responses import JSONResponse, Response, StreamingResponse
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

    @api.delete("/sessions/{session_id}")
    def delete(session_id: str, x_api_key: str | None = Header(default=None)):
        return denied(x_api_key) or send(svc.delete_session(session_id))

    @api.get("/photos/{sha256}")
    def photo(sha256: str, x_api_key: str | None = Header(default=None)):
        refused = denied(x_api_key)
        if refused:
            return refused
        r = svc.photo(sha256)
        if isinstance(r, Reply):
            return send(r)
        headers = {"Cache-Control": "private, max-age=3600"}
        if r.variant:
            headers["X-Photo-Variant"] = r.variant
        if r.source_file and r.source_file.isascii():      # headers are latin-1 only
            headers["X-Source-File"] = r.source_file
        return Response(r.data, media_type=r.mime, headers=headers)

    @api.get("/usage")
    def usage(x_api_key: str | None = Header(default=None)):
        return denied(x_api_key) or send(svc.usage())

    api.state.service = svc
    return api