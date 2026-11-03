"""Profile the full sensor log for the trap list (L-items in docs/trap_list.md).

Usage:  python scripts/audit_sensor_csv.py data/logs/vibration_temp_2026H1.csv
Prints evidence with CSV line numbers (header = line 1). Read-only, no database.
"""
import re
import sys
from collections import Counter

import pandas as pd

KNOWN_MACHINES = {"CNC-01", "CNC-02", "CNC-03", "MILL-01", "WELD-01", "WELD-02", "CRN-01",
                  "CRN-02", "CMP-01", "BLAST-01", "PMP-T01", "LTH-01"}


def ts_format(v: str) -> str:
    v = v.strip()
    if re.fullmatch(r"\d{11,14}", v):
        return "epoch_ms"
    if re.fullmatch(r"\d{9,10}", v):
        return "epoch_s"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})", v):
        return "iso_offset"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", v):
        return "iso_naive"
    return "other" if v else "blank"


def to_utc(v: str, fmt: str):
    if fmt == "epoch_ms":
        return pd.to_datetime(int(v), unit="ms", utc=True)
    if fmt == "epoch_s":
        return pd.to_datetime(int(v), unit="s", utc=True)
    if fmt == "iso_offset":
        return pd.to_datetime(v, utc=True)
    if fmt == "iso_naive":
        return pd.Timestamp(v).tz_localize("Asia/Singapore").tz_convert("UTC")
    return pd.NaT


def examples(mask, df, n=3, cols=None):
    sub = df[mask].head(n)
    cols = cols or ["line", "timestamp", "machine_id", "vib_rms_mms", "temp", "temp_unit"]
    return "; ".join(f"line {r.line}: " + ",".join(str(getattr(r, c)) for c in cols[1:])
                     for r in sub.itertuples())


