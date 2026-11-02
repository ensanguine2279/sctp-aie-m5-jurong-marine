#!/usr/bin/env python3
"""Ingest the CMMS SQL dump into MySQL, with lineage.

The dump is PARSED, never executed, so nothing in it can drop or overwrite our
tables. For each of machines, employees, work_orders and safety_incidents:

    ingest_runs          one row per load: file, SHA-256, git commit, counts, status
    cmms_<table>_raw     every INSERT tuple exactly as in the dump (text), with its
                         line number, tuple number, status (loaded / rejected),
                         reject reason, warnings and, for duplicates, the raw_id kept
    cmms_<table>         cleaned rows only; raw_id links each one to its raw row

    cmms_<table>_audit (view) clean + raw + run joined, for citations

Pipeline (each step is a small function you can test on its own):
    read_dump               split statements, parse INSERT tuples (values kept as text)
      -> clean_text_columns  trim; ids upper-case; categories lower-case
      -> parse_numbers       integers and decimals ('S$1,050' -> 1050.00)
      -> parse_dates         ISO, or dd/mm/yyyy (day first, flagged)
      -> normalise_severity  incidents only: 1 = worst before Jul 2025 -> 5 = worst scale
      -> split_rejects       unparseable tuples, missing/bad keys, duplicate keys
      -> check_references    machine_id / emp_code found in machines / employees?
      -> add_warnings        loaded, but something is missing or suspicious
      -> load                one transaction for all four tables

Parents (machines, employees) are cleaned first so children can be checked.
Re-loading the same file name replaces that file's rows; other files are kept.

Severity rule: before --severity-cutover the scale was 1 = worst; from it, 5 = worst.
severity_5_worst puts every incident on the new scale (old s -> 6 - s). Incidents
from the cutover to --severity-ambiguous-until are NOT converted (scale 'ambiguous')
until the client confirms the exact switch-over date.

Usage
    python scripts/ingest_cmms.py data/sql/jurongmarine_cmms.sql --dry-run
    python scripts/ingest_cmms.py data/sql/jurongmarine_cmms.sql --recreate   # first time
    python scripts/ingest_cmms.py data/sql/jurongmarine_cmms.sql
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from db_config import add_common_args, load_db_config, safe_identifier, to_records
from lineage import RUNS_DDL, finish_run, start_run

DEFAULT_DUMP = "data/sql/jurongmarine_cmms.sql"
SEVERITY_CUTOVER = date(2025, 7, 1)
SEVERITY_AMBIGUOUS_UNTIL = date(2025, 7, 31)
SOURCE_COLUMNS = ["raw_id", "source_line", "tuple_no", "raw_tuple", "parse_error"]
LINEAGE_COLUMNS = {"raw_id", "run_id", "source_file", "source_line", "tuple_no", "raw_tuple",
                   "status", "reject_reason", "duplicate_of_raw_id", "warnings"}


# ------------------------------------------------------------------ tables ---

@dataclass(frozen=True)
class TableSpec:
    name: str
    key: str
    key_type: str                 # 'int' | 'code'
    columns: tuple                # columns in the dump, canonical order
    clean_ddl: tuple              # (column, SQL type) in the clean table, key first
    codes: tuple = ()             # ids: trimmed, upper-case (same rule as the sensor logs)
    categories: tuple = ()        # trimmed, lower-case
    texts: tuple = ()             # trimmed, whitespace collapsed
    ints: tuple = ()
    decimals: tuple = ()          # (column, decimal places)
    dates: tuple = ()
    indexes: tuple = ()           # extra KEY definitions on the clean table
    report: tuple = ()            # columns whose distinct values the summary prints

    @property
    def raw_names(self) -> dict:
        """Dump column -> raw table column. A dump column that clashes with a lineage
        column (work_orders.status vs the load status) gets a _src suffix."""
        return {c: f"{c}_src" if c in LINEAGE_COLUMNS else c for c in self.columns}

    @property
    def raw_columns(self) -> list[str]:
        return ["raw_id", "run_id", "source_file", "source_line", "tuple_no", "raw_tuple",
                *self.raw_names.values(), "status", "reject_reason", "duplicate_of_raw_id",
                "warnings"]

    @property
    def clean_columns(self) -> list[str]:
        return ["raw_id", *(c for c, _ in self.clean_ddl)]


SPECS = [
    TableSpec(
        "machines", "machine_id", "code",
        columns=("machine_id", "machine_name", "model", "year_installed"),
        clean_ddl=(("machine_id", "VARCHAR(20) NOT NULL"),
                   ("machine_name", "VARCHAR(100) NULL"),
                   ("model", "VARCHAR(100) NULL"),
                   ("year_installed", "INT NULL")),
        codes=("machine_id",), texts=("machine_name", "model"), ints=("year_installed",),
    ),
    TableSpec(
        "employees", "emp_id", "int",
        columns=("emp_id", "emp_code", "emp_name", "trade", "shift"),
        clean_ddl=(("emp_id", "INT NOT NULL"),
                   ("emp_code", "VARCHAR(20) NULL COMMENT 'not unique in source: see warnings'"),
                   ("emp_name", "VARCHAR(100) NULL COMMENT 'personal data (PDPA): keep out of answers'"),
                   ("trade", "VARCHAR(40) NULL"),
                   ("shift", "VARCHAR(20) NULL")),
        codes=("emp_code",), categories=("trade", "shift"), texts=("emp_name",), ints=("emp_id",),
        indexes=("KEY ix_{t}_emp_code (emp_code)",), report=("trade", "shift"),
    ),
    TableSpec(
        "work_orders", "wo_id", "int",
        columns=("wo_id", "machine_id", "wo_type", "description", "wo_date",
                 "duration_hrs", "cost_sgd", "emp_code", "status"),
        clean_ddl=(("wo_id", "INT NOT NULL"),
                   ("machine_id", "VARCHAR(20) NULL"),
                   ("wo_type", "VARCHAR(40) NULL"),
                   ("description", "VARCHAR(255) NULL"),
                   ("wo_date", "DATE NULL"),
                   ("duration_hrs", "DECIMAL(7,1) NULL"),
                   ("cost_sgd", "DECIMAL(12,2) NULL"),
                   ("emp_code", "VARCHAR(20) NULL"),
                   ("status", "VARCHAR(40) NULL")),
        codes=("machine_id", "emp_code"), categories=("wo_type", "status"),
        texts=("description",), ints=("wo_id",),
        decimals=(("duration_hrs", 1), ("cost_sgd", 2)), dates=("wo_date",),
        indexes=("KEY ix_{t}_machine_date (machine_id, wo_date)", "KEY ix_{t}_emp_code (emp_code)"),
        report=("wo_type", "status"),
    ),
    TableSpec(
        "safety_incidents", "incident_id", "int",
        columns=("incident_id", "incident_date", "machine_id", "description", "severity",
                 "lost_days", "emp_code", "reporting"),
        clean_ddl=(("incident_id", "INT NOT NULL"),
                   ("incident_date", "DATE NULL"),
                   ("machine_id", "VARCHAR(20) NULL"),
                   ("description", "VARCHAR(255) NULL"),
                   ("severity_as_logged", "INT NULL COMMENT 'as recorded; meaning depends on the date'"),
                   ("severity_scale", "VARCHAR(12) NOT NULL COMMENT '1_worst | 5_worst | ambiguous | unknown'"),
                   ("severity_5_worst", "TINYINT NULL COMMENT 'all incidents on the 5 = worst scale; NULL if unknown'"),
                   ("lost_days", "INT NULL"),
                   ("emp_code", "VARCHAR(20) NULL"),
                   ("reporting", "VARCHAR(100) NULL")),
        codes=("machine_id", "emp_code"), texts=("description", "reporting"),
        ints=("incident_id", "severity", "lost_days"), dates=("incident_date",),
        indexes=("KEY ix_{t}_machine_date (machine_id, incident_date)",
                 "KEY ix_{t}_emp_code (emp_code)"),
        report=("severity_scale", "reporting"),
    ),
]
SPEC_BY_NAME = {s.name: s for s in SPECS}


def ddl(spec: TableSpec, t: str) -> list[str]:
    raw_cols = "".join(f"        `{r}` VARCHAR(255) NULL COMMENT 'as in dump ({c})',\n"
                       for c, r in spec.raw_names.items())
    clean_cols = "".join(f"        `{c}` {d},\n" for c, d in spec.clean_ddl)
    indexes = "".join(f"        {i.format(t=t)},\n" for i in spec.indexes)
    raw_alias = ", ".join(f"r.`{r}` AS `{c}_raw`" for c, r in spec.raw_names.items())
    return [
        f"""
    CREATE TABLE IF NOT EXISTS {t}_raw (
        raw_id              BIGINT       NOT NULL PRIMARY KEY,
        run_id              BIGINT       NULL,
        source_file         VARCHAR(255) NOT NULL,
        source_line         INT          NOT NULL COMMENT 'line in the dump where the tuple starts',
        tuple_no            INT          NOT NULL COMMENT 'position in its INSERT (1 = first)',
        raw_tuple           TEXT         NOT NULL COMMENT 'tuple exactly as in the dump',
{raw_cols}        status              VARCHAR(10)  NOT NULL COMMENT 'loaded | rejected',
        reject_reason       VARCHAR(40)  NULL,
        duplicate_of_raw_id BIGINT       NULL COMMENT 'for duplicates: the raw row that was kept',
        warnings            VARCHAR(255) NULL COMMENT 'loaded with gaps, e.g. unknown_machine',
        KEY ix_raw_file (source_file, source_line),
        KEY ix_raw_status (status, reject_reason),
        CONSTRAINT fk_{t}_raw_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
            ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
        f"""
    CREATE TABLE IF NOT EXISTS {t} (
        id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
        raw_id              BIGINT       NOT NULL,
{clean_cols}        UNIQUE KEY uq_{t}_raw (raw_id),
        UNIQUE KEY uq_{t}_key (`{spec.key}`),
{indexes}        CONSTRAINT fk_{t}_raw FOREIGN KEY (raw_id) REFERENCES {t}_raw (raw_id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
        f"""
    CREATE OR REPLACE VIEW {t}_audit AS
    SELECT c.*, r.source_file, r.source_line, r.tuple_no, r.raw_tuple, {raw_alias},
           r.warnings, r.run_id, u.file_sha256, u.started_at AS loaded_at
    FROM {t} c
    JOIN {t}_raw r ON r.raw_id = c.raw_id
    LEFT JOIN ingest_runs u ON u.run_id = r.run_id
    """,
    ]


# ------------------------------------------------------------------- reading ---
# A small SQL tokenizer: enough to split a mysqldump / DBeaver export into
# statements and read INSERT ... VALUES tuples, without running any SQL.

def skip_quoted(text: str, i: int) -> int:
    """Return the index just after the quoted token starting at text[i]."""
    q, n, j = text[i], len(text), i + 1
    while j < n:
        c = text[j]
        if c == "\\" and q != "`":
            j += 2
            continue
        if c == q:
            if j + 1 < n and text[j + 1] == q:   # doubled quote: '' or ""
                j += 2
                continue
            return j + 1
        j += 1
    return n


@dataclass
class Statement:
    line: int   # line of the statement's first character (1-based)
    text: str


def iter_statements(text: str) -> Iterator[Statement]:
    """Split a SQL script on ';', ignoring ';' inside quotes and comments."""
    i, n, line = 0, len(text), 1
    start, start_line = None, 0
    while i < n:
        ch, nxt = text[i], text[i + 1] if i + 1 < n else ""
        if (ch == "-" and nxt == "-" and (i + 2 >= n or text[i + 2] in " \t\r\n")) or ch == "#":
            j = text.find("\n", i)
            i = n if j == -1 else j          # the newline is counted below
            continue
        if ch == "/" and nxt == "*":         # includes /*!40101 ... */ directives
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            line += text.count("\n", i, j)
            i = j
            continue
        if ch in "'\"`":
            if start is None:
                start, start_line = i, line
            j = skip_quoted(text, i)
            line += text.count("\n", i, j)
            i = j
            continue
        if ch == ";":
            if start is not None:
                yield Statement(start_line, text[start:i])
                start = None
            i += 1
            continue
        if ch == "\n":
            line += 1
        elif start is None and not ch.isspace():
            start, start_line = i, line
        i += 1
    if start is not None and text[start:].strip():
        yield Statement(start_line, text[start:])


_ESCAPES = {"0": "\0", "b": "\b", "n": "\n", "r": "\r", "t": "\t",
            "Z": "\x1a", "\\": "\\", "'": "'", '"': '"'}


def unescape(token: str) -> str:
    """Quoted SQL literal -> its text: 'O''Brien' and 'O\\'Brien' -> O'Brien."""
    q = token[0]
    inner = token[1:-1] if len(token) >= 2 and token.endswith(q) else token[1:]
    out, i = [], 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            out.append(_ESCAPES.get(inner[i + 1], inner[i + 1]))
            i += 2
        elif c == q and inner[i + 1:i + 2] == q:
            out.append(q)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_NAME = r'[`"]?([A-Za-z0-9_$]+)[`"]?'
_SCHEMA = r'(?:[`"]?[A-Za-z0-9_$]+[`"]?\s*\.\s*)?'
INSERT_RE = re.compile(
    r"^\s*INSERT\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY)\s+)?(?:IGNORE\s+)?INTO\s+"
    + _SCHEMA + _NAME + r"\s*(?:\(([^)]*)\))?\s*VALUES?\s*", re.I | re.S)
CREATE_RE = re.compile(
    r"^\s*CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _SCHEMA + _NAME + r"\s*\(",
    re.I)
_NOT_COLUMNS = {"PRIMARY", "KEY", "UNIQUE", "INDEX", "CONSTRAINT", "FOREIGN", "CHECK",
                "FULLTEXT", "SPATIAL"}


def _ident(s: str) -> str:
    return s.strip().strip('`"').strip()


def split_top_level(s: str) -> list[str]:
    """Split on commas outside parentheses and quotes."""
    parts, depth, i, last = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c in "'\"`":
            i = skip_quoted(s, i)
            continue
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            parts.append(s[last:i])
            last = i + 1
        i += 1
    parts.append(s[last:])
    return [p.strip() for p in parts if p.strip()]


def parse_create(stmt: str) -> tuple[str, list[str]] | None:
    """CREATE TABLE -> (table, column names in order). Used only for column order."""
    m = CREATE_RE.match(stmt)
    if not m:
        return None
    i = start = m.end()
    depth = 1
    while i < len(stmt) and depth:
        if stmt[i] in "'\"`":
            i = skip_quoted(stmt, i)
            continue
        depth += (stmt[i] == "(") - (stmt[i] == ")")
        i += 1
    cols = []
    for part in split_top_level(stmt[start:i - 1]):
        first = part.split(None, 1)[0]
        if _ident(first).upper() in _NOT_COLUMNS and not first.startswith(("`", '"')):
            continue
        cols.append(_ident(first))
    return m.group(1), cols


def parse_tuples(stmt: Statement, pos: int) -> list[dict]:
    """Parse VALUES (..),(..) from stmt.text[pos]. One dict per tuple:
    source_line, tuple_no, raw_tuple, values (list of str / None) or parse_error."""
    s, n = stmt.text, len(stmt.text)
    out, line, line_pos, i = [], stmt.line, 0, pos

    def ws(j: int) -> int:
        while j < n and s[j].isspace():
            j += 1
        return j

    while True:
        while i < n and (s[i].isspace() or s[i] == ","):
            i += 1
        if i >= n or s[i] != "(":
            break                                   # end, or ON DUPLICATE KEY UPDATE ...
        start = i
        line += s.count("\n", line_pos, start)
        line_pos = start
        vals, err = [], None
        i = ws(i + 1)
        if i < n and s[i] == ")":
            i += 1
        else:
            while True:
                i = ws(i)
                if i >= n:
                    err = "unterminated_tuple"
                    break
                if s[i] in "'\"":
                    j = skip_quoted(s, i)
                    vals.append(unescape(s[i:j]))
                else:
                    j, depth = i, 0
                    while j < n and not (depth == 0 and s[j] in ",)"):
                        if s[j] in "'\"":
                            j = skip_quoted(s, j)
                            continue
                        depth += (s[j] == "(") - (s[j] == ")")
                        j += 1
                    tok = s[i:j].strip()
                    vals.append(None if tok.upper() == "NULL" else tok)
                i = ws(j)
                if i < n and s[i] == ",":
                    i += 1
                elif i < n and s[i] == ")":
                    i += 1
                    break
                else:
                    err = "malformed_tuple"
                    break
        out.append({"source_line": line, "tuple_no": len(out) + 1, "raw_tuple": s[start:i],
                    "values": None if err else vals, "parse_error": err})
        if err:
            break                                   # can't trust the rest of this statement
    return out


@dataclass
class Dump:
    tuples: dict = field(default_factory=dict)          # table -> [tuple dicts]
    insert_columns: dict = field(default_factory=dict)  # table -> column list per INSERT
    create_columns: dict = field(default_factory=dict)  # table -> columns from CREATE TABLE
    statements: Counter = field(default_factory=Counter)


def parse_dump(text: str) -> Dump:
    dump = Dump()
    for stmt in iter_statements(text):
        m = INSERT_RE.match(stmt.text)
        if m:
            dump.statements["insert"] += 1
            t = m.group(1).lower()
            cols = [_ident(c).lower() for c in m.group(2).split(",")] if m.group(2) else None
            for tup in parse_tuples(stmt, m.end()):
                tup["columns"] = cols
                dump.tuples.setdefault(t, []).append(tup)
            continue
        created = parse_create(stmt.text)
        if created:
            dump.statements["create_table"] += 1
            dump.create_columns[created[0].lower()] = [c.lower() for c in created[1]]
            continue
        dump.statements[stmt.text.split(None, 1)[0].lower()] += 1
    return dump


def read_dump(path: str) -> Dump:
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    return parse_dump(text)


def to_frame(spec: TableSpec, dump: Dump) -> pd.DataFrame:
    """One row per tuple: the spec's columns as text (None = SQL NULL) + where it came from."""
    rows = []
    for tup in dump.tuples.get(spec.name, []):
        cols = tup["columns"] or dump.create_columns.get(spec.name) or list(spec.columns)
        row = {k: tup[k] for k in ("source_line", "tuple_no", "raw_tuple", "parse_error")}
        values = {}
        if tup["values"] is not None:
            if len(tup["values"]) == len(cols):
                values = dict(zip(cols, tup["values"]))
            else:
                row["parse_error"] = "column_count_mismatch"
        rows.append({**row, **{c: values.get(c) for c in spec.columns}})
    return pd.DataFrame(rows, columns=[*SOURCE_COLUMNS[1:], *spec.columns], dtype=object)


# ------------------------------------------------------------ pipeline steps ---

def _bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


def _text(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = " ".join(str(v).split())
    return s or None


def clean_text_columns(df: pd.DataFrame, codes=(), categories=(), texts=()) -> pd.DataFrame:
    """Trim everything; ids upper-case ('cnc-02 ' -> 'CNC-02'); categories lower-case."""
    df = df.copy()
    for cols, fix in [(codes, str.upper), (categories, str.lower), (texts, None)]:
        for c in cols:
            df[c] = df[c].map(lambda v, f=fix: (f(s) if f else s) if (s := _text(v)) else None)
    return df


def parse_int(v) -> int | None:
    """'12' -> 12, '1,200' -> 1200, '12.0' -> 12; '12.5', 'abc', blank -> None."""
    s = _text(v)
    if s is None:
        return None
    try:
        d = Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None
    return int(d) if d.is_finite() and d == d.to_integral_value() else None


_MONEY = re.compile(r"(?i)^(?:sgd|s\$|\$)\s*|\s*sgd$")


def parse_decimal(v, places: int) -> Decimal | None:
    """'S$1,050' -> 1050.00 (places=2). Not a number -> None."""
    s = _text(v)
    if s is None:
        return None
    try:
        d = Decimal(_MONEY.sub("", s).replace(",", ""))
    except InvalidOperation:
        return None
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) if d.is_finite() else None


def parse_numbers(df: pd.DataFrame, ints=(), decimals=()) -> pd.DataFrame:
    """Read numbers. Adds <col>_given (something was written) so a value that was
    written but couldn't be read is flagged later instead of passing as NULL."""
    df = df.copy()
    for c in ints:
        df[f"{c}_given"] = df[c].map(_text).notna()
        df[c] = df[c].map(parse_int).astype("Int64")
    for c, places in decimals:
        df[f"{c}_given"] = df[c].map(_text).notna()
        df[c] = df[c].map(lambda v, p=places: parse_decimal(v, p))
    return df


DATE_FORMATS = [  # (strptime format, name) - first match wins
    ("%Y-%m-%d", "iso"), ("%Y-%m-%d %H:%M:%S", "iso_datetime"), ("%Y/%m/%d", "ymd_slash"),
    ("%d/%m/%Y", "dmy_slash"), ("%d-%m-%Y", "dmy_dash"),   # Singapore: day first
    ("%d %b %Y", "d_mon_y"), ("%d-%b-%Y", "d_mon_y"),
]


def parse_date(v) -> tuple[date | None, str]:
    """Return (date, format). Format is 'missing', 'zero_date' or 'unparseable' on failure."""
    s = _text(v)
    if s is None:
        return None, "missing"
    if s.startswith("0000-00-00"):
        return None, "zero_date"
    for fmt, name in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date(), name
        except ValueError:
            pass
    return None, "unparseable"


def parse_dates(df: pd.DataFrame, cols=()) -> pd.DataFrame:
    """Dates as datetime.date; adds <col>_format for the report and warnings."""
    df = df.copy()
    for c in cols:
        parsed = df[c].map(parse_date)
        df[c] = parsed.map(lambda t: t[0])
        df[f"{c}_format"] = parsed.map(lambda t: t[1])
    return df


def normalise_severity(df: pd.DataFrame, cutover: date = SEVERITY_CUTOVER,
                       ambiguous_until: date | None = SEVERITY_AMBIGUOUS_UNTIL) -> pd.DataFrame:
    """Put every incident on the 5 = worst scale.

    Before `cutover` the scale was 1 = worst, so old s -> 6 - s. Incidents from
    `cutover` to `ambiguous_until` are left NULL (scale 'ambiguous') because the
    exact switch-over date isn't confirmed. Out-of-range or undated -> 'unknown'.
    The original number is kept in severity_as_logged.
    """
    df = df.copy()
    sev = df["severity"]
    when = pd.to_datetime(df["incident_date"])
    in_range = _bool(sev.between(1, 5))
    before = _bool(when < pd.Timestamp(cutover))
    ambiguous = (_bool((when >= pd.Timestamp(cutover)) & (when <= pd.Timestamp(ambiguous_until)))
                 if ambiguous_until else pd.Series(False, index=df.index))

    conditions = [sev.isna(), ~in_range, when.isna(), before, ambiguous]
    df["severity_scale"] = np.select(conditions, ["unknown", "unknown", "unknown", "1_worst",
                                                  "ambiguous"], default="5_worst")
    warning = pd.Series(
        np.select(conditions, ["", "severity_out_of_range", "severity_scale_unknown", "",
                               "severity_scale_ambiguous"], default=""), index=df.index, dtype=object)
    df["severity_warning"] = warning.where(warning != "", None)
    df.loc[sev.isna() & ~df["severity_given"], "severity_warning"] = "missing_severity"
    df["severity_as_logged"] = sev
    df["severity_5_worst"] = (sev.where(df["severity_scale"] == "5_worst")
                              .fillna((6 - sev).where(df["severity_scale"] == "1_worst"))
                              .astype("Int64"))
    return df


def split_rejects(df: pd.DataFrame, key: str, key_type: str, seen: dict) -> pd.DataFrame:
    """Add reject_reason and duplicate_of_raw_id. The first row with a key wins.

    `seen` maps key -> raw_id of the row kept (and, for a real load, keys already
    loaded from other files). A duplicate whose values differ from the kept row
    is rejected as duplicate_key_conflict, so it gets reviewed.
    """
    df = df.copy()
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    reason[df["parse_error"].notna()] = df["parse_error"]
    if key_type == "int":
        reason[reason.isna() & ~df[f"{key}_given"]] = "missing_key"
        reason[reason.isna() & df[key].isna()] = "bad_key"
    else:
        reason[reason.isna() & df[key].isna()] = "missing_key"

    value_cols = [c for c in df.columns if c not in (*SOURCE_COLUMNS, key)]
    signature = df[value_cols].apply(
        lambda r: "|".join("" if pd.isna(v) else str(v) for v in r), axis=1)
    dup_of = pd.Series(pd.NA, index=df.index, dtype="Int64")
    first: dict = {}
    for i, k, raw_id in zip(df.index, df[key], df["raw_id"]):
        if pd.notna(reason[i]):
            continue
        k = int(k) if key_type == "int" else k
        if k in seen:
            dup_of[i] = seen[k]
            reason[i] = ("duplicate_key_conflict" if k in first and first[k] != signature[i]
                         else "duplicate_key")
        else:
            seen[k] = raw_id
            first[k] = signature[i]
    df["reject_reason"] = reason
    df["duplicate_of_raw_id"] = dup_of
    return df


def check_references(df: pd.DataFrame, machine_ids: set, emp_codes: set,
                     parent: bool = False) -> pd.DataFrame:
    """Add machine_known / emp_known for child tables (orphans are flagged, not dropped)."""
    df = df.copy()
    if parent:
        return df
    if "machine_id" in df:
        df["machine_known"] = df["machine_id"].isin(machine_ids)
    if "emp_code" in df:
        df["emp_known"] = df["emp_code"].isin(emp_codes)
    return df


def add_warnings(df: pd.DataFrame, spec: TableSpec, today: date | None = None) -> pd.DataFrame:
    """Comma-separated notes for rows that load but have gaps or look wrong."""
    df = df.copy()
    ok = df["reject_reason"].isna()
    flags: dict[str, pd.Series] = {}

    for c in [*spec.ints, *(c for c, _ in spec.decimals)]:
        if c == spec.key:
            continue
        flags[f"bad_{c}"] = df[f"{c}_given"] & df[c].isna()
        flags[f"negative_{c}"] = df[c].map(lambda v: v is not None and not pd.isna(v) and v < 0)
    for c in spec.dates:
        fmt = df[f"{c}_format"]
        flags["missing_date"] = fmt == "missing"
        flags["bad_date"] = fmt.isin(["zero_date", "unparseable"])
        flags["date_assumed_day_first"] = fmt.str.startswith("dmy")
    if "year_installed" in df:
        year = today.year if today else date.today().year
        flags["implausible_year_installed"] = _bool(~df["year_installed"].between(1950, year)
                                                    & df["year_installed"].notna())
    if "machine_known" in df:
        flags["missing_machine_id"] = df["machine_id"].isna()
        flags["unknown_machine"] = df["machine_id"].notna() & ~df["machine_known"]
    if spec.name == "employees":
        counts = df.loc[ok, "emp_code"].value_counts()
        flags["missing_emp_code"] = df["emp_code"].isna()
        flags["duplicate_emp_code"] = df["emp_code"].map(counts).fillna(0) > 1
    elif "emp_known" in df:
        flags["missing_emp_code"] = df["emp_code"].isna()
        flags["unknown_employee"] = df["emp_code"].notna() & ~df["emp_known"]
    if "severity_warning" in df:
        for w in ["missing_severity", "severity_out_of_range", "severity_scale_unknown",
                  "severity_scale_ambiguous"]:
            flags[w] = df["severity_warning"] == w

    f = pd.DataFrame({k: _bool(v) for k, v in flags.items()}, index=df.index)
    df["warnings"] = (f.apply(lambda r: ",".join(r.index[r]) or None, axis=1)
                      if len(f.columns) and len(f) else pd.Series(None, index=df.index, dtype=object))
    return df


def process(dump: Dump, stats: Counter, *, source_file: str = "", run_id: int | None = None,
            first_raw_ids: dict | None = None, seen: dict | None = None,
            machine_ids: set | None = None, emp_codes: set | None = None,
            cutover: date = SEVERITY_CUTOVER,
            ambiguous_until: date | None = SEVERITY_AMBIGUOUS_UNTIL) -> dict:
    """Clean every table. Returns {table: (raw_rows, clean_rows)} and fills `stats`.

    For a real load, `first_raw_ids`, `seen`, `machine_ids` and `emp_codes` come from
    rows already loaded from other files.
    """
    first_raw_ids = first_raw_ids or {}
    seen = {k: dict(v) for k, v in (seen or {}).items()}
    machine_ids, emp_codes = set(machine_ids or ()), set(emp_codes or ())
    out = {}
    for spec in SPECS:
        frame = to_frame(spec, dump)
        first = first_raw_ids.get(spec.name, 1)
        frame.insert(0, "raw_id", range(first, first + len(frame)))

        cleaned = (frame
                   .pipe(clean_text_columns, spec.codes, spec.categories, spec.texts)
                   .pipe(parse_numbers, spec.ints, spec.decimals)
                   .pipe(parse_dates, spec.dates))
        if spec.name == "safety_incidents":
            cleaned = cleaned.pipe(normalise_severity, cutover, ambiguous_until)
        cleaned = (cleaned
                   .pipe(split_rejects, spec.key, spec.key_type, seen.setdefault(spec.name, {}))
                   .pipe(check_references, machine_ids, emp_codes,
                         parent=spec.name in ("machines", "employees"))
                   .pipe(add_warnings, spec))
        rejected = cleaned["reject_reason"].notna()

        raw = frame.rename(columns=spec.raw_names).assign(
            run_id=run_id, source_file=source_file,
            status=np.where(rejected, "rejected", "loaded"),
            reject_reason=cleaned["reject_reason"],
            duplicate_of_raw_id=cleaned["duplicate_of_raw_id"],
            warnings=cleaned["warnings"].where(~rejected),
        )[spec.raw_columns]
        clean = cleaned.loc[~rejected, spec.clean_columns]
        out[spec.name] = (raw, clean)

        # Parents first, so children can be checked against them.
        if spec.name == "machines":
            machine_ids |= set(clean["machine_id"]) | set(seen["machines"])
        if spec.name == "employees":
            emp_codes |= set(clean["emp_code"].dropna())

        t = spec.name
        stats["rows_read"] += len(raw)
        stats["rows_loaded"] += len(clean)
        stats["rows_rejected"] += int(rejected.sum())
        stats[f"{t}:read"] += len(raw)
        stats[f"{t}:loaded"] += len(clean)
        stats[f"{t}:rejected"] += int(rejected.sum())
        stats.update(f"{t}:rejected:{r}" for r in cleaned.loc[rejected, "reject_reason"])
        for w in cleaned.loc[~rejected, "warnings"].dropna():
            stats.update(f"{t}:warn:{x}" for x in w.split(","))
        for c in spec.report:
            stats.update(f"{t}:{c}:{'<blank>' if pd.isna(v) else v}" for v in clean[c])

    stats.update({f"statement:{k}": v for k, v in dump.statements.items()})
    for t in sorted(set(dump.tuples) - set(SPEC_BY_NAME)):
        stats[f"skipped:{t}"] += len(dump.tuples[t])
    return out


# ---------------------------------------------------------------------- load ---

def create_tables(engine, prefix: str, recreate: bool) -> None:
    from sqlalchemy import text

    # DDL commits implicitly in MySQL, so it runs before the data transaction.
    with engine.begin() as conn:
        conn.execute(text(RUNS_DDL))
        if recreate:
            for spec in SPECS:
                t = prefix + spec.name
                conn.execute(text(f"DROP VIEW IF EXISTS {t}_audit"))
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
                conn.execute(text(f"DROP TABLE IF EXISTS {t}_raw"))
        for spec in SPECS:
            for stmt in ddl(spec, prefix + spec.name):
                conn.execute(text(stmt))


def _insert(table: str, columns: list[str]):
    from sqlalchemy import text

    return text(f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})")


