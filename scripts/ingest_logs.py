#!/usr/bin/env python3
"""Ingest the vibration/temperature sensor log CSV into MySQL, with lineage.

Three tables, so every cleaned value can be traced back to the file line it came from:

    ingest_runs          one row per load: file, SHA-256, git commit, counts, status
    vibration_temp_raw   every CSV row exactly as in the file (text), with its line
                         number, status (loaded / rejected), reject reason and, for
                         duplicates, the raw_id of the row that was kept
    vibration_temp       cleaned rows only; raw_id links each one to its raw row

    vibration_temp_audit (view) clean + raw + run joined, for citations

Pipeline (each step is a small function you can test on its own):
    read CSV in chunks
      -> parse_timestamps     mixed ISO 8601 / epoch-ms -> one timezone
      -> clean_temperatures   mixed C / F -> temp_c (Celsius only)
      -> clean_values         machine ids, vibration as numbers
      -> split_rejects        rows that can't be loaded, with a reason
      -> load                 one transaction: old data stays if anything fails

Re-loading the same file name replaces that file's rows; other files are kept.

Timezone rule: every timestamp is converted to UTC first; `epoch_ms` stores
UTC epoch milliseconds and `sampled_at` stores Singapore local time (UTC+8).
ISO timestamps without an offset are assumed to be --source-tz (default
Asia/Singapore, where the sensors are).

Usage
    python scripts/ingest_logs.py data/logs/vibration_temp_2026H1.csv --dry-run
    python scripts/ingest_logs.py data/logs/vibration_temp_2026H1.csv --recreate   # first time
    python scripts/ingest_logs.py data/logs/vibration_temp_2026H1.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from db_config import add_common_args, load_db_config, safe_identifier, to_records
from lineage import RUNS_DDL, finish_run, start_run

STORE_TZ = "Asia/Singapore"
REQUIRED_COLUMNS = ["timestamp", "machine_id", "vib_rms_mms", "temp", "temp_unit"]
RAW_COLUMNS = [
    "raw_id", "run_id", "source_file", "source_line", *REQUIRED_COLUMNS,
    "status", "reject_reason", "duplicate_of_raw_id", "warnings",
]
CLEAN_COLUMNS = [
    "raw_id", "machine_id", "sampled_at", "epoch_ms", "vib_rms_mms", "temp_c", "ts_format",
]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS {t}_raw (
        raw_id              BIGINT       NOT NULL PRIMARY KEY,
        run_id              BIGINT       NULL,
        source_file         VARCHAR(255) NOT NULL,
        source_line         INT          NOT NULL COMMENT 'line in the CSV; header is line 1',
        `timestamp`         VARCHAR(64)  NULL COMMENT 'as in file',
        machine_id          VARCHAR(64)  NULL COMMENT 'as in file',
        vib_rms_mms         VARCHAR(64)  NULL COMMENT 'as in file',
        temp                VARCHAR(64)  NULL COMMENT 'as in file',
        temp_unit           VARCHAR(32)  NULL COMMENT 'as in file',
        status              VARCHAR(10)  NOT NULL COMMENT 'loaded | rejected',
        reject_reason       VARCHAR(40)  NULL,
        duplicate_of_raw_id BIGINT       NULL COMMENT 'for duplicates: the raw row that was kept',
        warnings            VARCHAR(255) NULL COMMENT 'loaded with gaps, e.g. missing_temp',
        KEY ix_raw_file (source_file, source_line),
        KEY ix_raw_status (status, reject_reason),
        CONSTRAINT fk_{t}_raw_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
            ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS {t} (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        raw_id      BIGINT       NOT NULL,
        machine_id  VARCHAR(20)  NOT NULL,
        sampled_at  DATETIME     NOT NULL COMMENT 'Singapore local time (UTC+8)',
        epoch_ms    BIGINT       NOT NULL COMMENT 'UTC epoch milliseconds',
        vib_rms_mms DECIMAL(8,2) NULL,
        temp_c      DECIMAL(8,2) NULL COMMENT 'Celsius; NULL if unit unknown',
        ts_format   VARCHAR(12)  NOT NULL COMMENT 'epoch_ms | iso_offset | iso_naive',
        UNIQUE KEY uq_{t}_raw (raw_id),
        UNIQUE KEY uq_{t}_machine_time (machine_id, epoch_ms),
        KEY ix_{t}_machine_sampled (machine_id, sampled_at),
        CONSTRAINT fk_{t}_raw FOREIGN KEY (raw_id) REFERENCES {t}_raw (raw_id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE OR REPLACE VIEW {t}_audit AS
    SELECT c.id, c.machine_id, c.sampled_at, c.epoch_ms, c.vib_rms_mms, c.temp_c,
           r.raw_id, r.source_file, r.source_line,
           r.`timestamp` AS timestamp_raw, r.temp AS temp_raw, r.temp_unit AS temp_unit_raw,
           r.warnings, r.run_id, u.file_sha256, u.started_at AS loaded_at
    FROM {t} c
    JOIN {t}_raw r ON r.raw_id = c.raw_id
    LEFT JOIN ingest_runs u ON u.run_id = r.run_id
    """,
]