def main(path: str) -> None:
    raw = open(path, "rb").read()
    crlf, bom = raw.count(b"\r\n"), raw.startswith(b"\xef\xbb\xbf")
    print(f"File: {path}  bytes={len(raw)}  CRLF lines={crlf}  BOM={bom}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.insert(0, "line", range(2, len(df) + 2))
    print(f"Rows: {len(df)}  Columns: {list(df.columns[1:])}")

    # Timestamps
    df["ts_format"] = df["timestamp"].map(ts_format)
    print("\n[Timestamps] formats:", dict(Counter(df["ts_format"])))
    for f in ("iso_naive", "epoch_s", "other", "blank"):
        m = df["ts_format"] == f
        if m.any():
            print(f"  {f}: {examples(m, df)}")
    zm = df["timestamp"].str.strip().str.endswith("Z")
    if zm.any():
        print(f"  ends in Z (UTC): {zm.sum()} e.g. {examples(zm, df)}")
    df["utc"] = [to_utc(v, f) if f not in ("other", "blank") else pd.NaT
                 for v, f in zip(df["timestamp"], df["ts_format"])]
    ok = df["utc"].notna()
    print(f"  range (SGT): {df.loc[ok, 'utc'].min().tz_convert('Asia/Singapore')} -> "
          f"{df.loc[ok, 'utc'].max().tz_convert('Asia/Singapore')}")

    # Machine ids
    mid = df["machine_id"]
    print("\n[Machine ids] as written:", dict(Counter(mid)))
    norm = mid.str.strip().str.upper()
    unknown = sorted(set(norm) - KNOWN_MACHINES)
    print(f"  not in CMMS after trim/upper: {unknown}")
    print(f"  needed trim/upper: {(mid != norm).sum()}  blank: {(norm == '').sum()}")
    df["mid"] = norm

    # Temperature units and values
    print("\n[Temp units] as written:", dict(Counter(df["temp_unit"])))
    unit_by_machine = df.groupby("mid")["temp_unit"].agg(lambda s: dict(Counter(s)))
    print("  units per machine:")
    for m, u in unit_by_machine.items():
        print(f"    {m}: {u}")
    t = pd.to_numeric(df["temp"], errors="coerce")
    u = df["temp_unit"].str.strip().str.lower().str.replace("°", "").str.replace("deg", "")
    is_f = u.isin(["f", "fahrenheit"])
    is_c = u.isin(["c", "celsius", "centigrade"])
    df["temp_c"] = t.where(~is_f, (t - 32) * 5 / 9).where(is_f | is_c)
    print(f"  non-numeric temp: {t.isna().sum()} {examples(t.isna(), df)}")
    print(f"  unknown/blank unit: {(~(is_f | is_c)).sum()} {examples(~(is_f | is_c), df)}")
    print("  temp_c describe:", df["temp_c"].describe().round(2).to_dict())
    for lo, hi, label in ((-50, 0, "<0 C"), (150, 10_000, ">150 C")):
        m = df["temp_c"].between(lo, hi, inclusive="left")
        if m.any():
            print(f"  {label}: {m.sum()} {examples(m, df)}")
    # Celsius values that look like Fahrenheit (and vice versa)
    m = is_c & (t > 100)
    if m.any():
        print(f"  logged C but >100: {m.sum()} {examples(m, df)}")

    # Vibration
    v = pd.to_numeric(df["vib_rms_mms"], errors="coerce")
    print("\n[Vibration] describe:", v.describe().round(3).to_dict())
    print(f"  non-numeric/blank: {v.isna().sum()} {examples(v.isna(), df)}")
    for label, m in (("negative", v < 0), ("zero", v == 0), (">20 mm/s", v > 20)):
        if m.any():
            print(f"  {label}: {m.sum()} {examples(m, df)}")
    df["vib"] = v

    # Duplicates: same machine, same instant (any format)
    key = df.loc[ok, ["mid", "utc"]]
    dup = key.duplicated(keep=False)
    print(f"\n[Duplicates] same machine+instant: {dup.sum()} rows in "
          f"{key[dup].drop_duplicates().shape[0]} groups")
    if dup.any():
        d = df.loc[ok][dup.values].sort_values(["mid", "utc"]).head(6)
        print("  e.g. " + "; ".join(f"line {r.line} {r.mid} {r.timestamp} {r.temp}{r.temp_unit}"
                                     for r in d.itertuples()))
        conflict = (df.loc[ok][dup.values].groupby(["mid", "utc"])
                    .agg(n_vib=("vib", "nunique"), n_temp=("temp_c", "nunique")))
        print(f"  groups with differing values: {((conflict.n_vib > 1) | (conflict.n_temp > 1)).sum()}")
    exact = df.duplicated(subset=["timestamp", "machine_id", "vib_rms_mms", "temp", "temp_unit"])
    print(f"  exact duplicate lines: {exact.sum()}")

    # Sampling interval and gaps per machine
    print("\n[Sampling / gaps] per machine (minutes):")
    clean = df.loc[ok].drop_duplicates(["mid", "utc"]).sort_values(["mid", "utc"])
    for m, g in clean.groupby("mid"):
        step = g["utc"].diff().dt.total_seconds().div(60).dropna()
        if step.empty:
            continue
        mode = step.mode().iloc[0]
        gaps = g.loc[step[step > 3 * mode].index]
        prev = g["utc"].shift().loc[gaps.index]
        big = sorted(zip(step[gaps.index], prev, gaps["utc"], gaps["line"]), reverse=True)[:3]
        print(f"  {m}: n={len(g)} typical={mode:g} min  irregular={int((step != mode).sum())}  "
              f"gaps>{3 * mode:g}min={len(gaps)}  total_gap_h={step[step > 3 * mode].sum() / 60:.1f}")
        for mins, a, b, line in big:
            print(f"      {mins / 60:.1f} h gap: {a.tz_convert('Asia/Singapore')} -> "
                  f"{b.tz_convert('Asia/Singapore')} (line {line})")
        order = g.sort_values("line")["utc"].is_monotonic_increasing
        if not order:
            print("      file order is not time order for this machine")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/logs/vibration_temp_2026H1.csv")