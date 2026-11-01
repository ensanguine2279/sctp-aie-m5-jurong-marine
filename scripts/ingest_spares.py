"""Ingest the Critical Spares spreadsheet into MySQL.

Pipeline (each step is a small function you can test on its own):
    read_sheet           find the header row, read the title/revision
      -> parse_costs        '$38/spool' -> 38.00 + cost_per='spool'
      -> parse_lead_times   '2-3 weeks (Germany)' -> 14-21 days + note
      -> parse_sizes        '85 in', '1-3/8 inch', '2.4mm' -> size_mm
      -> split_rejects      blank or duplicate part numbers
      -> machine_links      'CNC-01/02/03' -> CNC-01, CNC-02, CNC-03
      -> load               upsert by part_no in one transaction

Lineage: raw text is kept next to every parsed value, and each part records
the sheet row it came from (`source_row`) and the load that last wrote it
(`run_id` -> ingest_runs: file SHA-256, git commit, time). A part whose run_id
is older than the latest spares run was not in the latest sheet.

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
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import openpyxl
import pandas as pd

from db_config import add_common_args, load_db_config, to_records
from lineage import RUNS_DDL, finish_run, start_run

HEADER_MAP = {
    "part no": "part_no", "description": "description", "for machine": "for_machine",
    "qty": "qty", "min qty": "min_qty", "unit cost": "unit_cost_raw",
    "lead time": "lead_time_raw", "location": "location",
}

DDL = [
    """
    CREATE TABLE IF NOT EXISTS critical_spares (
        part_no            VARCHAR(50)   NOT NULL PRIMARY KEY,
        description        VARCHAR(255)  NOT NULL,
        for_machine        VARCHAR(100)  NULL COMMENT 'as written in the sheet',
        qty                INT           NOT NULL DEFAULT 0,
        min_qty            INT           NOT NULL DEFAULT 0,
        below_min          TINYINT(1)    AS (qty < min_qty) STORED,
        unit_cost          DECIMAL(12,2) NULL COMMENT 'currency not stated in sheet ($)',
        cost_per           VARCHAR(30)   NULL COMMENT 'e.g. spool, when not per piece',
        unit_cost_raw      VARCHAR(50)   NULL,
        size_value         DECIMAL(10,4) NULL COMMENT 'size as written, in size_unit',
        size_unit          VARCHAR(5)    NULL COMMENT 'mm | in',
        size_mm            DECIMAL(10,2) NULL COMMENT 'size converted to mm',
        size_raw           VARCHAR(30)   NULL,
        lead_time_min_days INT           NULL,
        lead_time_max_days INT           NULL,
        is_local_supply    TINYINT(1)    NOT NULL DEFAULT 0,
        lead_time_note     VARCHAR(100)  NULL COMMENT 'e.g. supplier country',
        lead_time_raw      VARCHAR(100)  NULL,
        location           VARCHAR(50)   NULL,
        source_file        VARCHAR(255)  NULL,
        source_row         INT           NULL COMMENT 'row number in the spreadsheet',
        source_rev         VARCHAR(20)   NULL,
        source_title       VARCHAR(255)  NULL,
        run_id             BIGINT        NULL COMMENT 'load that last wrote this row',
        loaded_at          DATETIME      NOT NULL,
        CONSTRAINT fk_spares_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
            ON DELETE SET NULL
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
]

SPARE_COLUMNS = [
    "part_no", "description", "for_machine", "qty", "min_qty", "unit_cost", "cost_per",
    "unit_cost_raw", "size_value", "size_unit", "size_mm", "size_raw", "lead_time_min_days",
    "lead_time_max_days", "is_local_supply", "lead_time_note", "lead_time_raw", "location",
    "source_file", "source_row", "source_rev", "source_title", "run_id", "loaded_at",
]
UPSERT_SPARE = (
    f"INSERT INTO critical_spares ({', '.join(SPARE_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in SPARE_COLUMNS)}) "
    "ON DUPLICATE KEY UPDATE "
    + ", ".join(f"{c}=VALUES({c})" for c in SPARE_COLUMNS if c != "part_no")
)


# ------------------------------------------------------------------- reading ---