# ------------------------------------------------------------ pipeline steps ---

def _bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


def parse_timestamps(df: pd.DataFrame, source_tz: str = STORE_TZ) -> pd.DataFrame:
    """Parse mixed epoch-ms and ISO 8601 timestamps into one timezone.

    Adds: timestamp_raw, ts_format, epoch_ms (UTC), sampled_at (Singapore, naive).
    Unparseable values get ts_format='unknown' and NaT, and are rejected later.
    """
    df = df.copy()
    raw = df["timestamp"].astype("string").str.strip()
    is_epoch = _bool(raw.str.fullmatch(r"\d{11,14}"))
    has_offset = _bool(raw.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True))
    is_aware = ~is_epoch & has_offset
    is_naive = ~is_epoch & ~has_offset & _bool(raw.str.len() > 0)

    utc = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    if is_epoch.any():
        utc[is_epoch] = pd.to_datetime(raw[is_epoch].astype("int64"), unit="ms", utc=True)
    if is_aware.any():
        utc[is_aware] = pd.to_datetime(raw[is_aware], utc=True, format="ISO8601", errors="coerce")
    if is_naive.any():
        local = pd.to_datetime(raw[is_naive], format="ISO8601", errors="coerce")
        utc[is_naive] = local.dt.tz_localize(
            source_tz, ambiguous="NaT", nonexistent="NaT"
        ).dt.tz_convert("UTC")

    fmt = np.select([is_epoch, is_aware, is_naive], ["epoch_ms", "iso_offset", "iso_naive"],
                    default="unknown")
    df["ts_format"] = np.where(utc.isna(), "unknown", fmt)
    df["timestamp_raw"] = raw
    df["epoch_ms"] = ((utc - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)).astype("Int64")
    df["sampled_at"] = utc.dt.tz_convert(STORE_TZ).dt.tz_localize(None)
    return df.drop(columns=["timestamp"])


def normalise_unit(unit: pd.Series) -> pd.Series:
    """'°C', 'degC', 'celsius', ' c ' -> 'C'; same for F. Anything else -> <NA>."""
    u = (unit.astype("string").str.upper()
         .str.replace(r"[°º\s]|DEG(REES?)?", "", regex=True)
         .replace({"CELSIUS": "C", "CENTIGRADE": "C", "FAHRENHEIT": "F"}))
    return u.where(u.isin(["C", "F"]))


def clean_temperatures(df: pd.DataFrame) -> pd.DataFrame:
    """Convert every reading to Celsius in temp_c. Unknown units -> NULL.

    The original reading and unit stay in `temp` / `temp_unit` for audit.
    """
    df = df.copy()
    temp = pd.to_numeric(df["temp"], errors="coerce")
    unit = normalise_unit(df["temp_unit"])
    is_c, is_f = _bool(unit == "C"), _bool(unit == "F")

    df["temp"] = temp
    df["temp_c"] = np.where(is_c, temp, np.where(is_f, (temp - 32) * 5 / 9, np.nan)).round(2)
    df["unit_ok"] = is_c | is_f
    return df


