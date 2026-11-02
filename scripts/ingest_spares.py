#!/usr/bin/env python3
"""Ingest the Critical Spares spreadsheet into MySQL, with lineage.

Tables, so every cleaned part can be traced back to the sheet row it came from:

    ingest_runs              one row per load: file, SHA-256, git commit, counts, status
    critical_spares_raw      every sheet row exactly as written (text), with its row
                             number, sheet revision, status (loaded / rejected),
                             reject reason, warnings, and for duplicates the raw_id kept
    critical_spares          cleaned parts only; raw_id links each to its raw row
    critical_spare_machines  part -> machine links ('CNC-01/02/03' -> 3 rows)

    critical_spares_audit (view) clean + raw + run joined, for citations

Pipeline (each step is a small function you can test on its own):
    read_sheet           find the header row, read the title/revision
      -> clean_basics       trim text, part numbers upper-case, quantities as numbers
      -> parse_costs        '$38/spool' -> 38.00 + cost_per='spool'
      -> parse_lead_times   '2-3 weeks (Germany)' -> 14-21 days + note
      -> parse_sizes        '85 in', '1-3/8 inch', '2.4mm' -> size_mm
      -> split_rejects      blank or duplicate part numbers
      -> add_warnings       loaded, but something couldn't be read
      -> machine_links      'CNC-01/02/03' -> CNC-01, CNC-02, CNC-03
      -> load               full refresh in one transaction

The sheet is a complete list, so each load replaces all spares rows with the
new sheet (ingest_runs keeps the history of which file was loaded when).

Usage
    python scripts/ingest_spares.py data/tabular/critical_spares.xlsx --dry-run
    python scripts/ingest_spares.py data/tabular/critical_spares.xlsx --recreate   # first time
    python scripts/ingest_spares.py data/tabular/critical_spares.xlsx
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

from db_config import add_common_args, load_db_config, to_records
from lineage import RUNS_DDL, finish_run, start_run

HEADER_MAP = {  # sheet header -> raw column name
    "part no": "part_no", "description": "description", "for machine": "for_machine",
    "qty": "qty", "min qty": "min_qty", "unit cost": "unit_cost",
    "lead time": "lead_time", "location": "location",
}
SHEET_COLUMNS = list(HEADER_MAP.values())
RAW_ID_STRIDE = 100_000  # raw_id = run_id * 100000 + sheet row: unique and readable

RAW_COLUMNS = [
    "raw_id", "run_id", "source_file", "source_row", "source_rev", "source_title",
    *SHEET_COLUMNS, "status", "reject_reason", "duplicate_of_raw_id", "warnings",
]
CLEAN_COLUMNS = [
    "part_no", "raw_id", "description", "qty", "min_qty", "unit_cost", "cost_per",
    "size_value", "size_unit", "size_mm", "size_raw", "lead_time_min_days",
    "lead_time_max_days", "is_local_supply", "lead_time_note", "location",
]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS critical_spares_raw (
        raw_id              BIGINT       NOT NULL PRIMARY KEY COMMENT 'run_id * 100000 + source_row',
        run_id              BIGINT       NULL,
        source_file         VARCHAR(255) NOT NULL,
        source_row          INT          NOT NULL COMMENT 'row number in the spreadsheet',
        source_rev          VARCHAR(20)  NULL COMMENT 'revision from the sheet title',
        source_title        VARCHAR(255) NULL,
        part_no             VARCHAR(100) NULL COMMENT 'as in sheet',
        description         VARCHAR(255) NULL COMMENT 'as in sheet',
        for_machine         VARCHAR(100) NULL COMMENT 'as in sheet',
        qty                 VARCHAR(50)  NULL COMMENT 'as in sheet',
        min_qty             VARCHAR(50)  NULL COMMENT 'as in sheet',
        unit_cost           VARCHAR(50)  NULL COMMENT 'as in sheet',
        lead_time           VARCHAR(100) NULL COMMENT 'as in sheet',
        location            VARCHAR(50)  NULL COMMENT 'as in sheet',
        status              VARCHAR(10)  NOT NULL COMMENT 'loaded | rejected',
        reject_reason       VARCHAR(40)  NULL,
        duplicate_of_raw_id BIGINT       NULL COMMENT 'for duplicates: the raw row that was kept',
        warnings            VARCHAR(255) NULL COMMENT 'loaded, but e.g. lead_time_unparsed',
        KEY ix_spares_raw_status (status, reject_reason),
        CONSTRAINT fk_spares_raw_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
            ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS critical_spares (
        part_no            VARCHAR(50)   NOT NULL PRIMARY KEY,
        raw_id             BIGINT        NOT NULL,
        description        VARCHAR(255)  NOT NULL,
        qty                INT           NOT NULL DEFAULT 0,
        min_qty            INT           NOT NULL DEFAULT 0,
        below_min          TINYINT(1)    AS (qty < min_qty) STORED,
        unit_cost          DECIMAL(12,2) NULL COMMENT 'currency not stated in sheet ($)',
        cost_per           VARCHAR(30)   NULL COMMENT 'e.g. spool, when not per piece',
        size_value         DECIMAL(10,4) NULL COMMENT 'size as written, in size_unit',
        size_unit          VARCHAR(5)    NULL COMMENT 'mm | in',
        size_mm            DECIMAL(10,2) NULL COMMENT 'size converted to mm',
        size_raw           VARCHAR(30)   NULL COMMENT 'the part of the description it came from',
        lead_time_min_days INT           NULL,
        lead_time_max_days INT           NULL,
        is_local_supply    TINYINT(1)    NOT NULL DEFAULT 0,
        lead_time_note     VARCHAR(100)  NULL COMMENT 'e.g. supplier country',
        location           VARCHAR(50)   NULL,
        UNIQUE KEY uq_spares_raw (raw_id),
        CONSTRAINT fk_spares_raw FOREIGN KEY (raw_id) REFERENCES critical_spares_raw (raw_id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS critical_spare_machines (
        part_no      VARCHAR(50) NOT NULL,
        machine_code VARCHAR(50) NOT NULL COMMENT 'ALL = used on every machine',
        PRIMARY KEY (part_no, machine_code),
        CONSTRAINT fk_csm_part FOREIGN KEY (part_no)
            REFERENCES critical_spares (part_no) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE OR REPLACE VIEW critical_spares_audit AS
    SELECT c.*, r.source_file, r.source_row, r.source_rev,
           r.for_machine AS for_machine_raw, r.qty AS qty_raw, r.min_qty AS min_qty_raw,
           r.unit_cost AS unit_cost_raw, r.lead_time AS lead_time_raw, r.warnings,
           r.run_id, u.file_sha256, u.started_at AS loaded_at
    FROM critical_spares c
    JOIN critical_spares_raw r ON r.raw_id = c.raw_id
    LEFT JOIN ingest_runs u ON u.run_id = r.run_id
    """,
]