def read_sheet(path: str) -> tuple[pd.DataFrame, dict]:
    """Return (rows as text, metadata). The header row is found by 'Part No'."""
    ws = openpyxl.load_workbook(path, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    hdr = next((i for i, r in enumerate(rows)
                if any(str(c).strip().lower() == "part no" for c in r if c is not None)), None)
    if hdr is None:
        sys.exit("No header row with 'Part No' found.")

    cols = {HEADER_MAP[str(c).strip().lower()]: j for j, c in enumerate(rows[hdr])
            if c is not None and str(c).strip().lower() in HEADER_MAP}
    missing = set(HEADER_MAP.values()) - cols.keys()
    if missing:
        sys.exit(f"Sheet is missing column(s): {sorted(missing)}")

    data = [{k: (r[j] if j < len(r) else None) for k, j in cols.items()} for r in rows[hdr + 1:]]
    df = pd.DataFrame(data, columns=list(HEADER_MAP.values()))
    df = df.dropna(how="all")
    df.index = df.index + hdr + 2  # index = spreadsheet row number, for error messages
    df["source_row"] = df.index

    title = next((str(r[0]).strip() for r in rows[:hdr] if r and r[0]), None)
    rev = re.search(r"\brev\.?\s*(\w+)", title or "", re.I)
    meta = {"source_file": Path(path).name, "source_title": title,
            "source_rev": rev.group(1) if rev else None,
            "loaded_at": datetime.now().replace(microsecond=0)}
    return df, meta


# ------------------------------------------------------------ pipeline steps ---

def _text(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def clean_basics(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ["part_no", "description", "for_machine", "location"]:
        df[c] = df[c].map(_text)
    df["part_no"] = df["part_no"].str.upper()
    for c in ["qty", "min_qty"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
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


def split_rejects(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    reason[df["part_no"].isna()] = "missing_part_no"
    reason[reason.isna() & df.duplicated("part_no", keep="first")] = "duplicate_part_no"
    reason[reason.isna() & df["description"].isna()] = "missing_description"
    return df[reason.isna()], df[reason.notna()].assign(reason=reason[reason.notna()])


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


# ---------------------------------------------------------------------- load ---

def create_tables(engine, recreate: bool) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:  # DDL commits implicitly in MySQL
        conn.execute(text(RUNS_DDL))
        if recreate:
            conn.execute(text("DROP TABLE IF EXISTS critical_spare_machines"))
            conn.execute(text("DROP TABLE IF EXISTS critical_spares"))
        for stmt in DDL:
            conn.execute(text(stmt))


def load(engine, spares: pd.DataFrame, links: pd.DataFrame) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:  # data: all or nothing
        conn.execute(text(UPSERT_SPARE), to_records(spares[SPARE_COLUMNS]))
        conn.execute(text("DELETE FROM critical_spare_machines WHERE part_no = :p"),
                     [{"p": p} for p in spares["part_no"]])
        if len(links):
            conn.execute(text("INSERT INTO critical_spare_machines (part_no, machine_code) "
                              "VALUES (:part_no, :machine_code)"), to_records(links))


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

    raw, meta = read_sheet(a.xlsx)
    cleaned = (raw
               .pipe(clean_basics)
               .pipe(parse_costs)
               .pipe(parse_lead_times)
               .pipe(parse_sizes)
               .assign(**meta))
    spares, rejects = split_rejects(cleaned)
    links = machine_links(spares)

    for row, r in rejects.iterrows():
        print(f"SKIPPED sheet row {row}: {r['reason']} ({r['part_no']})", file=sys.stderr)

    if a.dry_run:
        show = ["source_row", "part_no", "qty", "min_qty", "unit_cost", "cost_per", "size_raw", "size_mm",
                "lead_time_min_days", "lead_time_max_days", "is_local_supply", "lead_time_note"]
        with pd.option_context("display.width", 200):
            print(spares[show].to_string(index=False))
    else:
        try:
            cfg = load_db_config(a.env_file)
        except (ValueError, FileNotFoundError) as e:
            sys.exit(str(e))
        engine = cfg.engine()
        print(f"Loading {a.xlsx} into {cfg.describe()}")
        create_tables(engine, a.recreate)
        run_id = start_run(engine, "ingest_spares.py", a.xlsx)
        spares = spares.assign(run_id=run_id)
        rows_read = len(spares) + len(rejects)
        try:
            load(engine, spares, links)
        except BaseException as e:
            finish_run(engine, run_id, ok=False, rows_read=rows_read,
                       error=f"{type(e).__name__}: {e}")
            print(f"Load failed - nothing changed. Recorded as run {run_id} in ingest_runs.",
                  file=sys.stderr)
            raise
        finish_run(engine, run_id, ok=True, rows_read=rows_read, rows_loaded=len(spares),
                   rows_rejected=len(rejects))
        print(f"Recorded as run {run_id} in ingest_runs.")

    stats = Counter({
        "parts": len(spares), "machine_links": len(links), "rejected": len(rejects),
        "below_min_qty": int((spares["qty"] < spares["min_qty"]).sum()),
        "size_in_inches": int((spares["size_unit"] == "in").sum()),
        "size_in_mm": int((spares["size_unit"] == "mm").sum()),
        "no_explicit_size": int(spares["size_unit"].isna().sum()),
        "lead_time_unparsed": int((spares["lead_time_raw"].notna()
                                   & spares["lead_time_min_days"].isna()
                                   & (spares["is_local_supply"] == 0)).sum()),
    })
    print(f"\nRev {meta['source_rev']}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    print("Dry run - nothing written to the database." if a.dry_run else "Done.")


if __name__ == "__main__":
    main()