def clean_values(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["machine_id"] = df["machine_id"].astype("string").str.strip().str.upper()
    df.loc[df["machine_id"] == "", "machine_id"] = pd.NA
    df["vib_rms_mms"] = pd.to_numeric(df["vib_rms_mms"], errors="coerce").round(2)
    return df


def split_rejects(df: pd.DataFrame, seen: dict) -> pd.DataFrame:
    """Add reject_reason and duplicate_of_raw_id columns.

    `seen` maps (machine_id, epoch_ms) -> raw_id of the row kept, across chunks
    (and, for a real load, across other files already in the table).
    """
    df = df.copy()
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    reason[df["ts_format"] == "unknown"] = "bad_timestamp"
    reason[reason.isna() & df["machine_id"].isna()] = "missing_machine_id"

    dup_of = pd.Series(pd.NA, index=df.index, dtype="Int64")
    for i, key, raw_id in zip(df.index, zip(df["machine_id"], df["epoch_ms"]), df["raw_id"]):
        if pd.isna(reason[i]):
            if key in seen:
                reason[i] = "duplicate_reading"
                dup_of[i] = seen[key]
            else:
                seen[key] = raw_id
    df["reject_reason"] = reason
    df["duplicate_of_raw_id"] = dup_of
    return df


def add_warnings(df: pd.DataFrame) -> pd.DataFrame:
    """Comma-separated notes for rows that load but have gaps."""
    df = df.copy()
    flags = pd.DataFrame({
        "unknown_temp_unit": ~df["unit_ok"] & df["temp"].notna(),
        "missing_temp": df["temp"].isna(),
        "missing_vibration": df["vib_rms_mms"].isna(),
    })
    df["warnings"] = flags.apply(lambda r: ",".join(r.index[r]) or None, axis=1)
    return df


def process(csv_path: str, chunk_size: int, source_tz: str, stats: Counter,
            seen: dict | None = None, first_raw_id: int = 1, run_id: int | None = None):
    """Yield (raw_rows, clean_rows) per chunk and count everything worth reporting."""
    seen = {} if seen is None else seen
    source_file = Path(csv_path).name
    offset = 0
    reader = pd.read_csv(csv_path, chunksize=chunk_size, dtype=str, keep_default_na=False,
                         na_values=[""])
    for chunk in reader:
        missing = set(REQUIRED_COLUMNS) - set(chunk.columns)
        if missing:
            sys.exit(f"CSV is missing column(s): {sorted(missing)}")
        n = len(chunk)
        chunk = chunk[REQUIRED_COLUMNS].copy()
        chunk.index = range(offset, offset + n)
        chunk["raw_id"] = range(first_raw_id + offset, first_raw_id + offset + n)
        chunk["source_line"] = range(offset + 2, offset + 2 + n)  # header is line 1
        offset += n

        cleaned = (chunk
                   .pipe(parse_timestamps, source_tz=source_tz)
                   .pipe(clean_temperatures)
                   .pipe(clean_values)
                   .pipe(split_rejects, seen=seen)
                   .pipe(add_warnings))
        rejected = cleaned["reject_reason"].notna()

        raw = chunk.assign(
            run_id=run_id, source_file=source_file,
            status=np.where(rejected, "rejected", "loaded"),
            reject_reason=cleaned["reject_reason"],
            duplicate_of_raw_id=cleaned["duplicate_of_raw_id"],
            warnings=cleaned["warnings"].where(~rejected),
        )[RAW_COLUMNS]
        clean = cleaned.loc[~rejected, CLEAN_COLUMNS]

        stats["rows_read"] += n
        stats["rows_loaded"] += len(clean)
        stats["rows_rejected"] += int(rejected.sum())
        stats.update(f"ts_format:{f}" for f in cleaned["ts_format"])
        stats.update(f"temp_unit:{u}" for u in chunk["temp_unit"].fillna("<blank>"))
        stats.update(f"rejected:{r}" for r in cleaned.loc[rejected, "reject_reason"])
        for w in cleaned.loc[~rejected, "warnings"].dropna():
            stats.update(f"warn:{x}" for x in w.split(","))
        yield raw, clean


# ---------------------------------------------------------------------- load ---

def create_tables(engine, table: str, recreate: bool) -> None:
    from sqlalchemy import text

    # DDL commits implicitly in MySQL, so it runs before the data transaction.
    with engine.begin() as conn:
        conn.execute(text(RUNS_DDL))
        if recreate:
            conn.execute(text(f"DROP VIEW IF EXISTS {table}_audit"))
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {table}_raw"))
        for stmt in DDL:
            conn.execute(text(stmt.format(t=table)))


def load(engine, table: str, csv_path: str, chunk_size: int, source_tz: str,
         stats: Counter, run_id: int) -> None:
    """Replace this file's rows in ONE transaction; other files' rows are kept."""
    from sqlalchemy import text

    source_file = Path(csv_path).name
    insert_raw = text(f"INSERT INTO {table}_raw ({', '.join(f'`{c}`' for c in RAW_COLUMNS)}) "
                      f"VALUES ({', '.join(':' + c for c in RAW_COLUMNS)})")
    insert_clean = text(f"INSERT INTO {table} ({', '.join(CLEAN_COLUMNS)}) "
                        f"VALUES ({', '.join(':' + c for c in CLEAN_COLUMNS)})")

    with engine.begin() as conn:
        # Deleting raw rows cascades to their clean rows.
        conn.execute(text(f"DELETE FROM {table}_raw WHERE source_file = :f"), {"f": source_file})
        first_raw_id = conn.execute(text(f"SELECT COALESCE(MAX(raw_id), 0) + 1 FROM {table}_raw")
                                    ).scalar_one()
        # Readings already loaded from OTHER files count as duplicates too.
        seen = {(m, int(e)): r for m, e, r in conn.execute(text(
            f"SELECT machine_id, epoch_ms, raw_id FROM {table}"))}

        for n, (raw, clean) in enumerate(process(csv_path, chunk_size, source_tz, stats, seen,
                                                 first_raw_id, run_id), 1):
            conn.execute(insert_raw, to_records(raw))
            if len(clean):
                conn.execute(insert_clean, to_records(clean))
            print(f"  chunk {n}: {len(raw)} raw rows, {len(clean)} clean rows")


def print_report(stats: Counter) -> None:
    print(f"\nRows read: {stats['rows_read']}   loaded: {stats['rows_loaded']}   "
          f"rejected: {stats['rows_rejected']}")
    for prefix, title in [("ts_format:", "Timestamp formats"), ("temp_unit:", "Temperature units"),
                          ("rejected:", "Rejected rows"), ("warn:", "Loaded with gaps")]:
        items = {k[len(prefix):]: v for k, v in stats.items() if k.startswith(prefix) and v}
        if items:
            print(f"{title}: " + ", ".join(f"{k}={v}" for k, v in sorted(items.items()))
                  + f" (total {sum(items.values())})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=os.getenv("CSV_FILE", "data/logs/vibration_temp_2026H1.csv"))
    ap.add_argument("--table", default="vibration_temp")
    ap.add_argument("--chunk-size", type=int, default=10_000)
    ap.add_argument("--source-tz", default=STORE_TZ,
                    help="timezone for ISO timestamps with no offset (default %(default)s)")
    ap.add_argument("--rejects", help="also write rejected rows + reason to this CSV")
    ap.add_argument("--recreate", action="store_true",
                    help="drop and recreate the tables (first run, or after a schema change)")
    add_common_args(ap)
    a = ap.parse_args()

    if not Path(a.csv).is_file():
        sys.exit(f"CSV not found: {a.csv}")
    table = safe_identifier(a.table)
    stats: Counter = Counter()
    rejects: list[pd.DataFrame] = []

    if a.dry_run:
        for i, (raw, clean) in enumerate(process(a.csv, a.chunk_size, a.source_tz, stats)):
            if i == 0:
                print("Clean rows (first 5):")
                print(clean.head().to_string(index=False))
            rejects.append(raw[raw["status"] == "rejected"])
        rej = pd.concat(rejects) if rejects else pd.DataFrame()
        if len(rej):
            print("\nRejected rows (first 5):")
            print(rej.head()[["source_line", *REQUIRED_COLUMNS, "reject_reason",
                              "duplicate_of_raw_id"]].to_string(index=False))
    else:
        try:
            cfg = load_db_config(a.env_file)
        except (ValueError, FileNotFoundError) as e:
            sys.exit(str(e))
        engine = cfg.engine()
        print(f"Loading {a.csv} into {cfg.describe()} tables {table}, {table}_raw")
        create_tables(engine, table, a.recreate)
        run_id = start_run(engine, "ingest_logs.py", a.csv)
        try:
            load(engine, table, a.csv, a.chunk_size, a.source_tz, stats, run_id)
        except BaseException as e:
            finish_run(engine, run_id, ok=False, rows_read=stats["rows_read"],
                       error=f"{type(e).__name__}: {e}")
            print(f"Load failed - nothing changed. Recorded as run {run_id} in ingest_runs.",
                  file=sys.stderr)
            raise
        finish_run(engine, run_id, ok=True, rows_read=stats["rows_read"],
                   rows_loaded=stats["rows_loaded"], rows_rejected=stats["rows_rejected"])
        print(f"Recorded as run {run_id} in ingest_runs.")
        if a.rejects:
            from sqlalchemy import text
            with engine.connect() as conn:
                rej = pd.read_sql(text(f"SELECT * FROM {table}_raw WHERE status='rejected' "
                                       f"AND run_id=:r"), conn, params={"r": run_id})
            rejects = [rej]

    print_report(stats)
    if a.rejects and rejects and len(pd.concat(rejects)):
        Path(a.rejects).parent.mkdir(parents=True, exist_ok=True)
        pd.concat(rejects).to_csv(a.rejects, index=False)
        print(f"Rejected rows written to {a.rejects}")
    print("Dry run - nothing written to the database." if a.dry_run else "Done.")


if __name__ == "__main__":
    main()