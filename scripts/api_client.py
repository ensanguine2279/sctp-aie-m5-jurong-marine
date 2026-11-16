#!/usr/bin/env python3
"""The page's way to the agent: a small client for agent_api.py (phase 3, 15 Nov 2026).

The Streamlit page holds no database login, no LiteLLM key and no agent code. It knows two
settings, AGENT_API_URL and AGENT_API_KEY, and everything it shows comes from the API:

    ask_stream(question, sid)  POST /ask/stream: yields {"event", "data"} as they arrive
                               (session, step..., then answer or error)
    ask(question, sid)         POST /ask, the same answer in one reply
    session(sid)               GET  /sessions/{sid}: one conversation to draw
    recent(limit)              GET  /sessions: the sidebar's list
    delete(sid)                DELETE /sessions/{sid}
    photo(sha256)              GET  /photos/{sha256}: {bytes, mime, variant} or None
    usage()                    GET  /usage: the key's spend against its budget
    health()                   GET  /health

Standard library only (urllib), so the page's image needs no HTTP package. Every failure is
an ApiError(status, code, message): status 0 when the API couldn't be reached. The key is
sent in the X-API-Key header and never appears in an error.

Tests and local runs without a server pass `transport=ServiceTransport(service)`: the same
client calls an agent_api.AgentService in this process, so a test drives the page's client,
the API's service and the real agent graph together.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Iterator

KEY_HEADER = "X-API-Key"
TIMEOUT = 30                 # seconds, for everything but a streamed answer
STREAM_TIMEOUT = 330         # the API gives up on a silent question after 300 s


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{message} ({code}, HTTP {status})" if status else message)
        self.status, self.code, self.message = status, code, message


def parse_sse(lines: Iterable[str]) -> Iterator[dict]:
    """Server-sent events -> {"event", "data"} dicts, data parsed as JSON. An event ends at a
    blank line; lines starting with ':' are comments (keep-alives)."""
    event, data = None, []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                text = "\n".join(data)
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = text
                yield {"event": event or "message", "data": parsed}
            event, data = None, []
        elif line.startswith(":"):
            continue
        else:
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
    if data:                                         # a stream cut off without its blank line
        try:
            yield {"event": event or "message", "data": json.loads("\n".join(data))}
        except ValueError:
            pass


def _error_from(status: int, body: bytes) -> ApiError:
    try:
        e = json.loads(body.decode("utf-8")).get("error") or {}
        return ApiError(status, e.get("code") or "http_error",
                        e.get("message") or f"The API answered HTTP {status}.")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ApiError(status, "http_error", f"The API answered HTTP {status}.")


# ================================================================ transports ===

class UrllibTransport:
    """HTTP to a running API. send() returns (status, headers, body bytes); stream() yields
    the reply's lines (an error status raises ApiError before any line)."""

    def __init__(self, base_url: str, opener=urllib.request.urlopen):
        self.base = base_url.rstrip("/")
        self._open = opener

    def _request(self, method, path, headers, body):
        data = None if body is None else json.dumps(body).encode("utf-8")
        h = dict(headers)
        if data is not None:
            h["Content-Type"] = "application/json"
        return urllib.request.Request(self.base + path, data=data, headers=h, method=method)

    def send(self, method, path, headers, body=None, timeout=TIMEOUT):
        try:
            with self._open(self._request(method, path, headers, body), timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(0, "unreachable", f"Can't reach the agent API at {self.base} "
                                             f"({type(e).__name__}).") from None

    def stream(self, method, path, headers, body=None, timeout=STREAM_TIMEOUT):
        try:
            r = self._open(self._request(method, path, headers, body), timeout=timeout)
        except urllib.error.HTTPError as e:
            raise _error_from(e.code, e.read() or b"") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(0, "unreachable", f"Can't reach the agent API at {self.base} "
                                             f"({type(e).__name__}).") from None
        try:
            for raw in r:
                yield raw.decode("utf-8")
        except (TimeoutError, OSError) as e:
            raise ApiError(0, "stream_lost", f"The answer stopped arriving "
                                             f"({type(e).__name__}).") from None
        finally:
            r.close()


class ServiceTransport:
    """The API's routes, called in this process on an agent_api.AgentService: what the
    FastAPI app does, without a server (tests; a laptop with no API running)."""

    def __init__(self, service):
        self.svc = service

    def _route(self, method, path, headers, body):
        import agent_api
        if path == "/health":
            return self.svc.health()
        if not self.svc.authorised(headers.get(KEY_HEADER)):
            return agent_api.error(401, "unauthorised",
                                   f"Send the API key in the {KEY_HEADER} header.")
        url = urllib.parse.urlsplit(path)
        parts = [p for p in url.path.split("/") if p]
        query = dict(urllib.parse.parse_qsl(url.query))
        if method == "POST" and parts == ["ask"]:
            return self.svc.ask(body.get("question"), body.get("session_id"))
        if parts == ["sessions"] and method == "GET":
            return self.svc.recent(query.get("limit", agent_api.RECENT_DEFAULT))
        if len(parts) == 2 and parts[0] == "sessions":
            sid = urllib.parse.unquote(parts[1])
            return self.svc.session(sid) if method == "GET" else self.svc.delete_session(sid)
        if len(parts) == 2 and parts[0] == "photos":
            return self.svc.photo(urllib.parse.unquote(parts[1]))
        if parts == ["usage"]:
            return self.svc.usage()
        return agent_api.error(404, "not_found", "No such route.")

    def send(self, method, path, headers, body=None, timeout=TIMEOUT):
        import agent_api
        r = self._route(method, path, headers, body)
        if isinstance(r, agent_api.Photo):
            h = {"content-type": r.mime}
            if r.variant:
                h["x-photo-variant"] = r.variant
            return 200, h, r.data
        return r.status, {"content-type": "application/json"}, json.dumps(r.body).encode()

    def stream(self, method, path, headers, body=None, timeout=STREAM_TIMEOUT):
        import agent_api
        if not self.svc.authorised(headers.get(KEY_HEADER)):
            raise _error_from(401, json.dumps(agent_api.error(
                401, "unauthorised", "Send the API key.").body).encode())
        for evt in self.svc.ask_stream(body.get("question"), body.get("session_id")):
            yield from agent_api.sse(evt).splitlines(keepends=True)


# ==================================================================== client ===

class ApiClient:
    def __init__(self, base_url: str | None = None, key: str | None = None, *,
                 transport=None):
        if not key:
            raise ValueError("an API key is required (AGENT_API_KEY)")
        if transport is None:
            if not base_url:
                raise ValueError("the API's address is required (AGENT_API_URL)")
            transport = UrllibTransport(base_url)
        self.base_url = (base_url or "(in process)").rstrip("/")
        self._key = key
        self._t = transport

    def _headers(self) -> dict:
        return {KEY_HEADER: self._key, "Accept": "application/json"}

    def _json(self, method, path, body=None, ok=(200,)) -> dict:
        status, _, data = self._t.send(method, path, self._headers(), body)
        if status not in ok:
            raise _error_from(status, data)
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(status, "bad_reply", "The API's reply wasn't JSON.") from None

    # ------------------------------------------------------------------ routes ---
    def health(self) -> dict:
        """The API's /health body, also when it says it can't answer (503 guard_not_ready)."""
        return self._json("GET", "/health", ok=(200, 503))

    def ask_stream(self, question: str, session_id: str | None = None) -> Iterator[dict]:
        """The answer as it happens: {"event": "session"|"step"|"answer"|"error", "data"}.
        A request the API refuses outright (401, 422 before streaming) raises ApiError."""
        lines = self._t.stream("POST", "/ask/stream", {**self._headers(),
                                                       "Accept": "text/event-stream"},
                               {"question": question, "session_id": session_id})
        yield from parse_sse(lines)

    def ask(self, question: str, session_id: str | None = None) -> dict:
        return self._json("POST", "/ask", {"question": question, "session_id": session_id})

    def session(self, session_id: str) -> dict:
        return self._json("GET", f"/sessions/{urllib.parse.quote(session_id, safe='')}")

    def recent(self, limit: int = 8) -> list[dict]:
        return self._json("GET", f"/sessions?limit={int(limit)}").get("sessions") or []

    def delete(self, session_id: str) -> None:
        self._json("DELETE", f"/sessions/{urllib.parse.quote(session_id, safe='')}")

    def usage(self) -> dict:
        return self._json("GET", "/usage")

    def photo(self, sha256: str) -> dict | None:
        """{bytes, mime, variant} for a photo an answer cites, or None if there is none."""
        status, headers, data = self._t.send(
            "GET", f"/photos/{urllib.parse.quote(sha256, safe='')}", self._headers())
        if status == 404:
            return None
        if status != 200:
            raise _error_from(status, data)
        h = {k.lower(): v for k, v in headers.items()}
        return {"bytes": data, "mime": h.get("content-type", "application/octet-stream"),
                "variant": h.get("x-photo-variant")}


def from_env(env_file: str | None = None, transport=None) -> ApiClient:
    """The client from AGENT_API_URL and AGENT_API_KEY (both required: no default address,
    so a page that isn't configured says so instead of calling somewhere unexpected)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file) if env_file else load_dotenv()
    except ImportError:
        pass
    url = (os.environ.get("AGENT_API_URL") or "").strip()
    key = (os.environ.get("AGENT_API_KEY") or "").strip()
    missing = [n for n, v in (("AGENT_API_URL", url), ("AGENT_API_KEY", key)) if not v]
    if missing and not (transport and key):
        raise ValueError(f"Missing setting(s): {', '.join(missing)}. The page talks only to "
                         f"the agent API; set its address and key (see .env.example).")
    return ApiClient(url or None, key, transport=transport)