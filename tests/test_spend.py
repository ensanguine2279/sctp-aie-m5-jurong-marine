"""Tests for scripts/spend.py: the key's spend against its budget, from LiteLLM's /key/info.
No network: the HTTP call is replaced by a stand-in. The reply shape is the one the real
proxy gave for the app's key on 9 Nov 2026 (key and token values left out)."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import spend as sp

REPLY = {"key": "sk-...", "info": {
    "key_name": "sk-...xxxx", "spend": 3.4219, "max_budget": 10.0, "budget_duration": "30d",
    "budget_reset_at": "2026-12-01T00:00:00+00:00", "rpm_limit": 60,
    "metadata": {"member": "capstone", "tag_rpm_limit": {}}, "status": "active"}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_for(payload=None, raise_=None, seen=None):
    def opener(req, timeout):
        if seen is not None:
            seen.append((req.full_url, req.get_header("Authorization"), timeout))
        if raise_:
            raise raise_
        return _Resp(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    return opener


def test_the_reply_becomes_spend_budget_and_percentage():
    s = sp.parse_key_info(REPLY)
    assert s.ok and s.spend == 3.4219 and s.max_budget == 10.0 and s.member == "capstone"
    assert s.used == pytest.approx(0.34219) and not s.warn
    assert sp.budget_line(s) == "Key capstone: $3.42 of $10.00 (34%), resets 2026-12-01"
    assert "sk-" not in json.dumps(vars(s))                 # never keeps the key


def test_nearly_spent_is_a_warning():
    s = sp.parse_key_info({"info": {**REPLY["info"], "spend": 8.5}})
    assert s.warn and "nearly spent" in sp.budget_line(s)


def test_a_key_without_a_cap():
    s = sp.parse_key_info({"info": {"spend": 1.2, "max_budget": None}})
    assert s.used is None and not s.warn and "no budget cap" in sp.budget_line(s)


def test_the_request_goes_to_key_info_with_the_apps_key():
    seen = []
    s = sp.key_spend("https://proxy.example/v1", "sk-test", opener=opener_for(REPLY, seen=seen))
    assert s.ok
    assert seen == [("https://proxy.example/key/info", "Bearer sk-test", sp.TIMEOUT_S)]


@pytest.mark.parametrize("raise_,says", [
    (urllib.error.HTTPError("u", 401, "no", {}, None), "HTTP 401"),
    (urllib.error.URLError("down"), "couldn't reach the proxy"),
    (TimeoutError(), "couldn't reach the proxy"),
])
def test_a_proxy_problem_never_raises(raise_, says):
    s = sp.key_spend("https://p", "k", opener=opener_for(raise_=raise_))
    assert not s.ok and says in s.error
    assert sp.budget_line(s).startswith("Spend not available:")


def test_a_bad_reply_or_no_settings_is_not_available(monkeypatch):
    assert "wasn't JSON" in sp.key_spend("https://p", "k", opener=opener_for(b"<html>")).error
    assert "no spend" in sp.key_spend("https://p", "k", opener=opener_for({"info": {}})).error
    # Empty arguments fall back to the environment, so clear it: on 9 Nov 2026 this test
    # failed on a machine whose shell had the app's real OPENAI_* settings exported.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert "must be set" in sp.key_spend("", "", opener=opener_for(REPLY)).error


def test_settings_come_from_the_environment_when_not_given(monkeypatch):
    seen = []
    monkeypatch.setenv("OPENAI_BASE_URL", "https://from-env.example")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert sp.key_spend(opener=opener_for(REPLY, seen=seen)).ok
    assert seen[0][:2] == ("https://from-env.example/key/info", "Bearer sk-env")


def test_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(sp, "key_spend", lambda **kw: sp.parse_key_info(REPLY))
    assert sp.main([]) == 0 and "$3.42 of $10.00" in capsys.readouterr().out
    assert sp.main(["--json"]) == 0 and json.loads(capsys.readouterr().out)["used"] == pytest.approx(0.34219)
    monkeypatch.setattr(sp, "key_spend", lambda **kw: sp.KeySpend(False, error="x"))
    assert sp.main([]) == 1