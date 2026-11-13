"""Shared pytest set-up.

- The report header says whether the guardrail's staff name list is loaded (without it the
  app fails closed, so the tests that ask questions fail) and which LangGraph the agent's
  graph runs on.
- Without langgraph installed, tests/mini_langgraph.py stands in for it (12 Nov 2026: the
  agent is a LangGraph graph, so every test that asks a question needs one). The header
  says so; install langgraph to test the real thing (`uv add langgraph` /
  `pip install langgraph`).
"""
from __future__ import annotations

import mini_langgraph

_STAND_IN = mini_langgraph.install()          # does nothing where langgraph is installed


def pytest_report_header(config):
    lines = []
    try:
        import guardrail
    except Exception as e:                     # an import problem shows up in the tests too
        lines.append(f"guardrail: not importable ({type(e).__name__})")
    else:
        n = guardrail.names_loaded()
        if n:
            lines.append(f"guardrail: staff name list loaded ({n} name phrases)")
        else:
            lines.append("guardrail: STAFF NAME LIST MISSING (scripts/guard_names.py). The app "
                         "fails closed, so the agent tests will fail. Build it: python "
                         "scripts/guardrail.py build-names data/sql/jurongmarine_cmms.sql "
                         "--extra Halim")
    if _STAND_IN:
        lines.append("langgraph: NOT INSTALLED, the agent's graph runs on tests/mini_langgraph.py "
                     "(uv add langgraph / pip install langgraph to test the real one)")
    else:
        try:
            from importlib.metadata import version
            lines.append(f"langgraph: {version('langgraph')}")
        except Exception:
            lines.append("langgraph: installed")
    return lines