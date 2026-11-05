#!/usr/bin/env python3
"""The agent's tools: fixed, parameterised queries over the cleaned data, every answer
with its sources.

The LLM never writes SQL for a question these tools cover. It picks a tool and fills in
its arguments; this code validates them (pydantic), resolves machine names, runs a fixed
query as the read-only app user (agent_ro, see agent_user.py) and returns a ToolResult:

    data      what the answer may say; every number in the answer comes from here
    sources   where each fact came from: table, file, line / row / image, SHA-256
    notes     caveats the answer must carry (OCR uncertain, scale converted, ...)
    error     set when the tool couldn't answer: the app says "I couldn't check X",
              never a made-up value (HARNESS_CHECKLIST.md section 4)

Same question -> same tool and arguments -> same SQL -> same answer.

Tools                     Client ask   Reads (all granted to agent_ro)
    list_machines           both       machine_overview
    get_nameplate           Ask 1      machine_nameplate, img_nameplates_audit
    machine_lubrication     Ask 1      the two above + machine_spares, critical_spares_audit
    get_spares              Ask 1      machine_spares, critical_spares_audit
    count_incidents         Ask 2      incident_detail, cmms_safety_incidents_audit
    list_incidents          Ask 2      the same
    get_incident            Ask 2      + incident_report_vs_cmms, img_incident_reports_audit
    list_work_orders        both       work_order_detail, cmms_work_orders_audit
    sensor_trend            Ask 2      vibration_temp_audit
    compare_sensor_windows  Ask 2      vibration_temp_audit (CNC-02 vs the other machines)
    find_sop                Ask 1      sops, sop_machines, machine_sops, sops_audit, sop_steps_audit
    query_sql               fallback   one checked SELECT on the granted objects

Severity: every incident tool works on severity_5_worst only. A severity number from the
user must come with the scale it is on ("5_is_worst" or "1_is_worst"); without one the
tool refuses with needs_clarification, so the app asks instead of guessing (trap list C1,
the auditor's test).

Usage
    python scripts/agent_tools.py schemas                       # the tool definitions for the LLM
    python scripts/agent_tools.py call find_sop '{"question": "entering the blasting chamber"}'
    python scripts/agent_tools.py call count_incidents '{"date_from": "2025-01-01", "date_to": "2025-12-31", "at_least_as_severe_as": 4, "severity_scale": "5_is_worst"}'

With uv (personal repo): `uv run python scripts/agent_tools.py ...`.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("agent_tools")

DEFAULT_TIMEOUT_MS = 10_000       # per query; MySQL's MAX_EXECUTION_TIME (SELECT only)
SCALE_SWITCH = date(2025, 7, 1)   # severity: 1 = worst before, 5 = worst from (trap list C1)
READING_MINUTES = 12              # one sensor reading per machine every 12 minutes (L4)
MAX_ROWS = 50                     # rows a listing tool returns to the model
SQL_ROW_LIMIT = 200               # LIMIT forced on query_sql


# =================================================================== results ===

class ToolError(Exception):
    """An honest 'can't answer this' with a code the app can act on:
    needs_clarification (ask the user), unknown_machine, ambiguous_machine, no_data,
    not_allowed (query_sql refused), bad_arguments."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any = None
    sources: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: dict | None = None            # {"code": ..., "message": ...}
    queries: list[dict] = field(default_factory=list)   # trace: sql, params, rows, ms
    ms: int = 0

    def to_dict(self, trace: bool = True) -> dict:
        d = {"tool": self.tool, "ok": self.ok, "data": self.data, "sources": self.sources,
             "notes": self.notes, "error": self.error, "ms": self.ms}
        if trace:
            d["queries"] = self.queries
        return plain(d)

    def for_model(self) -> str:
        """What goes back to the LLM: no SQL trace (the app logs it), compact JSON."""
        return json.dumps(self.to_dict(trace=False), ensure_ascii=False, separators=(",", ":"))


def plain(v):
    """Values JSON can hold: dates as ISO strings, Decimal/numpy as float/int, NaN as None."""
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if v is None or isinstance(v, (str, bool)):
        return v
    if pd.api.types.is_scalar(v) and pd.isna(v):     # NaN, NaT, pd.NA
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return None if pd.isna(v) else v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "item"):                  # numpy scalar
        v = v.item()
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 4)
    return v


def cite(kind: str, obj: str, *, file: str | None = None, locator: str | None = None,
         sha256: str | None = None, what: str | None = None) -> dict:
    """One source. kind: table | file | image | sop. `label` is what the answer shows."""
    where = ", ".join(x for x in [file, locator] if x) or obj
    label = f"{where} ({obj})" if file else where
    if what:
        label = f"{what}: {label}"
    if sha256:
        label += f", SHA-256 {str(sha256)[:12]}…"
    return {k: v for k, v in {"kind": kind, "object": obj, "file": file, "locator": locator,
                              "sha256": sha256, "what": what, "label": label}.items()
            if v is not None}


def lines_locator(word: str, values, limit: int = 15) -> str:
    vals = sorted({int(v) for v in values if v is not None and not pd.isna(v)})
    if not vals:
        return ""
    shown = ", ".join(str(v) for v in vals[:limit])
    more = f" and {len(vals) - limit} more" if len(vals) > limit else ""
    return f"{word}{'s' if len(vals) > 1 else ''} {shown}{more}"


def cite_rows(df: pd.DataFrame, obj: str, kind: str, locator_col: str, word: str,
              sha_col: str = "file_sha256", what: str | None = None) -> list[dict]:
    """One source per file, listing the lines/rows the answer used."""
    if df is None or not len(df) or "source_file" not in df:
        return [cite("table", obj, what=what)]
    out = []
    keys = df.assign(_f=df["source_file"].fillna("?"),
                     _h=df[sha_col].fillna("") if sha_col in df else "")
    for (f, sha), g in keys.groupby(["_f", "_h"]):
        out.append(cite(kind, obj, file=f, locator=lines_locator(word, g[locator_col]),
                        sha256=sha or None, what=what))
    return out


# ====================================================================== db ===

@dataclass
class QueryRecord:
    sql: str
    params: dict
    rows: int
    ms: int


class Db:
    """Read-only query access for the tools. `run(sql, params)` executes one statement
    and returns its rows as dicts; every call is recorded for the trace."""

    def __init__(self, run: Callable[[str, dict], list[dict]], dialect: str):
        self._run, self.dialect = run, dialect
        self.calls: list[QueryRecord] = []
        self.cache: dict[str, Any] = {}

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        t0 = time.perf_counter()
        rows = self._run(sql, params)
        self.calls.append(QueryRecord(" ".join(sql.split()), plain(params), len(rows),
                                      int((time.perf_counter() - t0) * 1000)))
        return rows

    def frame(self, sql: str, params: dict | None = None, columns: list[str] | None = None
              ) -> pd.DataFrame:
        rows = self.query(sql, params)
        return pd.DataFrame(rows, columns=columns) if not rows else pd.DataFrame(rows)


def mysql_db(engine, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Db:
    """Db over a SQLAlchemy engine. MAX_EXECUTION_TIME stops a runaway SELECT."""
    from sqlalchemy import text

    def run(sql, params):
        with engine.connect() as conn:
            conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout_ms)}"))
            return [dict(r._mapping) for r in conn.execute(text(sql), params)]

    return Db(run, "mysql")


def sqlite_db(conn) -> Db:
    """Db over a sqlite3 connection (tests). Same :name parameters as SQLAlchemy text()."""
    def run(sql, params):
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description or []]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    return Db(run, "sqlite")