def load(engine, prefix: str, dump: Dump, dump_path: str, stats: Counter, run_id: int,
         **options) -> dict:
    """Replace this file's rows in all four tables in ONE transaction; other files are kept."""
    from sqlalchemy import text

    source_file = Path(dump_path).name
    with engine.begin() as conn:
        first_raw_ids, seen = {}, {}
        for spec in SPECS:
            t = prefix + spec.name
            # Deleting raw rows cascades to their clean rows.
            conn.execute(text(f"DELETE FROM {t}_raw WHERE source_file = :f"), {"f": source_file})
            first_raw_ids[spec.name] = conn.execute(
                text(f"SELECT COALESCE(MAX(raw_id), 0) + 1 FROM {t}_raw")).scalar_one()
            # Keys already loaded from OTHER files count as duplicates too.
            seen[spec.name] = {k: r for k, r in conn.execute(
                text(f"SELECT `{spec.key}`, raw_id FROM {t}"))}
        emp_codes = {e for (e,) in conn.execute(
            text(f"SELECT emp_code FROM {prefix}employees WHERE emp_code IS NOT NULL"))}

        tables = process(dump, stats, source_file=source_file, run_id=run_id,
                         first_raw_ids=first_raw_ids, seen=seen,
                         machine_ids=set(seen["machines"]), emp_codes=emp_codes, **options)
        for spec in SPECS:
            t = prefix + spec.name
            raw, clean = tables[spec.name]
            if len(raw):
                conn.execute(_insert(f"{t}_raw", spec.raw_columns), to_records(raw))
            if len(clean):
                conn.execute(_insert(t, spec.clean_columns), to_records(clean))
            print(f"  {t}: {len(raw)} raw rows, {len(clean)} clean rows")
    return tables


