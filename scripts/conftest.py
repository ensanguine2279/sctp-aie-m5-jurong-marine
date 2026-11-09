"""Shared pytest set-up. Only reports: it changes no test's behaviour."""
from __future__ import annotations


def pytest_report_header(config):
    """At the top of every run: is the guardrail's staff name list loaded? Without it the
    app fails closed (every question refused), so the agent, page and eval tests that ask
    questions fail too; this line says why, once, before those failures do."""
    try:
        import guardrail
    except Exception as e:                     # an import problem shows up in the tests too
        return f"guardrail: not importable ({type(e).__name__})"
    n = guardrail.names_loaded()
    if n:
        return f"guardrail: staff name list loaded ({n} name phrases)"
    return ("guardrail: STAFF NAME LIST MISSING (scripts/guard_names.py). The app fails closed, "
            "so the agent tests will fail. Build it: python scripts/guardrail.py build-names "
            "data/sql/jurongmarine_cmms.sql --extra Halim")