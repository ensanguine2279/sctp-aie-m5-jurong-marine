"""Tests for frontend/api_client.py, the page's only way to the agent (phase 3, 15 Nov 2026).

The HTTP side runs on a stand-in for urllib's opener, so no server or network is needed. The
end-to-end tests use ServiceTransport: the client calls a real agent_api.AgentService, which
runs the real agent graph on a scripted model and saves to real conversations (SQLite and
LangGraph's in-memory checkpointer), exactly as tests/test_agent_api.py sets them up.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import agent_api as api
import api_client as ac
import conversations as conv
from test_agent_api import KEY, PLATE, SHA, conversations, scripted, service  # noqa: F401
import agent_app as app
from test_agent_graph import model_says, result


# ------------------------------------------------------------------- SSE ---

def test_server_sent_events_are_read_back_as_sent():
    text = "".join(api.sse(e) for e in (
        {"event": "session", "data": {"session_id": "a" * 32}},
        {"event": "step", "data": {"node": "model", "round": 1}},
        {"event": "answer", "data": {"answer": "Line one.\nLine two."}}))
    got = list(ac.parse_sse(text.splitlines(keepends=True)))
    assert [e["event"] for e in got] == ["session", "step", "answer"]
    assert got[2]["data"]["answer"] == "Line one.\nLine two."


def test_comments_crlf_split_data_and_a_cut_off_last_event():
    lines = [": keep-alive\r\n", "event: step\r\n", 'data: {"node":\r\n', 'data: "tools"}\r\n',
             "\r\n", "event: answer\n", 'data: {"answer": "x"}\n']        # no closing blank
    got = list(ac.parse_sse(lines))
    assert got == [{"event": "step", "data": {"node": "tools"}},
                   {"event": "answer", "data": {"answer": "x"}}]


# ------------------------------------------------------------------ HTTP ---

class FakeReply:
    def __init__(self, status=200, body=b"{}", headers=None, lines=None):
        self.status, self._body = status, body
        self.headers = headers or {"Content-Type": "application/json"}
        self._lines = lines or []
        self.closed = False

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener_returning(reply=None, raise_=None, seen=None):
    def opener(req, timeout=None):
        if seen is not None:
            seen.append(req)
        if raise_:
            raise raise_
        return reply
    return opener


def http_error(code, body):
    return urllib.error.HTTPError("http://api/x", code, "err", {}, io.BytesIO(body))


def test_the_key_goes_in_its_header_and_json_comes_back():
    seen = []
    t = ac.UrllibTransport("https://api.example/", opener_returning(
        FakeReply(body=b'{"sessions": [{"session_id": "x"}]}'), seen=seen))
    c = ac.ApiClient("https://api.example/", KEY, transport=t)
    assert c.recent(3) == [{"session_id": "x"}]
    req = seen[0]
    assert req.full_url == "https://api.example/sessions?limit=3"
    assert req.get_header("X-api-key") == KEY and req.get_method() == "GET"


def test_an_api_error_keeps_its_code_and_message():
    body = json.dumps({"error": {"code": "unknown_session", "message": "no conversation"}})
    c = ac.ApiClient("http://api", KEY, transport=ac.UrllibTransport(
        "http://api", opener_returning(raise_=http_error(404, body.encode()))))
    with pytest.raises(ac.ApiError) as e:
        c.session("a" * 32)
    assert (e.value.status, e.value.code) == (404, "unknown_session")


def test_an_unreachable_api_is_status_0_and_never_shows_the_key():
    c = ac.ApiClient("http://api.internal", KEY, transport=ac.UrllibTransport(
        "http://api.internal", opener_returning(raise_=urllib.error.URLError("refused"))))
    with pytest.raises(ac.ApiError) as e:
        c.usage()
    assert e.value.status == 0 and e.value.code == "unreachable"
    assert "api.internal" in str(e.value) and KEY not in str(e.value)


def test_a_reply_that_isnt_json_is_an_error_not_a_crash():
    c = ac.ApiClient("http://api", KEY, transport=ac.UrllibTransport(
        "http://api", opener_returning(FakeReply(body=b"<html>proxy error</html>"))))
    with pytest.raises(ac.ApiError) as e:
        c.usage()
    assert e.value.code == "bad_reply"


def test_streaming_over_http_reads_lines_as_they_come_and_closes():
    reply = FakeReply(lines=[b"event: session\n", b'data: {"session_id": "s"}\n', b"\n",
                             b"event: answer\n", b'data: {"answer": "ok"}\n', b"\n"])
    seen = []
    c = ac.ApiClient("http://api", KEY, transport=ac.UrllibTransport(
        "http://api", opener_returning(reply, seen=seen)))
    got = list(c.ask_stream("hello", None))
    assert [e["event"] for e in got] == ["session", "answer"] and reply.closed
    assert json.loads(seen[0].data) == {"question": "hello", "session_id": None}
    assert seen[0].get_header("Content-type") == "application/json"


def test_a_refused_stream_raises_before_any_event():
    body = json.dumps({"error": {"code": "unauthorised", "message": "Send the key."}}).encode()
    c = ac.ApiClient("http://api", "wrong", transport=ac.UrllibTransport(
        "http://api", opener_returning(raise_=http_error(401, body))))
    with pytest.raises(ac.ApiError) as e:
        list(c.ask_stream("hello"))
    assert e.value.status == 401


def test_a_missing_photo_is_none_and_a_found_one_keeps_its_type():
    c = ac.ApiClient("http://api", KEY, transport=ac.UrllibTransport(
        "http://api", opener_returning(raise_=http_error(404, b"{}"))))
    assert c.photo(SHA) is None
    c = ac.ApiClient("http://api", KEY, transport=ac.UrllibTransport("http://api", opener_returning(
        FakeReply(body=b"jpeg", headers={"Content-Type": "image/jpeg",
                                         "X-Photo-Variant": "redacted"}))))
    assert c.photo(SHA) == {"bytes": b"jpeg", "mime": "image/jpeg", "variant": "redacted"}


# -------------------------------------------------------------- settings ---

def _empty_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text("", encoding="utf-8")
    return str(p)


def test_both_settings_are_required_and_named(tmp_path):
    with pytest.raises(ValueError) as e:
        ac.from_env(_empty_env(tmp_path))
    assert "AGENT_API_URL" in str(e.value) and "AGENT_API_KEY" in str(e.value)


def test_the_key_alone_is_not_enough_and_never_printed(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", KEY)
    with pytest.raises(ValueError) as e:
        ac.from_env(_empty_env(tmp_path))
    assert "AGENT_API_URL" in str(e.value) and KEY not in str(e.value)


def test_the_page_reads_only_its_two_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_URL", "https://jme-agent-api.example.run.app/")
    monkeypatch.setenv("AGENT_API_KEY", KEY + "\r")         # a CRLF .env
    c = ac.from_env(_empty_env(tmp_path))
    assert c.base_url == "https://jme-agent-api.example.run.app" and c._key == KEY


# ------------------------------------------------- end to end (in process) ---

def client_for(svc, key=KEY):
    return ac.ApiClient(None, key, transport=ac.ServiceTransport(svc))


def test_a_conversation_through_the_client_from_first_question_to_delete(scripted):  # noqa: F811
    convs = conversations()
    c = client_for(service(convs))
    scripted([("c1", "list_machines", "{}")], "Twelve machines.", sources=["CMMS machines"])
    events = list(c.ask_stream("how many machines?"))
    assert [e["event"] for e in events] == ["session"] + ["step"] * 5 + ["answer"]
    sid = events[0]["data"]["session_id"]
    assert events[-1]["data"]["session_id"] == sid

    scripted("Still twelve.")
    events = list(c.ask_stream("and now?", sid))
    assert events[0]["data"]["session_id"] == sid and events[-1]["data"]["turns"] == 2

    got = c.session(sid)
    assert [m["text"] for m in got["messages"]] == ["how many machines?", "Twelve machines.",
                                                    "and now?", "Still twelve."]
    assert [r["session_id"] for r in c.recent(8)] == [sid]
    c.delete(sid)
    with pytest.raises(ac.ApiError) as e:
        c.session(sid)
    assert e.value.status == 404 and c.recent(8) == []


def test_the_wrong_key_gets_nothing_but_health():
    c = client_for(service(conversations()), key="not-the-key")
    assert "guard_ready" in c.health()
    for call in (lambda: c.recent(), lambda: c.session(conv.new_id()), lambda: c.usage(),
                 lambda: c.photo(SHA), lambda: list(c.ask_stream("hi"))):
        with pytest.raises(ac.ApiError) as e:
            call()
        assert e.value.status == 401


def test_photos_come_through_the_client_by_hash(monkeypatch):
    monkeypatch.setattr(app, "complete", model_says(
        [("c1", "read_nameplate", '{"machine": "CNC-02"}')], "Rated 7.5 kW."))
    monkeypatch.setattr(app.at, "run_tool", lambda db, name, args: result(sources=[PLATE]))
    svc = service(conversations(), photo_fn=lambda db, sha: {
        "bytes": b"jpeg", "mime": "image/jpeg", "variant": "as_is", "source_file": "p.jpg"}
        if sha == SHA else None)
    c = client_for(svc)
    answer = c.ask("what power is CNC-02 rated for?")
    assert [i["sha256"] for i in answer["images"]] == [SHA]
    assert c.photo(SHA)["bytes"] == b"jpeg" and c.photo("cd" * 32) is None


def test_a_bad_question_streams_one_error_event():
    c = client_for(service(conversations()))
    assert [e["event"] for e in c.ask_stream("   ")] == ["error"]


# ---------------------------------------- the private API on Cloud Run (16 Nov 2026) ---

def jwt(exp):
    import base64

    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'RS256'})}.{part({'aud': 'x', 'exp': exp})}.sig"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def metadata_server(tokens, seen):
    def opener(req, timeout=None):
        seen.append(req)
        return FakeReply(body=tokens.pop(0).encode())
    return opener


def test_the_identity_token_comes_from_the_metadata_server_for_the_apis_url():
    seen = []
    tok = ac.GoogleIdToken("https://jme-agent-api-abc.a.run.app/",
                           opener=metadata_server([jwt(10_000)], seen), clock=Clock(1_000))
    assert tok() == jwt(10_000)
    req = seen[0]
    assert req.full_url.startswith(ac.METADATA_TOKEN_URL + "?audience=")
    assert "audience=https%3A%2F%2Fjme-agent-api-abc.a.run.app" in req.full_url
    assert req.get_header("Metadata-flavor") == "Google"


def test_the_token_is_kept_until_shortly_before_it_expires():
    seen, clock = [], Clock(1_000)
    tok = ac.GoogleIdToken("https://api.run.app", clock=clock,
                           opener=metadata_server([jwt(4_600), jwt(8_200)], seen))
    tok()
    clock.t = 4_600 - ac.TOKEN_MARGIN - 1
    assert tok() == jwt(4_600) and len(seen) == 1                # still good: not fetched
    clock.t = 4_600 - ac.TOKEN_MARGIN
    assert tok() == jwt(8_200) and len(seen) == 2                # near expiry: a new one


def test_a_token_without_a_readable_expiry_is_still_renewed():
    seen, clock = [], Clock(0)
    tok = ac.GoogleIdToken("https://api.run.app", clock=clock,
                           opener=metadata_server(["a.b.c", "d.e.f"], seen))
    assert tok() == "a.b.c"
    clock.t = ac.TOKEN_FALLBACK_LIFE - ac.TOKEN_MARGIN
    assert tok() == "d.e.f"
    assert ac.token_expiry("not-a-jwt") is None and ac.token_expiry(jwt(5)) == 5.0


def test_no_metadata_server_is_an_api_error_that_says_what_to_do():
    tok = ac.GoogleIdToken("https://api.run.app",
                           opener=opener_returning(raise_=urllib.error.URLError("no host")))
    with pytest.raises(ac.ApiError) as e:
        tok()
    assert e.value.status == 0 and e.value.code == "no_identity_token"
    assert "unset it locally" in e.value.message
    junk = ac.GoogleIdToken("https://api.run.app", opener=opener_returning(FakeReply(
        body=b"<html>not a token</html>")))
    with pytest.raises(ac.ApiError):
        junk()


def test_every_request_carries_the_token_next_to_the_key():
    seen = []
    t = ac.UrllibTransport("https://api.run.app", opener_returning(
        FakeReply(body=b'{"sessions": []}'), seen=seen))
    c = ac.ApiClient("https://api.run.app", KEY, transport=t, id_token=lambda: "tok.en.x")
    c.recent()
    assert seen[0].get_header("Authorization") == "Bearer tok.en.x"
    assert seen[0].get_header("X-api-key") == KEY
    plain = ac.ApiClient("https://api.run.app", KEY, transport=t)
    plain.recent()
    assert seen[1].get_header("Authorization") is None          # locally: the key alone


def test_google_auth_is_switched_on_only_by_its_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_API_URL", "https://jme-agent-api-abc.a.run.app")
    monkeypatch.setenv("AGENT_API_KEY", KEY)
    assert ac.from_env(_empty_env(tmp_path))._id_token is None
    monkeypatch.setenv("AGENT_API_AUTH", "Google")
    c = ac.from_env(_empty_env(tmp_path))
    assert isinstance(c._id_token, ac.GoogleIdToken)
    assert c._id_token.audience == "https://jme-agent-api-abc.a.run.app"
    monkeypatch.setenv("AGENT_API_AUTH", "gogle")                      # a typo: refused
    with pytest.raises(ValueError) as e:
        ac.from_env(_empty_env(tmp_path))
    assert "AGENT_API_AUTH" in str(e.value)
    monkeypatch.setenv("AGENT_API_AUTH", "google")
    monkeypatch.setenv("AGENT_API_URL", "http://localhost:8000")       # no token for that
    with pytest.raises(ValueError) as e:
        ac.from_env(_empty_env(tmp_path))
    assert "https" in str(e.value)