def _items(stats: Counter, prefix: str) -> str:
    """'a=1, b=2 (total 3)'; empty string if there is nothing under `prefix`."""
    items = {k[len(prefix):]: v for k, v in stats.items() if k.startswith(prefix) and v}
    if not items:
        return ""
    return (", ".join(f"{k}={v}" for k, v in sorted(items.items()))
            + f" (total {sum(items.values())})")


def print_report(stats: Counter) -> None:
    print(f"\nRows read: {stats['rows_read']}   loaded: {stats['rows_loaded']}   "
          f"rejected: {stats['rows_rejected']}")
    print(f"Statements in dump: {_items(stats, 'statement:')}")
    if skipped := _items(stats, "skipped:"):
        print(f"Tables with no spec (NOT loaded): {skipped}")
    for spec in SPECS:
        t = spec.name
        print(f"\n{t}: read {stats[f'{t}:read']}, loaded {stats[f'{t}:loaded']}, "
              f"rejected {stats[f'{t}:rejected']}")
        if not stats[f"{t}:read"]:
            print("  NO ROWS FOUND IN DUMP")
        for prefix, title in [("rejected:", "Rejected rows"), ("warn:", "Loaded with gaps")]:
            if items := _items(stats, f"{t}:{prefix}"):
                print(f"  {title}: {items}")
        for c in spec.report:
            if items := _items(stats, f"{t}:{c}:"):
                print(f"  {c}: {items}")