# ------------------------------------------------------------------- reading ---

def read_sheet(path: str) -> tuple[pd.DataFrame, dict]:
    """Return (sheet rows exactly as read, metadata). The header row is found by 'Part No'."""
    ws = openpyxl.load_workbook(path, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    hdr = next((i for i, r in enumerate(rows)
                if any(str(c).strip().lower() == "part no" for c in r if c is not None)), None)
    if hdr is None:
        sys.exit("No header row with 'Part No' found.")

    cols = {HEADER_MAP[str(c).strip().lower()]: j for j, c in enumerate(rows[hdr])
            if c is not None and str(c).strip().lower() in HEADER_MAP}
    missing = set(SHEET_COLUMNS) - cols.keys()
    if missing:
        sys.exit(f"Sheet is missing column(s): {sorted(missing)}")

    data = [{k: (r[j] if j < len(r) else None) for k, j in cols.items()} for r in rows[hdr + 1:]]
    df = pd.DataFrame(data, columns=SHEET_COLUMNS, dtype=object)
    df.index = df.index + hdr + 2            # index = spreadsheet row number
    df = df.dropna(how="all")                # fully blank rows aren't data
    df["source_row"] = df.index

    title = next((str(r[0]).strip() for r in rows[:hdr] if r and r[0]), None)
    rev = re.search(r"\brev\.?\s*(\w+)", title or "", re.I)
    meta = {"source_file": Path(path).name, "source_title": title,
            "source_rev": rev.group(1) if rev else None}
    return df, meta


# ------------------------------------------------------------ pipeline steps ---

def _text(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def clean_basics(df: pd.DataFrame) -> pd.DataFrame:
    """Trim text, upper-case part numbers, read quantities as numbers.

    A blank or non-numeric qty/min_qty becomes 0 and is flagged (qty_ok=False),
    so it shows up as a warning instead of passing silently as zero stock.
    """
    df = df.copy()
    for c in ["part_no", "description", "for_machine", "location"]:
        df[c] = df[c].map(_text)
    df["part_no"] = df["part_no"].str.upper()
    for c in ["qty", "min_qty"]:
        num = pd.to_numeric(df[c].map(_text), errors="coerce")
        df[f"{c}_ok"] = num.notna()
        df[c] = num.fillna(0).astype(int)
    return df


def parse_cost(v) -> tuple[Decimal | None, str | None]:
    """'$12.50' -> (12.50, None); '$38/spool' -> (38.00, 'spool')."""
    s = _text(v)
    if s is None:
        return None, None
    num, _, per = s.partition("/")
    try:
        return Decimal(re.sub(r"[^\d.]", "", num)).quantize(Decimal("0.01")), (per.strip() or None)
    except InvalidOperation:
        return None, None


def parse_costs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parsed = df["unit_cost_raw"].map(parse_cost)
    df["unit_cost"] = parsed.map(lambda t: t[0])
    df["cost_per"] = parsed.map(lambda t: t[1])
    df["unit_cost_raw"] = df["unit_cost_raw"].map(_text)
    return df


UNIT_DAYS = {"day": 1, "days": 1, "d": 1, "wk": 7, "wks": 7, "week": 7, "weeks": 7, "w": 7,
             "month": 30, "months": 30, "mth": 30, "mths": 30}


def parse_lead_time(v) -> tuple[int | None, int | None, int, str | None]:
    """Return (min_days, max_days, is_local, note).

    '3 days' -> 3,3 | '2-3 weeks (Germany)' -> 14,21,'Germany' | '6-8 weeks!!' -> 42,56
    'local' -> None,None,1
    """
    s = _text(v)
    if s is None:
        return None, None, 0, None
    note = None
    m = re.search(r"\(([^)]*)\)", s)
    if m:
        note = m.group(1).strip() or None
        s = (s[:m.start()] + s[m.end():]).strip()
    s = s.rstrip("!").strip().lower()
    if s == "local":
        return None, None, 1, note
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(\d+(?:\.\d+)?)?\s*([a-z]+)", s)
    if not m or m.group(3) not in UNIT_DAYS:
        return None, None, 0, note
    mult = UNIT_DAYS[m.group(3)]
    lo = round(float(m.group(1)) * mult)
    return lo, (round(float(m.group(2)) * mult) if m.group(2) else lo), 0, note


def parse_lead_times(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parsed = df["lead_time_raw"].map(parse_lead_time)
    for i, c in enumerate(["lead_time_min_days", "lead_time_max_days", "is_local_supply",
                           "lead_time_note"]):
        df[c] = parsed.map(lambda t, i=i: t[i])
    for c in ["lead_time_min_days", "lead_time_max_days"]:
        df[c] = df[c].astype("Int64")
    df["lead_time_raw"] = df["lead_time_raw"].map(_text)
    return df


_INCH = r'(?:inches|inch|in\b|")'
_SIZE_PATTERNS = [  # (regex, unit) - first match wins
    (re.compile(rf"(\d+)\s*-\s*(\d+)/(\d+)\s*{_INCH}", re.I), "in"),  # 1-3/8 inch
    (re.compile(rf"(\d+)/(\d+)\s*{_INCH}", re.I), "in"),              # 3/8"
    (re.compile(rf"(\d+(?:\.\d+)?)\s*{_INCH}", re.I), "in"),          # 85 in, 0.045in, 10in
    (re.compile(r"(\d+(?:\.\d+)?)\s*mm\b", re.I), "mm"),               # 2.4mm
]


def parse_size(desc) -> tuple[float | None, str | None, float | None, str | None]:
    """Find an explicit size in a description: (value, unit, size_mm, matched text).

    Bearing designations like '6205' are NOT decoded - only sizes with a unit.
    """
    s = _text(desc)
    if s is None:
        return None, None, None, None
    for rx, unit in _SIZE_PATTERNS:
        m = rx.search(s)
        if not m:
            continue
        g = [int(x) if x.isdigit() else x for x in m.groups()]
        if len(g) == 3:
            value = float(g[0] + Fraction(g[1], g[2]))
        elif len(g) == 2:
            value = float(Fraction(g[0], g[1]))
        else:
            value = float(g[0])
        mm = value * 25.4 if unit == "in" else value
        return round(value, 4), unit, round(mm, 2), m.group(0).strip()
    return None, None, None, None


def parse_sizes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    parsed = df["description"].map(parse_size)
    for i, c in enumerate(["size_value", "size_unit", "size_mm", "size_raw"]):
        df[c] = parsed.map(lambda t, i=i: t[i])
    return df


def split_rejects(df: pd.DataFrame) -> pd.DataFrame:
    """Add reject_reason and duplicate_of_raw_id. The first row of a part number wins."""
    df = df.copy()
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    reason[df["part_no"].isna()] = "missing_part_no"

    dup_of = pd.Series(pd.NA, index=df.index, dtype="Int64")
    kept: dict[str, int] = {}
    for i, part, raw_id in zip(df.index, df["part_no"], df["raw_id"]):
        if pd.isna(reason[i]):
            if part in kept:
                reason[i] = "duplicate_part_no"
                dup_of[i] = kept[part]
            else:
                kept[part] = raw_id
    reason[reason.isna() & df["description"].isna()] = "missing_description"
    df["reject_reason"] = reason
    df["duplicate_of_raw_id"] = dup_of
    return df


def add_warnings(df: pd.DataFrame) -> pd.DataFrame:
    """Comma-separated notes for rows that load but where something couldn't be read."""
    df = df.copy()
    flags = pd.DataFrame({
        "qty_missing_or_invalid": ~df["qty_ok"],
        "min_qty_missing_or_invalid": ~df["min_qty_ok"],
        "unit_cost_unparsed": df["unit_cost_raw"].notna() & df["unit_cost"].isna(),
        "lead_time_unparsed": (df["lead_time_raw"].notna() & df["lead_time_min_days"].isna()
                               & (df["is_local_supply"] == 0)),
    }, index=df.index)
    df["warnings"] = flags.apply(lambda r: ",".join(r.index[r]) or None, axis=1)
    return df


def split_machines(v) -> list[str]:
    """'CNC-01/02/03' -> ['CNC-01', 'CNC-02', 'CNC-03']; 'ALL' -> ['ALL']."""
    s = _text(v)
    if s is None:
        return []
    out, prefix = [], ""
    for p in (x.strip().upper() for x in s.split("/") if x.strip()):
        if "-" in p:
            prefix = p.rsplit("-", 1)[0] + "-"
            out.append(p)
        else:
            out.append(prefix + p)
    return list(dict.fromkeys(out))


def machine_links(df: pd.DataFrame) -> pd.DataFrame:
    rows = [(p, m) for p, fm in zip(df["part_no"], df["for_machine"]) for m in split_machines(fm)]
    return pd.DataFrame(rows, columns=["part_no", "machine_code"])


def prepare(path: str, run_id: int = 0):
    """Read and clean the sheet. Returns (raw_rows, clean_rows, machine_links, meta)."""
    sheet, meta = read_sheet(path)
    sheet["raw_id"] = run_id * RAW_ID_STRIDE + sheet["source_row"]

    cleaned = (sheet
               .rename(columns={"unit_cost": "unit_cost_raw", "lead_time": "lead_time_raw"})
               .pipe(clean_basics)
               .pipe(parse_costs)
               .pipe(parse_lead_times)
               .pipe(parse_sizes)
               .pipe(split_rejects)
               .pipe(add_warnings))
    rejected = cleaned["reject_reason"].notna()

    raw = sheet.assign(
        run_id=run_id or None, **meta,
        status=np.where(rejected, "rejected", "loaded"),
        reject_reason=cleaned["reject_reason"],
        duplicate_of_raw_id=cleaned["duplicate_of_raw_id"],
        warnings=cleaned["warnings"].where(~rejected),
    )
    for c in SHEET_COLUMNS:  # raw = text exactly as in the sheet
        raw[c] = raw[c].map(_text)
    links = machine_links(cleaned.loc[~rejected])
    clean = cleaned.loc[~rejected, CLEAN_COLUMNS]
    return raw[RAW_COLUMNS], clean, links, meta


# ---------------------------------------------------------------------- load ---

def create_tables(engine, recreate: bool) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:  # DDL commits implicitly in MySQL
        conn.execute(text(RUNS_DDL))
        if recreate:
            conn.execute(text("DROP VIEW IF EXISTS critical_spares_audit"))
            for t in ["critical_spare_machines", "critical_spares", "critical_spares_raw"]:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        for stmt in DDL:
            conn.execute(text(stmt))


def _insert(table: str, columns: list[str]):
    from sqlalchemy import text

    return text(f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})")


def load(engine, raw: pd.DataFrame, clean: pd.DataFrame, links: pd.DataFrame) -> None:
    """Replace all spares data with this sheet, in ONE transaction."""
    from sqlalchemy import text

    with engine.begin() as conn:
        # Cascades: raw -> critical_spares -> critical_spare_machines
        conn.execute(text("DELETE FROM critical_spares_raw"))
        conn.execute(_insert("critical_spares_raw", RAW_COLUMNS), to_records(raw))
        if len(clean):
            conn.execute(_insert("critical_spares", CLEAN_COLUMNS), to_records(clean))
        if len(links):
            conn.execute(_insert("critical_spare_machines", ["part_no", "machine_code"]),
                         to_records(links))


def summary(raw: pd.DataFrame, clean: pd.DataFrame, links: pd.DataFrame) -> Counter:
    stats = Counter({
        "rows_read": len(raw), "parts_loaded": len(clean),
        "rejected": int((raw["status"] == "rejected").sum()), "machine_links": len(links),
        "below_min_qty": int((clean["qty"] < clean["min_qty"]).sum()),
        "size_in_inches": int((clean["size_unit"] == "in").sum()),
        "size_in_mm": int((clean["size_unit"] == "mm").sum()),
        "no_explicit_size": int(clean["size_unit"].isna().sum()),
    })
    stats.update(f"rejected:{r}" for r in raw["reject_reason"].dropna())
    for w in raw["warnings"].dropna():
        stats.update(f"warn:{x}" for x in w.split(","))
    return stats


def print_report(stats: Counter, rev) -> None:
    main = [k for k in stats if ":" not in k]
    print(f"\nRev {rev}: " + ", ".join(f"{k}={stats[k]}" for k in main))
    for prefix, title in [("rejected:", "Rejected rows"), ("warn:", "Loaded with warnings")]:
        items = {k[len(prefix):]: v for k, v in stats.items() if k.startswith(prefix)}
        if items:
            print(f"{title}: " + ", ".join(f"{k}={v}" for k, v in sorted(items.items())))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xlsx")
    ap.add_argument("--recreate", action="store_true",
                    help="drop and recreate the tables (first run, or after a schema change)")
    add_common_args(ap)
    a = ap.parse_args()
    if not Path(a.xlsx).is_file():
        sys.exit(f"File not found: {a.xlsx}")

    if a.dry_run:
        raw, clean, links, meta = prepare(a.xlsx)
        show = ["raw_id", "part_no", "qty", "min_qty", "unit_cost", "cost_per", "size_raw",
                "size_mm", "lead_time_min_days", "lead_time_max_days", "is_local_supply",
                "lead_time_note"]
        with pd.option_context("display.width", 200):
            print(clean[show].to_string(index=False))
            rej = raw[raw["status"] == "rejected"]
            if len(rej):
                print("\nRejected rows:")
                print(rej[["source_row", "part_no", "description", "reject_reason",
                           "duplicate_of_raw_id"]].to_string(index=False))
        print_report(summary(raw, clean, links), meta["source_rev"])
        print("Dry run - nothing written to the database.")
        return

    try:
        cfg = load_db_config(a.env_file)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    engine = cfg.engine()
    print(f"Loading {a.xlsx} into {cfg.describe()}")
    create_tables(engine, a.recreate)
    run_id = start_run(engine, "ingest_spares.py", a.xlsx)
    try:
        raw, clean, links, meta = prepare(a.xlsx, run_id)
        load(engine, raw, clean, links)
    except BaseException as e:
        finish_run(engine, run_id, ok=False, error=f"{type(e).__name__}: {e}")
        print(f"Load failed - nothing changed. Recorded as run {run_id} in ingest_runs.",
              file=sys.stderr)
        raise
    stats = summary(raw, clean, links)
    finish_run(engine, run_id, ok=True, rows_read=stats["rows_read"],
               rows_loaded=stats["parts_loaded"], rows_rejected=stats["rejected"])
    print(f"Recorded as run {run_id} in ingest_runs.")
    print_report(stats, meta["source_rev"])
    print("Done.")


if __name__ == "__main__":
    main()
