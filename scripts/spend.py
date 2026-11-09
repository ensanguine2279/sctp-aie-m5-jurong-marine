#!/usr/bin/env python3
"""The app's LLM spend against its budget, from the LiteLLM proxy (HARNESS_CHECKLIST.md
section 7: "you track cumulative spend against your team's API budget").

The proxy already knows every call this key made and what it cost: it is the same figure
it uses to block the key at its cap. `GET /key/info`, asked with the app's own key, returns
the key's spend, max_budget and the date the budget resets. No admin key is needed or
used here; team-wide spend stays with the proxy's own tools (spend_report.py, the /ui
dashboard), see llm-budget-proxy-setup.md.

The proxy writes spend to its database in batches, so this figure lags by about a minute.
The cost of each call as it happens comes from the x-litellm-response-cost header instead
(agent_app.complete() reads it; the page shows it under each answer).

Usage
    python scripts/spend.py                 # this key: spend, budget, % used, reset date
    python scripts/spend.py --json          # the same, as JSON

With uv (personal repo): `uv run python scripts/spend.py`. Standard library only.
Needs OPENAI_BASE_URL and OPENAI_API_KEY (the app's .env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass

WARN_AT = 0.8              # warn from 80% of the budget used
TIMEOUT_S = 5


@dataclass
class KeySpend:
    ok: bool
    spend: float | None = None           # USD spent in this budget period
    max_budget: float | None = None      # USD cap for the period (None: no cap on the key)
    budget_reset_at: str | None = None   # when the period restarts (ISO, UTC)
    budget_duration: str | None = None   # e.g. "30d"
    member: str | None = None            # metadata.member from members.json
    error: str | None = None             # why the figure isn't available (ok False)

    @property
    def used(self) -> float | None:
        """Fraction of the budget used, or None without a cap."""
        if self.spend is None or not self.max_budget:
            return None
        return self.spend / self.max_budget

    @property
    def warn(self) -> bool:
        return self.used is not None and self.used >= WARN_AT


def parse_key_info(payload: dict) -> KeySpend:
    """LiteLLM's /key/info reply ({"key": ..., "info": {...}}) -> KeySpend. Never keeps the
    key or its hash: only spend, budget and dates."""
    info = payload.get("info", payload) if isinstance(payload, dict) else {}
    if not isinstance(info, dict) or "spend" not in info:
        return KeySpend(False, error="the proxy's reply has no spend figure")

    def num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None
    return KeySpend(True, spend=num(info.get("spend")), max_budget=num(info.get("max_budget")),
                    budget_reset_at=info.get("budget_reset_at"),
                    budget_duration=info.get("budget_duration"),
                    member=(info.get("metadata") or {}).get("member"))


def key_spend(base_url: str | None = None, api_key: str | None = None, *,
              timeout: float = TIMEOUT_S, opener=urllib.request.urlopen) -> KeySpend:
    """This key's spend from the proxy. Never raises: a down proxy, a refused key or a
    malformed reply comes back as KeySpend(ok=False, error=...), so the page can say
    "spend not available" instead of breaking."""
    base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not base_url or not api_key:
        return KeySpend(False, error="OPENAI_BASE_URL and OPENAI_API_KEY must be set")
    if base_url.endswith("/v1"):                      # /key/info sits beside /v1, not in it
        base_url = base_url[:-3]
    req = urllib.request.Request(f"{base_url}/key/info",
                                 headers={"Authorization": f"Bearer {api_key}"})
    try:
        with opener(req, timeout=timeout) as r:
            return parse_key_info(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        return KeySpend(False, error=f"the proxy refused the request (HTTP {e.code})")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return KeySpend(False, error=f"couldn't reach the proxy ({type(e).__name__})")
    except ValueError:
        return KeySpend(False, error="the proxy's reply wasn't JSON")


def budget_line(s: KeySpend) -> str:
    """One line for people: 'Key capstone: $3.42 of $10.00 (34%), resets 2026-12-01'."""
    if not s.ok:
        return f"Spend not available: {s.error}."
    who = f"Key {s.member}" if s.member else "This key"
    reset = f", resets {s.budget_reset_at[:10]}" if s.budget_reset_at else ""
    if s.used is None:
        return f"{who}: ${s.spend:.2f} spent, no budget cap{reset}"
    line = f"{who}: ${s.spend:.2f} of ${s.max_budget:.2f} ({s.used:.0%}){reset}"
    return line + (" - nearly spent: check headroom before a demo" if s.warn else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print as JSON")
    ap.add_argument("--env-file", default=None, help="path to .env (default: nearest .env)")
    a = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(a.env_file) if a.env_file else load_dotenv()
    except ImportError:
        pass                                          # the environment may already be set
    s = key_spend()
    if a.json:
        print(json.dumps({**asdict(s), "used": s.used, "warn": s.warn}, indent=2))
    else:
        print(budget_line(s))
    return 0 if s.ok else 1


if __name__ == "__main__":
    sys.exit(main())