def connect(env_file: str | None = None, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Db:
    """Connect as the app user (AGENT_DB_USERNAME), never as DB_USERNAME, who owns every
    table including employee names (PDPA, agent_user.py)."""
    from dotenv import load_dotenv

    from db_config import getenv

    load_dotenv(env_file) if env_file else load_dotenv()
    user, pw = getenv("AGENT_DB_USERNAME", "agent_ro"), getenv("AGENT_DB_PASSWORD")
    if not pw:
        raise ValueError("Set AGENT_DB_PASSWORD in .env: the app connects as the read-only user.")
    if user == getenv("DB_USERNAME"):
        raise ValueError("AGENT_DB_USERNAME is the owner account (DB_USERNAME). The app must "
                         "connect as the read-only user made by agent_user.py.")
    from db_config import DbConfig

    cfg = DbConfig(user=user, password=pw, host=getenv("DB_HOST"),
                   port=int(getenv("DB_PORT", "3306")), name=getenv("DB_NAME"))
    return mysql_db(cfg.engine(), timeout_ms)


def _between(col: str, date_from, date_to, params: dict, *, end_exclusive=True) -> list[str]:
    """WHERE parts for a date range. Bounds are passed as ISO strings, which MySQL and
    SQLite both compare correctly with DATE / DATETIME columns. date_to is inclusive."""
    out = []
    if date_from:
        params["date_from"] = date_from.isoformat()
        out.append(f"{col} >= :date_from")
    if date_to:
        params["date_to_excl"] = (date_to + timedelta(days=1)).isoformat()
        out.append(f"{col} < :date_to_excl")
    return out


def _like(text_: str) -> str:
    """LIKE pattern for `text_`, escaped with '!' (a backslash means different things in
    MySQL and SQLite string literals)."""
    return "%" + re.sub(r"([!%_])", r"!\1", text_.lower()) + "%"


# ================================================================ machines ===

def _key(s: str) -> str:
    """'CNC-02', 'cnc 2', 'CNC2' -> 'cnc2'; 'PMP-T01' -> 'pmpt1'."""
    k = re.sub(r"[^a-z0-9]", "", str(s).lower())
    return re.sub(r"\d+", lambda m: str(int(m.group())), k)


_NAME_STOP = {"the", "machine", "our", "one", "unit", "number", "no", "for", "of", "and", "big",
              "small", "main", "shop", "new", "old"}
# Words people use for a machine that its CMMS name doesn't contain. A judgement, like
# ingest_sops.ALIASES: keep it short; add an entry with a test when a real question misses.
MACHINE_ALIASES = {
    "sandblasting": "BLAST-01", "sandblast": "BLAST-01", "blast booth": "BLAST-01",
    "blast room": "BLAST-01", "test pump sump": "PMP-T01", "pump sump": "PMP-T01",
}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def machines(db: Db) -> list[dict]:
    if "machines" not in db.cache:
        db.cache["machines"] = db.query(
            "SELECT machine_id, machine_name, model, sensor_readings, first_reading, last_reading "
            "FROM machine_overview ORDER BY machine_id")
    return db.cache["machines"]


def resolve_machine(text_: str, known: list[dict]) -> str:
    """The CMMS machine_id the user means, or ToolError. Never a guess: an unknown name
    is unknown_machine, two candidates are ambiguous_machine (the app asks which)."""
    if not text_ or not text_.strip():
        raise ToolError("bad_arguments", "No machine given.")
    ids = {_key(m["machine_id"]): m["machine_id"] for m in known}
    k = _key(text_)
    if k in ids:
        return ids[k]
    # an id inside longer text: "CNC-02 spindle"
    found = {ids[_key(t)] for t in re.findall(r"[A-Za-z]{2,6}[\s-]?[A-Za-z]?\d{1,3}", text_)
             if _key(t) in ids}
    if len(found) == 1:
        return found.pop()
    low = text_.lower()
    for phrase, mid in MACHINE_ALIASES.items():
        if phrase in low and mid in ids.values():
            return mid
    words = {_stem(w) for w in re.findall(r"[a-z]+", low) if len(w) >= 3 and w not in _NAME_STOP}
    numbers = {int(n) for n in re.findall(r"\d+", low)}
    scored = []
    for m in known:
        name_words = {_stem(w) for w in re.findall(r"[a-z]+", f"{m['machine_name']} {m['model']}"
                                                    .lower())}
        s = len(words & name_words)
        if s:
            scored.append((s, m["machine_id"]))
    if scored:
        best = max(s for s, _ in scored)
        cands = sorted(mid for s, mid in scored if s == best)
        if len(cands) > 1 and numbers:     # "crane 2" -> CRN-02
            by_no = [mid for mid in cands
                     if int((re.findall(r"\d+", mid) or ["-1"])[-1]) in numbers]
            cands = by_no or cands
        if len(cands) == 1:
            return cands[0]
        names = ", ".join(f"{mid} ({_name(known, mid)})" for mid in cands)
        raise ToolError("ambiguous_machine", f"'{text_}' could be {names}. Ask which one.")
    raise ToolError("unknown_machine",
                    f"No machine in the CMMS matches '{text_}'. The 12 machines are: "
                    + ", ".join(f"{m['machine_id']} ({m['machine_name']})" for m in known) + ".")


def _name(known, mid):
    return next((m["machine_name"] for m in known if m["machine_id"] == mid), mid)


def machine_arg(db: Db, text_: str | None) -> str | None:
    return resolve_machine(text_, machines(db)) if text_ else None


# ================================================================== registry ===

class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Tool:
    name: str
    description: str
    args: type[Args]
    fn: Callable[[Db, Any], ToolResult]


TOOLS: dict[str, Tool] = {}


def tool(name: str, description: str, args: type[Args]):
    def register(fn):
        TOOLS[name] = Tool(name, " ".join(description.split()), args, fn)
        return fn
    return register


def _schema(model: type[Args]) -> dict:
    s = model.model_json_schema()
    s.pop("title", None)
    for p in s.get("properties", {}).values():
        p.pop("title", None)
    return s


def tool_schemas() -> list[dict]:
    """OpenAI-style function definitions (what the LiteLLM proxy takes in `tools`)."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": _schema(t.args)}} for t in TOOLS.values()]


def run_tool(db: Db, name: str, arguments: dict | str | None) -> ToolResult:
    """Validate, run, and never raise: a failure becomes an honest ToolResult."""
    t0, first_call = time.perf_counter(), len(db.calls)
    result = None
    try:
        if name not in TOOLS:
            raise ToolError("unknown_tool", f"There is no tool called {name!r}.")
        if isinstance(arguments, str):
            arguments = json.loads(arguments or "{}")
        args = TOOLS[name].args.model_validate(arguments or {})
        result = TOOLS[name].fn(db, args)
    except ToolError as e:
        result = ToolResult(name, False, error={"code": e.code, "message": e.message})
    except (ValidationError, json.JSONDecodeError) as e:
        msg = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'arguments'}: {x['msg']}"
                        for x in e.errors()) if isinstance(e, ValidationError) else str(e)
        result = ToolResult(name, False, error={"code": "bad_arguments", "message": msg})
    except Exception as e:   # database down, timeout, a bug: never a made-up answer
        log.exception("tool %s failed", name)
        result = ToolResult(name, False, error={
            "code": "tool_failed",
            "message": f"{name} couldn't get an answer from the data ({type(e).__name__}). "
                       f"Say this couldn't be checked; don't estimate."})
    result.tool = name
    result.queries = [vars(q) for q in db.calls[first_call:]]
    result.ms = int((time.perf_counter() - t0) * 1000)
    log.info(json.dumps(plain({"event": "tool_call", "tool": name, "args": arguments,
                               "ok": result.ok, "error": result.error, "ms": result.ms,
                               "queries": [{"sql": q["sql"][:200], "rows": q["rows"],
                                            "ms": q["ms"]} for q in result.queries]})))
    return result


# ============================================================ list_machines ===

class NoArgs(Args):
    pass


@tool("list_machines", """The 12 machines in the CMMS: id, name, model, and what data each
has (work orders, incidents, spares, sensor readings and their dates). Use it to find a
machine id, or to say which machines have sensors (only 5 do).""", NoArgs)
def list_machines(db: Db, a: NoArgs) -> ToolResult:
    rows = db.query("SELECT machine_id, machine_name, model, year_installed, work_orders, "
                    "breakdowns, open_work_orders, incidents, spare_parts, spares_below_min, "
                    "sensor_readings, first_reading, last_reading FROM machine_overview "
                    "ORDER BY machine_id")
    no_sensor = [r["machine_id"] for r in rows if not r["sensor_readings"]]
    return ToolResult("list_machines", True, data={"machines": rows},
                      sources=[cite("table", "machine_overview",
                                    what="CMMS machines with counts from every source")],
                      notes=[f"No sensor readings for: {', '.join(no_sensor)}."] if no_sensor else [])


# ============================================================ nameplates ===

NAMEPLATE_FIELDS = ["manufacturer", "plate_model", "serial_no", "mfg_month", "power_kw", "phases",
                    "voltage_min_v", "voltage_max_v", "capacity_raw", "rated_current_a",
                    "duty_cycle_pct", "weight_kg", "lubricant", "grease_interval_h"]
# warning suffix (ingest_images.py) -> nameplate field
_FLAG_FIELD = {"lubricant": "lubricant", "oil": "lubricant", "grease_interval": "grease_interval_h",
               "grease": "grease_interval_h", "weight": "weight_kg", "power": "power_kw",
               "serial": "serial_no", "serial_no": "serial_no", "mfg_date": "mfg_month",
               "mfg_month": "mfg_month", "date": "mfg_month", "capacity": "capacity_raw",
               "model": "plate_model", "manufacturer": "manufacturer", "maker": "manufacturer",
               "voltage": "voltage_min_v", "current": "rated_current_a", "phases": "phases",
               "duty_cycle": "duty_cycle_pct"}
_WHOLE_FLAGS = {"lubricant_without_grade": "lubricant", "bad_weight": "weight_kg",
                "bad_mfg_date": "mfg_month", "duty_cycle_rating_on_non_welder": "capacity_raw",
                "placeholder_manufacturer": "manufacturer"}
_UNREADABLE = ("failed_format_", "bad_")
# Trap-list findings about plate values that a clear OCR reading doesn't settle. Kept in
# code, not in the prompt, so every answer that touches these fields carries them.
KNOWN_CONCERNS = {
    ("BLAST-01", "capacity_raw"): "The plate's capacity line (450 A @ 60%) is a welding-current "
        "rating on a blasting chamber; the client has been asked (trap list I2, Q15).",
    ("CRN-01", "capacity_raw"): "The plate's capacity line is a welder's rating, not a crane "
        "load. For crane loads cite SOP-CRANE-02 (find_sop) (trap list I19, D7).",
    ("CRN-02", "capacity_raw"): "OCR stored 2600 kg; checked by eye, the photo reads 2000 kg, "
        "as SOP-CRANE-02 says. For crane loads cite SOP-CRANE-02 (find_sop) (trap list I15, D7).",
    # Values checked by eye against the photo (trap list I15). The stored value is the OCR
    # tie-break and is NOT changed here; the answer gives both. The lasting fix is a
    # reviewed-corrections table at ingest, so the value itself carries its provenance.
    ("CNC-03", "grease_interval_h"): "OCR stored 590 h; checked by eye, the photo reads 500 h "
        "(trap list I15). Give 500 h as the photo's value and say how it was checked.",
    ("CNC-03", "weight_kg"): "OCR stored 8892 kg; checked by eye, the photo reads 8092 kg "
        "(trap list I15).",
    ("CRN-02", "grease_interval_h"): "OCR stored 509 h; checked by eye, the photo reads 500 h "
        "(trap list I15). Give 500 h as the photo's value and say how it was checked.",
    ("PMP-T01", "capacity_raw"): "The plate's capacity line is a welder's rating on a pump "
        "(trap list I19).",
    ("WELD-02", "capacity_raw"): "The plate's capacity line is a compressor's air delivery on a "
        "welder (trap list I19).",
    ("WELD-02", "weight_kg"): "The plate's weight is a compressor's, not a portable welder's "
        "(trap list I19).",
    ("BLAST-01", "lubricant"): "The plate names a slideway oil for a blasting chamber; the "
        "client has been asked to confirm (trap list I3, I20, Q15-Q16).",
    ("CMP-01", "lubricant"): "The plate names a slideway oil; screw compressors normally take "
        "the maker's compressor fluid. Client asked to confirm (trap list I20, Q16).",
    ("WELD-01", "lubricant"): "Inverter welders usually have no lubrication points; client "
        "asked to confirm (trap list I20, Q16).",
    ("WELD-02", "lubricant"): "Inverter welders usually have no lubrication points; client "
        "asked to confirm (trap list I20, Q16).",
}


def field_flags(warnings: str | None) -> dict[str, list[str]]:
    """img_nameplates warnings ('ocr_disagreed_lubricant,lubricant_without_grade,...') ->
    {field: [flags]}. Flags it can't tie to a field go under '_other'."""
    out: dict[str, list[str]] = {}
    for w in [x.strip() for x in str(warnings or "").split(",") if x.strip()]:
        target = _WHOLE_FLAGS.get(w)
        if not target:
            for prefix in ("ocr_no_majority_", "ocr_disagreed_", "failed_format_", "bad_"):
                if w.startswith(prefix):
                    target = _FLAG_FIELD.get(w[len(prefix):])
                    break
        out.setdefault(target or "_other", []).append(w)
    return out


def confidence(flags: list[str]) -> str:
    """unreadable: the value failed its check, don't quote it as fact. uncertain: no two
    OCR passes agreed, the stored value is a tie-break. read_by_majority: passes differed
    but two agreed. clear: no flag."""
    if any(f.startswith(_UNREADABLE) or f == "lubricant_without_grade" for f in flags):
        return "unreadable"
    if any(f.startswith("ocr_no_majority_") for f in flags):
        return "uncertain"
    if any(f.startswith("ocr_disagreed_") for f in flags):
        return "read_by_majority"
    return "clear"


class MachineArgs(Args):
    machine: str = Field(..., max_length=60,
                         description="Machine id or name as the user said it, e.g. 'CNC-02', "
                                     "'cnc 2', 'the compressor'.")


def _nameplate(db: Db, mid: str) -> tuple[dict | None, dict]:
    rows = db.query(
        "SELECT n.*, a.source_file, a.image_sha256, a.warnings FROM machine_nameplate n "
        "LEFT JOIN img_nameplates_audit a ON a.raw_id = n.raw_id WHERE n.machine_id = :m",
        {"m": mid})
    if not rows or not rows[0].get("has_nameplate"):
        return None, {}
    r = rows[0]
    flags = field_flags(r.get("warnings"))
    fields = {}
    for f in NAMEPLATE_FIELDS:
        if f not in r:
            continue
        fl = flags.get(f, [])
        entry = {"value": r[f], "confidence": confidence(fl) if r[f] is not None else "not_read"}
        if fl:
            entry["ocr_flags"] = fl
        if (mid, f) in KNOWN_CONCERNS:
            entry["concern"] = KNOWN_CONCERNS[(mid, f)]
        fields[f] = entry
    return r, fields


@tool("get_nameplate", """What a machine's nameplate photo says, field by field (maker,
model, serial, manufacture month, power, voltage, capacity, weight, lubricant, grease
interval), each with an OCR confidence: clear, read_by_majority, uncertain (no two OCR
passes agreed) or unreadable. Quote an uncertain or unreadable value only as 'the photo
reads ..., not certain' and cite the image.""", MachineArgs)
def get_nameplate(db: Db, a: MachineArgs) -> ToolResult:
    mid = machine_arg(db, a.machine)
    r, fields = _nameplate(db, mid)
    if r is None:
        return ToolResult("get_nameplate", True, data={"machine_id": mid, "has_nameplate": False},
                          notes=[f"There is no nameplate photo for {mid} in the data."],
                          sources=[cite("table", "machine_nameplate")])
    notes = [f"{f}: {e['concern']}" for f, e in fields.items() if "concern" in e]
    shaky = [f for f, e in fields.items() if e["confidence"] in ("uncertain", "unreadable")]
    if shaky:
        notes.append("Not reliably read from the photo: " + ", ".join(shaky)
                     + ". Say so and cite the image; don't state these as fact.")
    if r.get("model_agrees") == 0:
        notes.append(f"The plate's model ({r.get('plate_model')}) differs from the CMMS model "
                     f"({r.get('logged_model')}).")
    data = {"machine_id": mid, "machine_name": r.get("machine_name"), "has_nameplate": True,
            "cmms_model": r.get("logged_model"), "model_agrees": r.get("model_agrees"),
            "fields": fields}
    return ToolResult("get_nameplate", True, data=data, notes=notes, sources=[
        cite("image", "img_nameplates_audit", file=r.get("source_file"),
             sha256=r.get("image_sha256"), what=f"{mid} nameplate photo")])


# ================================================================= spares ===

SPARE_SQL = """
SELECT s.machine_id, s.part_no, s.description, s.qty, s.min_qty, s.below_min, s.unit_cost,
       s.cost_per, s.size_mm, s.lead_time_min_days, s.lead_time_max_days, s.is_local_supply,
       s.location, s.listed_for, a.source_file, a.source_row, a.source_rev, a.file_sha256,
       a.lead_time_raw, a.unit_cost_raw, a.qty_raw
FROM machine_spares s JOIN critical_spares_audit a ON a.part_no = s.part_no
"""


def _spare_row(r: dict) -> dict:
    size = r.get("size_mm")
    lead = ("local supply" if r.get("is_local_supply") else
            None if pd.isna(r.get("lead_time_max_days")) else
            f"{int(r['lead_time_min_days'])}-{int(r['lead_time_max_days'])} days")
    return {"part_no": r["part_no"], "description": r["description"],
            "listed_for": r.get("listed_for"), "qty": r["qty"], "min_qty": r["min_qty"],
            "below_min": bool(r["below_min"]),
            "unit_cost": r.get("unit_cost"), "cost_per": r.get("cost_per"),
            "size_mm": size, "size_in": None if size is None else round(float(size) / 25.4, 3),
            "lead_time": lead, "lead_time_as_written": r.get("lead_time_raw"),
            "location": r.get("location"), "sheet_row": r.get("source_row")}


class SparesArgs(Args):
    machine: str | None = Field(None, max_length=60, description="Machine id or name.")
    part_no: str | None = Field(None, max_length=40, description="Part number, e.g. BRG-6310.")
    below_min_only: bool = Field(False, description="Only parts below minimum stock.")


@tool("get_spares", """Critical spares from the storeman's sheet (critical_spares.xlsx):
for a machine, a part number, or every part below minimum stock. Gives quantity vs
minimum, lead time (as days and as written), location, size in mm and inches. A part
listed for ALL is shop-wide stock (e.g. hydraulic oil).""", SparesArgs)
def get_spares(db: Db, a: SparesArgs) -> ToolResult:
    mid = machine_arg(db, a.machine)
    where, params = [], {}
    if mid:
        where.append("s.machine_id = :m")
        params["m"] = mid
    if a.part_no:
        where.append("LOWER(s.part_no) = :p")
        params["p"] = a.part_no.strip().lower()
    if a.below_min_only:
        where.append("s.below_min = 1")
    sql = SPARE_SQL + (" WHERE " + " AND ".join(where) if where else "") \
        + " ORDER BY s.below_min DESC, s.part_no"
    rows = db.query(sql, params)
    if not mid:            # one row per part, not per machine it's listed for
        seen, uniq = set(), []
        for r in rows:
            if r["part_no"] not in seen:
                seen.add(r["part_no"])
                uniq.append(r)
        rows = uniq
    notes = ["Costs are as written on the sheet with no currency stated (trap list S2)."]
    if mid and rows and all(r.get("listed_for") == "ALL" for r in rows):
        notes.append(f"The sheet lists no spares specifically for {mid}, only shop-wide (ALL) "
                     f"items.")
    if not rows:
        what = (f"part {a.part_no}" if a.part_no else f"{mid}" if mid else "this filter")
        notes = [f"The critical spares sheet has nothing for {what}."]
    if any(str(r["part_no"]).upper().startswith("BELT-") for r in rows):
        notes.append("Belt part numbers give length in tenths of an inch (BELT-5V-850 = 85 in), "
                     "not mm; size_mm is from the description (trap list S5).")
    if any(r.get("listed_for") == "ALL" for r in rows):
        notes.append("Quantities are in each item's unit of issue (e.g. drums for oil, spools "
                     "for wire), not pieces (trap list S7).")
    df = pd.DataFrame(rows)
    return ToolResult("get_spares", True,
                      data={"machine_id": mid, "parts": [_spare_row(r) for r in rows[:MAX_ROWS]],
                            "count": len(rows),
                            "below_min": sum(1 for r in rows if r["below_min"])},
                      notes=notes,
                      sources=cite_rows(df, "critical_spares_audit", "file", "source_row", "row",
                                        what="Critical spares sheet"))


# ============================================================= lubrication ===

_OIL = re.compile(r"\b(oil|grease|lubric\w*|hydraulic|tonna|iso\s*vg)\b", re.I)
_GRADE = re.compile(r"(?:VG|M)\s*(\d{2,3})\b", re.I)


@tool("machine_lubrication", """What oil a machine takes and its grease interval, from both
sources that say anything about it: the nameplate photo and the critical spares sheet
(no SOP mentions oil). Says whether they agree, which to trust and why, and flags values
the OCR couldn't read reliably. Use this for 'what oil does X take' / 'grease interval'.""",
      MachineArgs)
def machine_lubrication(db: Db, a: MachineArgs) -> ToolResult:
    mid = machine_arg(db, a.machine)
    r, fields = _nameplate(db, mid)
    spares = [x for x in db.query(SPARE_SQL + " WHERE s.machine_id = :m ORDER BY s.part_no",
                                  {"m": mid}) if _OIL.search(str(x["description"]))]
    plate_oil = fields.get("lubricant") if r else None
    plate_grease = fields.get("grease_interval_h") if r else None
    notes, sources = [], []
    if r is None:
        notes.append(f"There is no nameplate photo for {mid}, so no machine-specific oil or "
                     f"grease interval is in the data.")
    else:
        sources.append(cite("image", "img_nameplates_audit", file=r.get("source_file"),
                            sha256=r.get("image_sha256"), what=f"{mid} nameplate photo"))
        for label, f in (("oil", plate_oil), ("grease interval", plate_grease)):
            if not f or f["value"] is None:
                notes.append(f"The nameplate gives no readable {label}.")
            elif f["confidence"] in ("uncertain", "unreadable"):
                notes.append(f"The nameplate's {label} ({f['value']}) was not reliably read "
                             f"({f['confidence']}): say so and point to the photo.")
            if f and f.get("concern"):
                notes.append(f["concern"])
    if spares:
        sources += cite_rows(pd.DataFrame(spares), "critical_spares_audit", "file", "source_row",
                             "row", what="Critical spares sheet")
    # Only spares listed for THIS machine can contradict its plate. A shop-wide (ALL) item,
    # e.g. hydraulic oil, is usually for a different system on the machine (5 Nov 2026:
    # CNC-03's plate VG68 was reported as 'disagreeing' with the shop's VG32 hydraulic oil).
    own = [x for x in spares if x.get("listed_for") != "ALL"]
    shop = [x for x in spares if x.get("listed_for") == "ALL"]
    plate_grades = set(_GRADE.findall(str(plate_oil["value"]))) if plate_oil and plate_oil["value"] else set()
    spare_grades = {g for x in own for g in _GRADE.findall(str(x["description"]))}
    agree = None if not (plate_grades and spare_grades) else bool(plate_grades & spare_grades)
    if agree is False:
        notes.append("The nameplate and the spares listed for this machine name different oil "
                     "grades: give both with their sources.")
    if shop:
        notes.append("The spares sheet's oil for this machine is shop-wide stock (listed for "
                     "ALL): " + ", ".join(f"{x['part_no']} {x['description']}" for x in shop)
                     + ". It isn't this machine's specification and doesn't contradict the "
                       "plate; it may be for another system, e.g. hydraulics.")
    trust = ("The nameplate is the machine's own specification and is trusted when it was "
             "read clearly. The spares sheet lists stock; an item for ALL machines is shop-wide "
             "stock, not this machine's specification. If the plate wasn't read clearly, neither "
             "source settles it: check the photo.")
    notes.append("No SOP in the data mentions oil or grease intervals (trap list D10).")
    return ToolResult("machine_lubrication", True, data={
        "machine_id": mid,
        "nameplate": None if r is None else {"lubricant": plate_oil,
                                             "grease_interval_h": plate_grease},
        "spares_sheet": [_spare_row(x) for x in spares],
        "grades_agree": agree, "which_to_trust": trust}, notes=notes, sources=sources)


# =============================================================== incidents ===

Scale = Literal["5_is_worst", "1_is_worst"]


class IncidentFilter(Args):
    machine: str | None = Field(None, max_length=60, description="Machine id or name.")
    date_from: date | None = Field(None, description="First incident date to include.")
    date_to: date | None = Field(None, description="Last incident date to include.")
    description_contains: str | None = Field(
        None, max_length=60, description="Words in the incident description, e.g. 'sling', "
                                         "'confined space', 'near miss'.")
    at_least_as_severe_as: int | None = Field(
        None, ge=1, le=5, description="Only incidents at least this severe, ON THE SCALE GIVEN "
                                      "IN severity_scale.")
    exact_severity: int | None = Field(None, ge=1, le=5,
                                       description="Only this severity, on severity_scale.")
    severity_scale: Scale | None = Field(
        None, description="REQUIRED with any severity number: the scale the USER's number is "
                          "on. 5_is_worst = the current scale (since Jul 2025); 1_is_worst = "
                          "the old one. If the user didn't say which, leave it out: the tool "
                          "will ask them. Never assume.")


def to_5_worst(value: int, scale: str) -> int:
    return value if scale == "5_is_worst" else 6 - value


INCIDENT_SQL = """
SELECT i.incident_id, i.incident_date, i.machine_id, i.machine_name, i.description,
       i.severity_5_worst, i.severity_scale, i.lost_days, i.reporting,
       a.severity_as_logged, a.source_file, a.source_line, a.file_sha256
FROM incident_detail i JOIN cmms_safety_incidents_audit a ON a.raw_id = i.raw_id
"""
INCIDENT_COLUMNS = ["incident_id", "incident_date", "machine_id", "machine_name", "description",
                    "severity_5_worst", "severity_scale", "lost_days", "reporting",
                    "severity_as_logged", "source_file", "source_line", "file_sha256"]
SEVERITY_RULE = ("Severities are on the current scale, 5 = most severe. Incidents before "
                 "1 Jul 2025 were logged on the old scale (1 = most severe) and converted at "
                 "ingest as 6 - logged value (trap list C1).")


def _incidents(db: Db, f: IncidentFilter) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(matching, unplaced, applied filter). unplaced: incidents that pass every other filter
    but whose severity can't be placed on one scale (July 2025, trap list C2), when a
    severity filter is used."""
    sev = f.at_least_as_severe_as if f.at_least_as_severe_as is not None else f.exact_severity
    if f.at_least_as_severe_as is not None and f.exact_severity is not None:
        raise ToolError("bad_arguments", "Give at_least_as_severe_as or exact_severity, not both.")
    if sev is not None and f.severity_scale is None:
        raise ToolError("needs_clarification",
                        f"Severity {sev} means opposite things on the two scales. Ask the user: "
                        f"the current scale (5 = most severe, used since Jul 2025) or the old "
                        f"one (1 = most severe)?")
    mid = machine_arg(db, f.machine)
    where, params = [], {}
    if mid:
        where.append("i.machine_id = :m")
        params["m"] = mid
    where += _between("i.incident_date", f.date_from, f.date_to, params)
    if f.description_contains:
        where.append("LOWER(i.description) LIKE :d ESCAPE '!'")
        params["d"] = _like(f.description_contains)
    df = db.frame(INCIDENT_SQL + (" WHERE " + " AND ".join(where) if where else "")
                  + " ORDER BY i.incident_date, i.incident_id", params, columns=INCIDENT_COLUMNS)
    applied = {"machine_id": mid, "date_from": f.date_from, "date_to": f.date_to,
               "description_contains": f.description_contains}
    unplaced = df.iloc[0:0]
    if len(df):
        df["incident_date"] = pd.to_datetime(df["incident_date"]).dt.date
        df["severity_5_worst"] = pd.to_numeric(df["severity_5_worst"], errors="coerce")
        df["lost_days"] = pd.to_numeric(df["lost_days"], errors="coerce")
    if sev is not None:
        t = to_5_worst(sev, f.severity_scale)
        applied["severity_5_worst"] = (f">= {t}" if f.at_least_as_severe_as is not None
                                       else f"= {t}")
        applied["severity_as_asked"] = f"{sev} on the {f.severity_scale} scale"
        if len(df):
            unplaced = df[df["severity_5_worst"].isna()]
            known = df.dropna(subset=["severity_5_worst"])
            df = known[known["severity_5_worst"] >= t] if f.at_least_as_severe_as is not None \
                else known[known["severity_5_worst"] == t]
    return df, unplaced, applied


def _logged(r) -> str:
    v = r.get("severity_as_logged")
    if v is None or pd.isna(v):
        return "not recorded"
    old = r["incident_date"] < SCALE_SWITCH
    return f"{int(v)} on the {'old (1 = most severe)' if old else 'current (5 = most severe)'} scale"


def _incident_row(r: dict) -> dict:
    s = r.get("severity_5_worst")
    return {"incident_id": int(r["incident_id"]), "incident_date": r["incident_date"],
            "machine_id": r["machine_id"], "description": r["description"],
            "severity_5_worst": None if s is None or pd.isna(s) else int(s),
            "severity_as_logged": _logged(r), "lost_days": r.get("lost_days"),
            "reporting": r.get("reporting"), "dump_line": r.get("source_line")}


GroupBy = Literal["machine", "year", "month", "severity", "description", "scale_period"]


class CountArgs(IncidentFilter):
    group_by: GroupBy | None = Field(None, description="Break the count down by this.")


def _group_key(df: pd.DataFrame, g: str) -> pd.Series:
    d = pd.to_datetime(df["incident_date"])
    return {"machine": df["machine_id"], "year": d.dt.year.astype(str),
            "month": d.dt.strftime("%Y-%m"),
            "severity": df["severity_5_worst"].map(lambda v: "unplaced" if pd.isna(v)
                                                   else str(int(v))),
            "description": df["description"],
            "scale_period": df["incident_date"].map(
                lambda x: "before 1 Jul 2025 (logged 1 = worst, converted)" if x < SCALE_SWITCH
                else "from 1 Jul 2025 (logged 5 = worst)")}[g]


@tool("count_incidents", """Count safety incidents from the CMMS, on ONE severity scale
(5 = most severe; old records converted). Filter by machine, dates, description words and
severity; group by machine, year, month, severity, description or scale_period. Use for
'how many', 'which machine has most', 'trend'. Incidents whose severity can't be placed
(July 2025, the switch month) are reported separately, never silently counted.""", CountArgs)
def count_incidents(db: Db, a: CountArgs) -> ToolResult:
    df, unplaced, applied = _incidents(db, a)
    data = {"count": len(df), "filter": applied, "severity_rule": SEVERITY_RULE,
            "lost_days_total": int(df["lost_days"].sum()) if len(df) else 0}
    if a.group_by and len(df):
        g = df.assign(_k=_group_key(df, a.group_by)).groupby("_k")
        data["groups"] = [{"key": k, "count": len(x), "lost_days": int(x["lost_days"].sum())}
                          for k, x in g]
        data["groups"].sort(key=lambda x: (-x["count"], str(x["key"])))
    if len(df) <= MAX_ROWS:
        data["incident_ids"] = [int(x) for x in df["incident_id"]]
    notes = ["lost_days only ever takes 0, 1, 2, 5 or 14: it may be a coded band, so treat "
             "totals with care (trap list C4)."]
    if len(unplaced):
        data["unplaced"] = [{"incident_id": int(r["incident_id"]), "incident_date": r["incident_date"],
                             "severity_as_logged": _logged(r)}
                            for r in unplaced.to_dict("records")]
        notes.append(f"{len(unplaced)} incident(s) in July 2025 can't be placed on one scale "
                     f"(the exact switch day is unconfirmed, trap list C2), so they are not in "
                     f"the count: mention them.")
    if a.machine:
        notes.append("Some incident descriptions don't fit the machine they're logged on "
                     "(trap list C6); quote them, don't reason from them.")
    return ToolResult("count_incidents", True, data=data, notes=notes,
                      sources=cite_rows(pd.concat([df, unplaced]), "cmms_safety_incidents_audit",
                                        "file", "source_line", "line", what="CMMS incidents"))


class ListIncidentArgs(IncidentFilter):
    newest_first: bool = Field(True, description="Order by date, newest first.")


@tool("list_incidents", """The incidents behind a count (up to 50), each with date,
machine, description, severity on the 5 = most severe scale AND as logged, lost days,
and its line in the CMMS dump. Same filters as count_incidents. Order is by incident
date (incident ids are not in date order).""", ListIncidentArgs)
def list_incidents(db: Db, a: ListIncidentArgs) -> ToolResult:
    df, unplaced, applied = _incidents(db, a)
    df = df.sort_values(["incident_date", "incident_id"], ascending=not a.newest_first)
    rows = [_incident_row(r) for r in df.head(MAX_ROWS).to_dict("records")]
    notes = []
    if len(df) > MAX_ROWS:
        notes.append(f"{len(df)} incidents match; the first {MAX_ROWS} are listed. Use "
                     f"count_incidents for totals.")
    if len(unplaced):
        notes.append(f"Not listed: {len(unplaced)} July 2025 incident(s) whose severity can't be "
                     f"placed on one scale ({', '.join(str(int(x)) for x in unplaced['incident_id'])}).")
    if not len(df):
        notes.append("No incidents in the CMMS match.")
    return ToolResult("list_incidents", True,
                      data={"count": len(df), "filter": applied, "severity_rule": SEVERITY_RULE,
                            "incidents": rows},
                      notes=notes,
                      sources=cite_rows(df.head(MAX_ROWS), "cmms_safety_incidents_audit", "file",
                                        "source_line", "line", what="CMMS incidents"))


class IncidentArgs(Args):
    incident_id: int = Field(..., ge=1, description="CMMS incident id, e.g. 914 (form IR-2025-914).")


@tool("get_incident", """One incident in full: the CMMS record and, if there is one, its
scanned incident form (immediate action, root cause, signature), with every field compared
form vs CMMS. Use it for 'what happened in incident X' or to check a record against
paper.""", IncidentArgs)
def get_incident(db: Db, a: IncidentArgs) -> ToolResult:
    rows = db.query(INCIDENT_SQL + " WHERE i.incident_id = :id", {"id": a.incident_id})
    if not rows:
        raise ToolError("no_data", f"There is no incident {a.incident_id} in the CMMS.")
    r = rows[0]
    r["incident_date"] = pd.to_datetime(r["incident_date"]).date()
    data = {"cmms": _incident_row(r), "severity_rule": SEVERITY_RULE}
    sources = [cite("file", "cmms_safety_incidents_audit", file=r["source_file"],
                    locator=f"line {r['source_line']}", sha256=r.get("file_sha256"),
                    what=f"CMMS incident {a.incident_id}")]
    notes = []
    form = db.query("SELECT report_no, source_file, image_sha256, severity_scale, "
                    "immediate_action, root_cause, signature_present, warnings "
                    "FROM img_incident_reports_audit WHERE incident_id = :id", {"id": a.incident_id})
    if form:
        f = form[0]
        compare = db.query("SELECT field, form_value, logged_value, agrees "
                           "FROM incident_report_vs_cmms WHERE incident_id = :id ORDER BY field",
                           {"id": a.incident_id})
        data["scanned_form"] = {
            "report_no": f["report_no"], "scale_printed": f["severity_scale"],
            "immediate_action": f["immediate_action"], "root_cause": f["root_cause"],
            "signed": bool(f["signature_present"]),
            "form_vs_cmms": compare}
        sources.append(cite("image", "img_incident_reports_audit", file=f["source_file"],
                            sha256=f.get("image_sha256"), what=f"Scanned form {f['report_no']}"))
        differ = [c["field"] for c in compare if c["agrees"] == 0]
        if differ:
            notes.append(f"The form and the CMMS disagree on: {', '.join(differ)}. Give both "
                         f"values with their sources.")
        unread = [c["field"] for c in compare if c["agrees"] is None and c["form_value"] is None]
        if unread:
            notes.append(f"Not readable on the scan: {', '.join(unread)}.")
        if f["root_cause"] and "attach" in str(f["root_cause"]).lower():
            notes.append("The root cause is in a 5-Why attachment that isn't in the data "
                         "(trap list I7).")
    else:
        notes.append(f"There is no scanned form for incident {a.incident_id} in the data.")
    if r["incident_date"].year == 2025 and r["incident_date"].month == 7 and \
            data["cmms"]["severity_5_worst"] is None:
        notes.append("Logged in July 2025, the scale switch month: its severity can't be "
                     "placed on one scale (trap list C2).")
    return ToolResult("get_incident", True, data=data, notes=notes, sources=sources)


# ============================================================= work orders ===

class WorkOrderArgs(Args):
    machine: str | None = Field(None, max_length=60, description="Machine id or name.")
    date_from: date | None = None
    date_to: date | None = None
    wo_type: Literal["any", "preventive", "corrective", "breakdown"] = "any"
    status: Literal["any", "open", "closed"] = Field(
        "any", description="open = anything not closed (OPEN, AWAITING PARTS).")
    description_contains: str | None = Field(None, max_length=60)


_TYPES = {"preventive": {"pm", "preventive"}, "corrective": {"corrective"},
          "breakdown": {"breakdown"}}


@tool("list_work_orders", """Work orders from the CMMS for a machine and/or period: counts
by type, hours, costs (closed and not-closed kept apart, because open ones carry
estimates), possible double entries, and the newest 50 with their dump lines.""",
      WorkOrderArgs)
def list_work_orders(db: Db, a: WorkOrderArgs) -> ToolResult:
    mid = machine_arg(db, a.machine)
    where, params = [], {}
    if mid:
        where.append("w.machine_id = :m")
        params["m"] = mid
    where += _between("w.wo_date", a.date_from, a.date_to, params)
    if a.description_contains:
        where.append("LOWER(w.description) LIKE :d ESCAPE '!'")
        params["d"] = _like(a.description_contains)
    df = db.frame(
        "SELECT w.wo_id, w.wo_date, w.machine_id, w.wo_type, w.description, w.duration_hrs, "
        "w.cost_sgd, w.status, a.source_file, a.source_line, a.file_sha256 "
        "FROM work_order_detail w JOIN cmms_work_orders_audit a ON a.raw_id = w.raw_id"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY w.wo_date, w.wo_id",
        params, columns=["wo_id", "wo_date", "machine_id", "wo_type", "description", "duration_hrs",
                         "cost_sgd", "status", "source_file", "source_line", "file_sha256"])
    if len(df):
        t = df["wo_type"].astype(str).str.strip().str.lower()
        closed = df["status"].astype(str).str.strip().str.lower() == "closed"
        keep = pd.Series(True, index=df.index)
        if a.wo_type != "any":
            keep &= t.isin(_TYPES[a.wo_type])
        if a.status != "any":
            keep &= closed if a.status == "closed" else ~closed
        df = df[keep].copy()
        for c in ("duration_hrs", "cost_sgd"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    notes = []
    data = {"machine_id": mid, "count": len(df)}
    if len(df):
        closed = df["status"].astype(str).str.strip().str.lower() == "closed"
        data.update({
            "by_type": df["wo_type"].astype(str).value_counts().to_dict(),
            "by_status": df["status"].astype(str).value_counts().to_dict(),
            "hours_total": round(float(df["duration_hrs"].sum()), 1),
            "cost_closed_sgd": round(float(df.loc[closed, "cost_sgd"].sum()), 2),
            "cost_not_closed_sgd": round(float(df.loc[~closed, "cost_sgd"].sum()), 2)})
        dup = df.groupby(["machine_id", "wo_date", df["wo_type"].astype(str).str.lower()])
        pairs = [list(map(int, g["wo_id"])) for _, g in dup if len(g) > 1]
        if pairs:
            data["possible_double_entries"] = pairs
            notes.append("Same machine, day and type more than once: possibly double-entered "
                         "(trap list C9). Counts and costs include them.")
        if (~closed).any():
            notes.append("Work orders that aren't closed already carry hours and costs; treat "
                         "those as estimates (trap list C10).")
        newest = df.sort_values(["wo_date", "wo_id"], ascending=False).head(MAX_ROWS)
        data["work_orders"] = [{"wo_id": int(r["wo_id"]), "wo_date": r["wo_date"],
                                "machine_id": r["machine_id"], "wo_type": r["wo_type"],
                                "description": r["description"], "hours": r["duration_hrs"],
                                "cost_sgd": r["cost_sgd"], "status": r["status"],
                                "dump_line": r["source_line"]} for r in newest.to_dict("records")]
        if len(df) > MAX_ROWS:
            notes.append(f"Newest {MAX_ROWS} of {len(df)} listed; the totals cover all of them.")
        notes.append("Work orders record no parts used (trap list C11).")
    else:
        notes.append("No work orders in the CMMS match.")
    return ToolResult("list_work_orders", True, data=data, notes=notes,
                      sources=cite_rows(df, "cmms_work_orders_audit", "file", "source_line",
                                        "line", what="CMMS work orders"))


# ================================================================= sensors ===

DAILY_SQL = """
SELECT DATE(sampled_at) AS day, COUNT(*) AS readings,
       AVG(vib_rms_mms) AS vib_mean, MAX(vib_rms_mms) AS vib_max,
       AVG(temp_c) AS temp_mean, MAX(temp_c) AS temp_max
FROM vibration_temp_audit
WHERE machine_id = :m AND sampled_at >= :a AND sampled_at < :b
GROUP BY DATE(sampled_at) ORDER BY day
"""
SENSOR_SOURCE_SQL = """
SELECT source_file, file_sha256, COUNT(*) AS readings, MIN(source_line) AS first_line,
       MAX(source_line) AS last_line
FROM vibration_temp_audit
WHERE machine_id = :m AND sampled_at >= :a AND sampled_at < :b
GROUP BY source_file, file_sha256
"""


def sensored(db: Db) -> dict[str, tuple[date, date]]:
    """machine_id -> (first, last reading day) for machines with sensor readings."""
    out = {}
    for m in machines(db):
        if m.get("sensor_readings"):
            out[m["machine_id"]] = (pd.to_datetime(m["first_reading"]).date(),
                                    pd.to_datetime(m["last_reading"]).date())
    return out


def _sensor_machine(db: Db, text_: str) -> tuple[str, tuple[date, date]]:
    mid = machine_arg(db, text_)
    have = sensored(db)
    if mid not in have:
        raise ToolError("no_data", f"There are no sensor readings for {mid} in the data. "
                                   f"Sensors cover only {', '.join(sorted(have))} (trap list L5).")
    return mid, have[mid]


def daily(db: Db, mid: str, start: date, end: date) -> pd.DataFrame:
    """Daily aggregates for [start, end] inclusive, computed in SQL (one row per day)."""
    df = db.frame(DAILY_SQL, {"m": mid, "a": f"{start.isoformat()} 00:00:00",
                              "b": f"{(end + timedelta(days=1)).isoformat()} 00:00:00"},
                  columns=["day", "readings", "vib_mean", "vib_max", "temp_mean", "temp_max"])
    if len(df):
        df["day"] = pd.to_datetime(df["day"])
        for c in ("readings", "vib_mean", "vib_max", "temp_mean", "temp_max"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def sensor_sources(db: Db, mid: str, start: date, end: date) -> list[dict]:
    rows = db.query(SENSOR_SOURCE_SQL, {"m": mid, "a": f"{start.isoformat()} 00:00:00",
                                        "b": f"{(end + timedelta(days=1)).isoformat()} 00:00:00"})
    return [cite("file", "vibration_temp_audit", file=r["source_file"],
                 locator=f"{int(r['readings'])} readings, lines {r['first_line']}-{r['last_line']}",
                 sha256=r["file_sha256"], what=f"{mid} sensor log") for r in rows]


def rollup(d: pd.DataFrame, bucket: str, start: date, end: date) -> pd.DataFrame:
    """Daily rows -> day/week/month rows, means weighted by readings, with coverage against
    the 12-minute grid (trap list L4)."""
    freq = {"day": "D", "week": "W-SUN", "month": "M"}[bucket]   # W-SUN: weeks Mon-Sun
    key = d["day"].dt.to_period(freq).dt.start_time
    d = d.assign(_k=key, _v=d["vib_mean"] * d["readings"], _t=d["temp_mean"] * d["readings"])
    g = d.groupby("_k").agg(readings=("readings", "sum"), _v=("_v", "sum"), _t=("_t", "sum"),
                            vib_max=("vib_max", "max"), temp_max=("temp_max", "max"))
    g["vib_mean"] = g["_v"] / g["readings"]
    g["temp_mean"] = g["_t"] / g["readings"]
    grid = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1),
                         freq=f"{READING_MINUTES}min", inclusive="left")
    expected = pd.Series(1, index=grid).groupby(grid.to_period(freq).start_time).sum()
    g["expected"] = expected.reindex(g.index).fillna(0).astype(int)
    g["coverage_pct"] = (100 * g["readings"] / g["expected"].where(g["expected"] > 0)).round(1)
    out = g.reset_index().rename(columns={"_k": "period_start"})
    for c in ("vib_mean", "vib_max", "temp_mean", "temp_max"):
        out[c] = out[c].round(2)
    return out[["period_start", "readings", "expected", "coverage_pct", "vib_mean", "vib_max",
                "temp_mean", "temp_max"]]


class TrendArgs(Args):
    machine: str = Field(..., max_length=60, description="Machine id or name.")
    date_from: date | None = Field(None, description="Default: the first reading.")
    date_to: date | None = Field(None, description="Default: the last reading.")
    bucket: Literal["day", "week", "month"] = "week"


SENSOR_NOTES = [
    "Vibration is RMS in mm/s; temperature in °C (Fahrenheit readings were converted at "
    "ingest, trap list L2).",
    "Single readings are noisy (about ±6 °C reading to reading): use the means, not one "
    "reading (trap list L6).",
]


@tool("sensor_trend", """Vibration (mm/s RMS) and temperature (°C) for one machine, per
day, week or month: mean, max, readings and coverage of the 12-minute schedule. Only
CMP-01, CNC-01, CNC-02, CNC-03 and PMP-T01 have sensors; data covers Jan-Jun 2026.""",
      TrendArgs)
def sensor_trend(db: Db, a: TrendArgs) -> ToolResult:
    mid, (first, last) = _sensor_machine(db, a.machine)
    start, end = max(a.date_from or first, first), min(a.date_to or last, last)
    if start > end:
        raise ToolError("no_data", f"{mid}'s readings run from {first} to {last}; nothing in "
                                   f"the period asked.")
    if a.bucket == "day" and (end - start).days > 120:
        raise ToolError("bad_arguments", "More than 120 days: use bucket 'week' or 'month'.")
    d = daily(db, mid, start, end)
    if not len(d):
        raise ToolError("no_data", f"No readings for {mid} between {start} and {end}.")
    r = rollup(d, a.bucket, start, end)
    notes = list(SENSOR_NOTES)
    low = r[r["coverage_pct"] < 90]
    if len(low):
        notes.append(f"{len(low)} {a.bucket}(s) have under 90% of the expected readings.")
    return ToolResult("sensor_trend", True,
                      data={"machine_id": mid, "from": start, "to": end, "bucket": a.bucket,
                            "series": r.to_dict("records")},
                      notes=notes, sources=sensor_sources(db, mid, start, end))


class CompareArgs(Args):
    machine: str = Field(..., max_length=60, description="Machine id or name, e.g. CNC-02.")
    baseline_from: date | None = Field(None, description="Default: first reading.")
    baseline_to: date | None = Field(None, description="Default: 90 days after the first reading.")
    recent_from: date | None = Field(None, description="Default: 30 days before the last reading.")
    recent_to: date | None = Field(None, description="Default: last reading.")


ELEVATED_SD = 3.0
# The smallest rise that counts, per metric. Vibration RMS has a true zero, so a relative
# rise means something. Temperature in °C doesn't (0 °C isn't "no heat"), so its minimum
# is absolute: a 10% rule missed CNC-02's +4.0 °C (7.8%, 8 sd) on the real data, 5 Nov 2026.
MIN_RISE = {"vib": ("pct", 10.0), "temp": ("abs", 2.0)}
VERDICT_RULE = (f"A metric is 'elevated' when the recent window's mean is at least "
                f"{ELEVATED_SD:g} standard deviations (of the baseline's daily means) above "
                f"the baseline mean, and the rise is at least {MIN_RISE['vib'][1]:g}% for "
                f"vibration or {MIN_RISE['temp'][1]:g} °C for temperature. The other sensored "
                f"machines are compared over the same windows, to tell a machine problem "
                f"from a shop-wide change.")


def window_stats(d: pd.DataFrame, b0, b1, r0, r1) -> dict:
    out = {}
    for metric in ("vib", "temp"):
        col = f"{metric}_mean"
        base = d[(d["day"] >= pd.Timestamp(b0)) & (d["day"] <= pd.Timestamp(b1))]
        rec = d[(d["day"] >= pd.Timestamp(r0)) & (d["day"] <= pd.Timestamp(r1))]
        if not len(base) or not len(rec):
            out[metric] = None
            continue
        bm = float((base[col] * base["readings"]).sum() / base["readings"].sum())
        rm = float((rec[col] * rec["readings"]).sum() / rec["readings"].sum())
        sd = float(base[col].std(ddof=1)) if len(base) > 1 else float("nan")
        change = rm - bm
        z = change / sd if sd and not math.isnan(sd) else None
        pct = 100 * change / bm if bm else None
        kind, minimum = MIN_RISE[metric]
        big_enough = (pct is not None and pct >= minimum) if kind == "pct" else change >= minimum
        out[metric] = {"baseline_mean": round(bm, 2), "baseline_daily_sd": round(sd, 3),
                       "recent_mean": round(rm, 2), "change": round(change, 2),
                       "change_pct": None if pct is None else round(pct, 1),
                       "change_in_sd": None if z is None else round(z, 1),
                       "baseline_days": len(base), "recent_days": len(rec),
                       "elevated": bool(z is not None and z >= ELEVATED_SD and big_enough)}
    return out


@tool("compare_sensor_windows", """Is a machine running differently now? Compares a recent
window of vibration and temperature with a baseline window, for the machine asked AND every
other machine with sensors, and gives a verdict by a fixed rule (machine_specific,
shop_wide or not_elevated) plus weekly series for a chart. Use it for 'is CNC-02 off' /
'is the night shift right'.""", CompareArgs)
def compare_sensor_windows(db: Db, a: CompareArgs) -> ToolResult:
    mid, (first, last) = _sensor_machine(db, a.machine)
    b0 = a.baseline_from or first
    b1 = a.baseline_to or min(b0 + timedelta(days=89), last)
    r1 = a.recent_to or last
    r0 = a.recent_from or max(r1 - timedelta(days=29), first)
    if b0 > b1 or r0 > r1:
        raise ToolError("bad_arguments", "A window ends before it starts.")
    if r0 <= b1:
        raise ToolError("bad_arguments", "The recent window must start after the baseline ends.")
    span0, span1 = min(b0, r0), max(b1, r1)
    per_day = 24 * 60 // READING_MINUTES
    results, chart, sources = {}, [], []
    for m in sorted(sensored(db)):
        d = daily(db, m, span0, span1)
        if not len(d):
            continue
        results[m] = window_stats(d, b0, b1, r0, r1)
        w = rollup(d, "week", span0, span1)
        # weeks run Mon-Sun, so the first and last can hold only a few days of data
        chart += [{"machine_id": m, "week_start": x["period_start"], "vib_mean": x["vib_mean"],
                   "temp_mean": x["temp_mean"], "days": int(x["expected"]) // per_day,
                   "coverage_pct": x["coverage_pct"]} for x in w.to_dict("records")]
        sources += sensor_sources(db, m, span0, span1)
    target = results.get(mid) or {}
    if not target or any(v is None for v in target.values()):
        raise ToolError("no_data", f"Not enough readings for {mid} in both windows.")
    others = {m: s for m, s in results.items() if m != mid}
    target_up = [k for k, v in target.items() if v and v["elevated"]]
    others_up = sorted(m for m, s in others.items() if any(v and v["elevated"] for v in s.values()))
    verdict = ("not_elevated" if not target_up else
               "shop_wide" if others_up else "machine_specific")
    return ToolResult("compare_sensor_windows", True, data={
        "machine_id": mid, "verdict": verdict, "elevated_metrics": target_up,
        "other_machines_elevated": others_up, "rule": VERDICT_RULE,
        "baseline": {"from": b0, "to": b1}, "recent": {"from": r0, "to": r1},
        "target": target, "others": others,
        "chart_weekly": chart},
        notes=list(SENSOR_NOTES) + ["In chart_weekly, a week with days < 7 is partial (the "
                                    "data starts on a Thursday, 1 Jan 2026): plot it, but don't "
                                    "read it as a full week."],
        sources=sources)


# ==================================================================== SOPs ===

class SopArgs(Args):
    question: str | None = Field(None, max_length=300,
                                 description="The user's question in their own words.")
    machine: str | None = Field(None, max_length=60,
                                description="A machine, if the question names one.")


def _sop_quote(db: Db, doc_no: str, revision: int) -> tuple[str, dict, list[str]]:
    steps = db.query("SELECT step_no, line_no, marker, step_text, refers_to, title, rev_label, "
                     "source_file, file_sha256 FROM sop_steps_audit "
                     "WHERE doc_no = :d AND revision = :r ORDER BY step_no",
                     {"d": doc_no, "r": revision})
    head = db.query("SELECT applies_to FROM sops_audit WHERE doc_no = :d AND revision = :r",
                    {"d": doc_no, "r": revision})
    s0 = steps[0]
    lines = [f"{doc_no} — {s0['title']} ({s0['rev_label'] or 'revision not stated'})"]
    if head and head[0].get("applies_to"):
        lines.append(f"Applies to: {head[0]['applies_to']}")
    lines += [f"{(s['marker'] + ' ') if s['marker'] else ''}{s['step_text']}" for s in steps]
    first, last = min(s["line_no"] for s in steps), max(s["line_no"] for s in steps)
    lines.append(f"[Source: {doc_no}, lines {first}-{last}]")
    src = cite("sop", "sop_steps_audit", file=s0["source_file"], locator=f"lines {first}-{last}",
               sha256=s0["file_sha256"], what=f"{doc_no} {s0['rev_label'] or ''}".strip())
    refs = [f"Step {s['step_no']} refers to the {s['refers_to']}, which isn't in the data."
            for s in steps if s["refers_to"]]
    return "\n".join(lines), src, refs


@tool("find_sop", """The SOP that covers a question, quoted WHOLE with its citation (never
a single step out of its procedure). Search is by keyword over the current SOPs; when
nothing scores high enough the answer is 'no SOP in the data covers this' and you must
not adapt another SOP. With only a machine, lists the SOPs that name it.""", SopArgs)
def find_sop(db: Db, a: SopArgs) -> ToolResult:
    from ingest_sops import MIN_SCORE, search_sops

    if not (a.question or a.machine):
        raise ToolError("bad_arguments", "Give a question, a machine, or both.")
    mid = machine_arg(db, a.machine)
    if not a.question:
        rows = db.query("SELECT doc_no, revision, title, rev_label, linked_by, sop_words, has_sop "
                        "FROM machine_sops WHERE machine_id = :m", {"m": mid})
        named = [r for r in rows if r["has_sop"]]
        if not named:
            return ToolResult("find_sop", True, data={"machine_id": mid, "found": False},
                              notes=[f"No SOP in the data names {mid}. Don't adapt another SOP."],
                              sources=[cite("table", "machine_sops")])
        return ToolResult("find_sop", True, data={"machine_id": mid, "found": True, "sops": named},
                          notes=["Call find_sop with the question to get the SOP quoted whole."],
                          sources=[cite("table", "machine_sops")])
    sops = db.frame("SELECT doc_no, revision, title, full_text, is_current FROM sops "
                    "WHERE is_current = 1")
    links = db.frame("SELECT doc_no, revision, machine_id, how, evidence FROM sop_machines",
                     columns=["doc_no", "revision", "machine_id", "how", "evidence"])
    if not len(sops):
        raise ToolError("no_data", "No SOPs are loaded.")
    sops["is_current"] = pd.to_numeric(sops["is_current"])
    sops["revision"] = pd.to_numeric(sops["revision"]).astype(int)
    if len(links):
        links["revision"] = pd.to_numeric(links["revision"]).astype(int)
    question = f"{a.question} {mid}" if mid else a.question
    hits = search_sops(question, sops, links)
    if not hits:
        return ToolResult("find_sop", True, data={"found": False, "question": a.question,
                                                  "sops_searched": sorted(sops["doc_no"])},
                          notes=[f"No SOP in the data covers this (searched "
                                 f"{len(sops)} current SOPs; none scored {MIN_SCORE:g}). Say so; "
                                 f"don't adapt another SOP."],
                          sources=[cite("table", "sops", what="Current SOPs searched")])
    doc_no, rev, score = hits[0]
    quote, src, notes = _sop_quote(db, doc_no, rev)
    if mid and not ((links["doc_no"] == doc_no) & (links["machine_id"] == mid)).any():
        notes.append(f"{doc_no} doesn't name {mid}; it matched on the question's words.")
    data = {"found": True, "doc_no": doc_no, "revision": rev, "score": score,
            "quote_whole": quote, "other_matches": [h[0] for h in hits[1:]]}
    notes.append("Quote the SOP whole, with its revision; it is the only revision in the data "
                 "(trap list D9).")
    return ToolResult("find_sop", True, data=data, notes=notes, sources=[src])


# =============================================================== query_sql ===

# The objects agent_user.py grants (allowed_objects with the default names). The database
# refuses anything else anyway; this check refuses it before the query is sent.
ALLOWED_OBJECTS = {
    "machine_spares", "work_order_detail", "incident_detail", "work_order_sensor_window",
    "machine_overview", "machine_nameplate", "machine_sops", "incident_report_vs_cmms",
    "table_relationships", "cmms_machines_audit", "cmms_work_orders_audit",
    "cmms_safety_incidents_audit", "vibration_temp_audit", "critical_spares_audit",
    "img_nameplates_audit", "img_incident_reports_audit", "sops", "sops_audit",
    "sop_steps_audit", "sop_machines",
}
# Columns query_sql may not touch: severity_as_logged means opposite things before and after
# Jul 2025 (C1); raw columns are values as written, before cleaning.
BLOCKED_COLUMNS = {"severity_as_logged", "raw_tuple"}
BLOCKED_FUNCTIONS = {"sleep", "benchmark", "load_file", "get_lock", "release_lock"}


def check_sql(sql: str, limit: int = SQL_ROW_LIMIT) -> str:
    """The query to run, or ToolError('not_allowed'). One SELECT, granted objects only, no
    SELECT *, no blocked columns or functions, LIMIT at most `limit`."""
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        raise ToolError("not_allowed", "query_sql is switched off: sqlglot isn't installed "
                                       "(pip install sqlglot).") from None
    try:
        statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise ToolError("not_allowed", f"Couldn't parse the SQL: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise ToolError("not_allowed", "Exactly one statement, please.")
    q = statements[0]
    if not isinstance(q, exp.Select):
        raise ToolError("not_allowed", "Only a single SELECT is allowed (no UNION, no writes).")
    if q.find(exp.Into) or q.args.get("locks"):
        raise ToolError("not_allowed", "SELECT ... INTO and locking reads aren't allowed.")
    ctes = {c.alias_or_name.lower() for c in q.find_all(exp.CTE)}
    tables = {t.name.lower() for t in q.find_all(exp.Table)} - ctes
    bad = sorted(t for t in tables if t not in ALLOWED_OBJECTS)
    if bad:
        raise ToolError("not_allowed", f"Not readable by the app: {', '.join(bad)}. Readable: "
                                       f"{', '.join(sorted(ALLOWED_OBJECTS))}.")
    if any(not isinstance(s.parent, exp.Count) for s in q.find_all(exp.Star)):
        raise ToolError("not_allowed", "Name the columns; SELECT * isn't allowed.")
    cols = {c.name.lower() for c in q.find_all(exp.Column)}
    blocked = sorted(c for c in cols if c in BLOCKED_COLUMNS or c.endswith("_raw"))
    if blocked:
        raise ToolError("not_allowed", f"Column(s) not allowed: {', '.join(blocked)}. For "
                                       f"severity use severity_5_worst.")
    funcs = {(f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
             for f in q.find_all(exp.Func)}
    if funcs & BLOCKED_FUNCTIONS:
        raise ToolError("not_allowed", f"Function not allowed: {', '.join(sorted(funcs & BLOCKED_FUNCTIONS))}.")
    lim = q.args.get("limit")
    n = None
    if lim is not None:
        try:
            n = int(lim.expression.name)
        except (AttributeError, ValueError):
            n = None
    if n is None or n > limit:
        q = q.limit(limit)
    return q.sql(dialect="mysql")


class SqlArgs(Args):
    sql: str = Field(..., max_length=2000,
                     description="ONE MySQL SELECT over the readable views, naming its columns.")
    reason: str = Field(..., max_length=200,
                        description="Why no other tool answers this question.")


@tool("query_sql", """LAST RESORT, only when no other tool fits: one read-only SELECT over the
views the app may read (machine_overview, work_order_detail, incident_detail, machine_spares,
machine_nameplate, machine_sops, incident_report_vs_cmms, work_order_sensor_window,
table_relationships, and the *_audit views). Name the columns; severity only as
severity_5_worst; at most 200 rows. The SQL is shown with the answer.""", SqlArgs)
def query_sql(db: Db, a: SqlArgs) -> ToolResult:
    sql = check_sql(a.sql)
    rows = db.query(sql)
    tables = sorted({t for t in ALLOWED_OBJECTS if re.search(rf"\b{t}\b", sql, re.I)})
    return ToolResult("query_sql", True, data={"sql": sql, "rows": rows[:SQL_ROW_LIMIT],
                                               "row_count": len(rows), "reason": a.reason},
                      notes=["Answer from a custom query: show the SQL with the answer.",
                             f"Capped at {SQL_ROW_LIMIT} rows."] if len(rows) >= SQL_ROW_LIMIT
                      else ["Answer from a custom query: show the SQL with the answer."],
                      sources=[cite("table", t) for t in tables])


# ===================================================================== CLI ===

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schemas", help="print the tool definitions the LLM gets")
    c = sub.add_parser("call", help="run one tool against the database, as the app user")
    c.add_argument("tool", choices=sorted(TOOLS))
    c.add_argument("arguments", nargs="?", default="{}", help="JSON arguments")
    c.add_argument("--trace", action="store_true", help="also print the SQL that ran")
    ap.add_argument("--env-file", help="path to .env (default: nearest .env)")
    a = ap.parse_args()
    if a.cmd == "schemas":
        print(json.dumps(tool_schemas(), indent=2))
        return
    try:
        db = connect(a.env_file)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    r = run_tool(db, a.tool, a.arguments)
    print(json.dumps(r.to_dict(trace=a.trace), indent=2, ensure_ascii=False))
    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    main()