def rejected_rows(tables: dict) -> pd.DataFrame:
    frames = [raw[raw["status"] == "rejected"].assign(table=name)
              for name, (raw, _) in tables.items()]
    frames = [f for f in frames if len(f)]
    cols = ["table", "source_line", "tuple_no", "raw_tuple", "reject_reason", "duplicate_of_raw_id"]
    return pd.concat(frames)[cols] if frames else pd.DataFrame(columns=cols)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", nargs="?", default=os.getenv("CMMS_DUMP", DEFAULT_DUMP))
    ap.add_argument("--prefix", default="cmms_",
                    help="table name prefix, so we never touch the dump's own tables "
                         "(default %(default)s)")
    ap.add_argument("--severity-cutover", type=date.fromisoformat, default=SEVERITY_CUTOVER,
                    help="first date on the 5 = worst scale (default %(default)s)")
    ap.add_argument("--severity-ambiguous-until", default=SEVERITY_AMBIGUOUS_UNTIL,
                    type=lambda s: None if s.lower() == "none" else date.fromisoformat(s),
                    help="incidents from the cutover to this date are not converted; "
                         "'none' once the cutover is confirmed (default %(default)s)")
    ap.add_argument("--rejects", help="also write rejected rows + reason to this CSV")
    ap.add_argument("--recreate", action="store_true",
                    help="drop and recreate the tables (first run, or after a schema change)")
    add_common_args(ap)
    a = ap.parse_args()

    if not Path(a.dump).is_file():
        sys.exit(f"Dump not found: {a.dump}")
    for spec in SPECS:
        safe_identifier(f"{a.prefix}{spec.name}_audit")
    options = {"cutover": a.severity_cutover, "ambiguous_until": a.severity_ambiguous_until}
    dump = read_dump(a.dump)
    stats: Counter = Counter()

    if a.dry_run:
        tables = process(dump, stats, source_file=Path(a.dump).name, **options)
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            for name, (_, clean) in tables.items():
                if len(clean):
                    print(f"\n{name} (first 5 clean rows):")
                    print(clean.head().to_string(index=False))
            rej = rejected_rows(tables)
            if len(rej):
                print("\nRejected rows (first 10):")
                print(rej.head(10).to_string(index=False))
    else:
        try:
            cfg = load_db_config(a.env_file)
        except (ValueError, FileNotFoundError) as e:
            sys.exit(str(e))
        engine = cfg.engine()
        print(f"Loading {a.dump} into {cfg.describe()} tables {a.prefix}*")
        create_tables(engine, a.prefix, a.recreate)
        run_id = start_run(engine, "ingest_cmms.py", a.dump)
        try:
            tables = load(engine, a.prefix, dump, a.dump, stats, run_id, **options)
        except BaseException as e:
            finish_run(engine, run_id, ok=False, rows_read=stats["rows_read"] or None,
                       error=f"{type(e).__name__}: {e}")
            print(f"Load failed - nothing changed. Recorded as run {run_id} in ingest_runs.",
                  file=sys.stderr)
            raise
        finish_run(engine, run_id, ok=True, rows_read=stats["rows_read"],
                   rows_loaded=stats["rows_loaded"], rows_rejected=stats["rows_rejected"])
        print(f"Recorded as run {run_id} in ingest_runs.")

    print_report(stats)
    print(f"\nSeverity: before {a.severity_cutover} the scale was 1 = worst (converted); "
          f"from then 5 = worst.")
    if a.severity_ambiguous_until:
        print(f"          Incidents {a.severity_cutover} to {a.severity_ambiguous_until} left "
              f"unconverted (switch-over date unconfirmed).")
    rej = rejected_rows(tables)
    if a.rejects and len(rej):
        Path(a.rejects).parent.mkdir(parents=True, exist_ok=True)
        rej.to_csv(a.rejects, index=False)
        print(f"Rejected rows written to {a.rejects}")
    print("Dry run - nothing written to the database." if a.dry_run else "Done.")


if __name__ == "__main__":
    main()