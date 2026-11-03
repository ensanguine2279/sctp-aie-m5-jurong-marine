#!/usr/bin/env python3
"""Run the whole pipeline and write a Markdown report of the results.

Runs, in order, each as its own process exactly as you would by hand:

    ingest_logs.py      sensor log CSV
    ingest_spares.py    critical spares sheet
    ingest_cmms.py      CMMS SQL dump
    relationships.py    link the sources, refresh the views and the ER diagram
    agent_user.py       optional (--agent): check or set up the app's read-only user

Each script's output still streams to the terminal. The report (reports/pipeline_<time>.md)
has one table row per step: status, time, run_id, rows read / loaded / rejected, plus the
input files' SHA-256, the run summaries, how the sources linked up, and the code version.

Where the numbers come from: for a real load, from each run's row in ingest_runs (the
database's own record), and the report checks each file's SHA-256 against the one
recorded. For a dry run, from the scripts' printed summaries.

PDPA: the report never copies a script's full output, only its summary lines (counts and
categories), because a dry run prints sample rows that can include employee names. The
last lines of a failed step's error output are included. --save-log keeps the full output
in reports/logs/<report name>.log, written as the run goes; that folder gets its own
.gitignore so logs never reach git. ingest_cmms.py masks employee names before printing
them (last 4 characters only), so the log holds only the masked form.

The first failed step stops the run (the steps after it are listed as not run), so
relationships.py never links half-loaded data; --keep-going runs them anyway. Ctrl+C stops
the current step and still writes the report.

Usage
    python scripts/pipeline.py --dry-run                          # nothing written to the database
    python scripts/pipeline.py --recreate                         # first load
    python scripts/pipeline.py                                    # later loads
    python scripts/pipeline.py --foreign-keys --agent check       # drop/re-add FKs, check the app user
    python scripts/pipeline.py --only cmms,relationships          # some steps only
    python scripts/pipeline.py --save-log                         # keep the full on-screen log too
    python scripts/pipeline.py --logs data/logs/vibration_temp_sample.csv --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from db_config import add_common_args, load_db_config
from lineage import file_sha256, git_commit

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ERROR_TAIL = 20  # lines of a failed step's error output kept in the report


# -------------------------------------------------------------------- steps ---

@dataclass(frozen=True)
class Step:
    key: str
    script: str
    title: str


STEPS = [
    Step("logs", "ingest_logs.py", "Sensor logs"),
    Step("spares", "ingest_spares.py", "Critical spares"),
    Step("cmms", "ingest_cmms.py", "CMMS dump"),
    Step("relationships", "relationships.py", "Links between sources"),
    Step("agent_user", "agent_user.py", "App user (PDPA)"),
]
BY_KEY = {s.key: s for s in STEPS}
INGEST_KEYS = ("logs", "spares", "cmms")
DROP_FKS = Step("drop_fks", "relationships.py", "Drop foreign keys before loading")
DEFAULT_INPUTS = {
    "logs": "data/logs/vibration_temp_2026H1.csv",
    "spares": "data/tabular/critical_spares.xlsx",
    "cmms": "data/sql/jurongmarine_cmms.sql",
}


@dataclass
class Planned:
    step: Step
    args: list[str]               # arguments after the script path
    input: str | None = None      # input file, relative to the repo root where possible

    def display(self) -> str:
        return " ".join(["python", f"scripts/{self.step.script}", *self.args])


def rel(path: str | Path, base: Path | None = None) -> str:
    """Path relative to the repo root if it's inside it, else absolute. `base` resolves
    relative paths (the user's working directory for paths typed on the command line)."""
    p = Path(path)
    p = (base / p if base and not p.is_absolute() else ROOT / p if not p.is_absolute() else p)
    p = p.resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def parse_only(value: str | None) -> list[str] | None:
    if not value:
        return None
    keys = [k.strip() for k in value.split(",") if k.strip()]
    bad = [k for k in keys if k not in BY_KEY]
    if bad:
        raise ValueError(f"Unknown step(s) {', '.join(bad)}; choose from {', '.join(BY_KEY)}")
    return keys


def build_plan(*, inputs: dict[str, str], dry_run: bool = False, recreate: bool = False,
               foreign_keys: bool = False, mermaid: str | None = None,
               agent: str | None = None, env_file: str | None = None,
               only: list[str] | None = None) -> list[Planned]:
    """The commands to run, in order. `only` limits the steps (always in pipeline order)."""
    common = (["--dry-run"] if dry_run else []) + (["--env-file", env_file] if env_file else [])
    if only and "agent_user" in only and not agent:
        agent = "check"
    keys = only or [*INGEST_KEYS, "relationships", *(["agent_user"] if agent else [])]
    plan: list[Planned] = []
    loads = [k for k in INGEST_KEYS if k in keys]
    if foreign_keys and loads and not dry_run:
        # A reload deletes rows the foreign keys protect, so they go first and come back last.
        plan.append(Planned(DROP_FKS, ["--drop-foreign-keys", *common]))
    for k in loads:
        plan.append(Planned(BY_KEY[k], [inputs[k], *(["--recreate"] if recreate else []),
                                        *common], inputs[k]))
    if "relationships" in keys:
        args = (["--foreign-keys"] if foreign_keys and not dry_run else []) + common
        plan.append(Planned(BY_KEY["relationships"],
                            args + (["--mermaid", mermaid] if mermaid else [])))
    if "agent_user" in keys and agent:
        mode = ["--dry-run"] if dry_run else (["--check"] if agent == "check" else [])
        plan.append(Planned(BY_KEY["agent_user"],
                            mode + (["--env-file", env_file] if env_file else [])))
    return plan


# ------------------------------------------------------------------ running ---

@dataclass
class Outcome:
    exit_code: int | None
    stdout: list[str]
    stderr: list[str]
    seconds: float
    interrupted: bool = False


STDERR_PREFIX = "[stderr] "  # marks error-stream lines in the saved log


def run_command(cmd: list[str], cwd: Path = ROOT, echo: bool = True, log=None) -> Outcome:
    """Run cmd, echoing its output live and keeping it. Ctrl+C stops it cleanly.

    With `log` (an open text file), every line also goes there as it arrives, error-stream
    lines marked with STDERR_PREFIX, so a step that hangs is logged up to where it stopped.
    """
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    out: list[str] = []
    err: list[str] = []
    lock = threading.Lock()

    def pump(stream, sink, target, prefix):
        for line in stream:
            sink.append(line.rstrip("\n"))
            if echo:
                target.write(line)
                target.flush()
            if log is not None:
                with lock:
                    log.write(prefix + line.rstrip("\n") + "\n")
                    log.flush()

    threads = [threading.Thread(target=pump, args=(proc.stdout, out, sys.stdout, ""),
                                daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err, sys.stderr, STDERR_PREFIX),
                                daemon=True)]
    for t in threads:
        t.start()
    interrupted = False
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        code = None
    for t in threads:
        t.join(5)
    return Outcome(code, out, err, time.monotonic() - start, interrupted)


# ------------------------------------------------------------------ parsing ---

# Summary lines worth reporting: counts and categories only. Everything else a script
# prints (sample rows, rejected rows) stays out of the report: it can hold names.
_COLON_PREFIXES = ("Rows read:", "Statements in dump:", "Tables with no spec", "Timestamp formats:",
                   "Temperature units:", "Rejected rows:", "Loaded with gaps:",
                   "Loaded with warnings:", "Severity:", "orphan keys:",
                   "parent key not unique:", "not checked:")
_PLAIN_PREFIXES = ("Incidents ", "Done.", "Dropped ", "Recorded as run", "Passed", "REFUSED",
                   "Skipped, not created yet", "NO ROWS FOUND", "Catalog written",
                   "ER diagram written")
_SUMMARY_RES = [
    re.compile(r"^Rev \S+: rows_read=\d+"),                                  # spares
    re.compile(r"^[a-z_]+: read \d+, loaded \d+, rejected \d+$"),           # cmms per table
    re.compile(r"^(trade|shift|wo_type|status|severity_scale|reporting): .+\(total \d+\)$"),
    re.compile(r"^(ok|FAIL|skip)\s{2,}\S"),                                  # agent_user check
    re.compile(r"^rows=\d+\s+matched=\d+"),                                  # relationships check
    re.compile(r"^[a-z_]+\s+\S+\.\S+ -> \S+\.\S+\s+\[(fk|soft)\]$"),         # relationships link
    re.compile(r"^[a-z_]+\s+(added fk_rel_\w+|exists|skip: .+|FAILED: .+)$"),  # foreign keys
    re.compile(r"^[a-z_]+\s+(created|skipped: .+)$"),                        # views
]


def summary_lines(lines: list[str]) -> list[str]:
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_COLON_PREFIXES):
            if line.split(":", 1)[1].strip():          # a bare "Rejected rows:" heads a table
                out.append(line)
        elif line.startswith(_PLAIN_PREFIXES) or any(r.match(line) for r in _SUMMARY_RES):
            out.append(line)
    return out


_COUNT_RES = [
    re.compile(r"Rows read: (\d+)\s+loaded: (\d+)\s+rejected: (\d+)"),        # logs, cmms
    re.compile(r"rows_read=(\d+), parts_loaded=(\d+), rejected=(\d+)"),        # spares
]
_RUN_RE = re.compile(r"Recorded as run (\d+) in ingest_runs")


def parse_counts(lines: list[str]) -> dict | None:
    for line in lines:
        for rx in _COUNT_RES:
            if m := rx.search(line):
                return dict(zip(("rows_read", "rows_loaded", "rows_rejected"),
                                map(int, m.groups())))
    return None


def parse_run_id(lines: list[str]) -> int | None:
    ids = [int(m.group(1)) for line in lines if (m := _RUN_RE.search(line))]
    return ids[-1] if ids else None


@dataclass
class Result:
    planned: Planned
    status: str                    # ok | failed | interrupted | not run
    exit_code: int | None = None
    seconds: float = 0.0
    summary: list[str] = field(default_factory=list)
    counts: dict | None = None
    run_id: int | None = None
    error_tail: list[str] = field(default_factory=list)


def run_pipeline(plan: list[Planned], runner=run_command, keep_going: bool = False,
                 echo=print, log=None) -> list[Result]:
    """Run each step in order. `log` (an open text file) gets a header and the outcome of
    every step; pass it to the runner too for the steps' own output."""
    def to_log(line: str) -> None:
        if log is not None:
            log.write(line + "\n")
            log.flush()

    results: list[Result] = []
    stop = False
    for i, p in enumerate(plan, 1):
        header = f"=== [{i}/{len(plan)}] {p.step.title}: {p.display()}"
        if stop:
            results.append(Result(p, "not run"))
            to_log(f"\n{header}\n--- not run: an earlier step didn't succeed")
            continue
        echo("\n" + header, flush=True)
        to_log("\n" + header)
        o = runner([sys.executable, str(SCRIPTS / p.step.script), *p.args])
        status = ("interrupted" if o.interrupted else "ok" if o.exit_code == 0 else "failed")
        to_log(f"--- {status}" + (f", exit code {o.exit_code}" if o.exit_code is not None else "")
               + f", {_duration(o.seconds)}")
        results.append(Result(
            p, status, o.exit_code, o.seconds, summary_lines(o.stdout),
            parse_counts(o.stdout), parse_run_id(o.stdout + o.stderr),
            o.stderr[-ERROR_TAIL:] if status != "ok" else []))
        if status == "interrupted" or (status == "failed" and not keep_going):
            stop = True
    return results


# ----------------------------------------------------------------- database ---

def fetch_db(env_file: str | None, run_ids: list[int], want_links: bool):
    """(ingest_runs rows by run_id, table_relationships rows | error text | None)."""
    from sqlalchemy import bindparam, text

    engine = load_db_config(env_file).engine()
    runs, links = {}, None
    with engine.connect() as conn:
        if run_ids:
            q = text("SELECT run_id, script, source_file, file_sha256, git_commit, status, "
                     "rows_read, rows_loaded, rows_rejected FROM ingest_runs "
                     "WHERE run_id IN :ids").bindparams(bindparam("ids", expanding=True))
            runs = {r["run_id"]: dict(r) for r in conn.execute(q, {"ids": run_ids}).mappings()}
        if want_links:
            links = [dict(r) for r in conn.execute(text(
                "SELECT name, child_table, child_column, parent_table, parent_column, enforced, "
                "child_rows, matched_rows, orphan_rows, orphan_values "
                "FROM table_relationships ORDER BY name")).mappings()]
    return runs, links


def git_dirty() -> bool | None:
    """True if tracked files have uncommitted changes; None outside a git repo."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return None if out.returncode != 0 else bool(out.stdout.strip())


@dataclass
class InputFile:
    step: str
    path: str
    size: int | None = None
    sha256: str | None = None


def describe_inputs(plan: list[Planned]) -> list[InputFile]:
    out = []
    for p in plan:
        if not p.input:
            continue
        f = Path(p.input) if Path(p.input).is_absolute() else ROOT / p.input
        out.append(InputFile(p.step.key, p.input, f.stat().st_size, file_sha256(str(f)))
                   if f.is_file() else InputFile(p.step.key, p.input))
    return out


# ------------------------------------------------------------------- report ---

def _cell(v) -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v).replace("|", "\\|").replace("\n", " ")


def _duration(s: float) -> str:
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m {int(s % 60):02d}s"


def _size(n: int | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def render_report(meta: dict, results: list[Result], inputs: list[InputFile],
                  runs: dict | None = None, links=None) -> str:
    runs = runs or {}
    bad = next((r for r in results if r.status != "ok"), None)
    result = ("**Succeeded**" if bad is None else
              f"**{bad.status.upper()}** at step {results.index(bad) + 1}, {bad.planned.step.title}")
    commit, dirty = meta.get("git_commit"), meta.get("git_dirty")
    code = ("not a git repository" if commit is None else
            f"`{commit}` **with uncommitted changes**: `git_commit` in `ingest_runs` does not "
            f"describe the code that ran" if dirty else f"`{commit}`")
    L = ["# Pipeline run", "",
         "| | |", "|---|---|",
         f"| Result | {result} |",
         f"| Started | {meta['started']:%Y-%m-%d %H:%M:%S %z} |",
         f"| Finished | {meta['finished']:%Y-%m-%d %H:%M:%S %z} "
         f"({_duration((meta['finished'] - meta['started']).total_seconds())}) |",
         f"| Mode | {'dry run: nothing written to the database' if meta['dry_run'] else 'load'} |",
         f"| Options | {_cell(' '.join(meta.get('options') or []) or None)} |",
         f"| Database | {_cell(meta.get('database'))} |",
         f"| Code | {code} |"]
    if meta.get("log"):
        name, link = meta["log"]
        L.append(f"| Full log | [`{name}`]({link}): everything shown on screen, employee "
                 f"names masked to their last 4 characters. Git-ignored; keep it private |")
    L.append("")

    L += ["## Steps", "",
          "| # | Step | Script | Status | Time | Run | Read | Loaded | Rejected |",
          "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        c = runs.get(r.run_id) or r.counts or {}
        L.append(f"| {i} | {_cell(r.planned.step.title)} | `{r.planned.step.script}` | "
                 f"{r.status} | {_duration(r.seconds) if r.status != 'not run' else '–'} | "
                 f"{_cell(r.run_id)} | {_cell(c.get('rows_read'))} | "
                 f"{_cell(c.get('rows_loaded'))} | {_cell(c.get('rows_rejected'))} |")
    L += ["", "For `relationships.py`, Read / Loaded / Rejected are rows checked / matched / "
          "orphans. Counts for a load come from `ingest_runs`; for a dry run, from the "
          "scripts' summaries.", ""]

    if inputs:
        L += ["## Input files", "",
              "| Step | File | Size | SHA-256 | Same file as loaded |", "|---|---|---|---|---|"]
        by_step = {r.planned.step.key: r for r in results}
        for f in inputs:
            row = runs.get(by_step[f.step].run_id) if f.step in by_step else None
            same = ("n/a (dry run)" if meta["dry_run"] else "–" if not row or not f.sha256
                    else "yes" if row["file_sha256"] == f.sha256 else "**NO**")
            L.append(f"| {f.step} | `{f.path}` | {_size(f.size)} | "
                     f"{f'`{f.sha256}`' if f.sha256 else '**file not found**'} | {same} |")
        L.append("")

    if isinstance(links, list) and links:
        L += ["## Links between sources", "",
              "From `table_relationships` after this run.", "",
              "| Link | Child -> parent | Kind | Rows | Matched | Orphans | Orphan keys |",
              "|---|---|---|---|---|---|---|"]
        for k in links:
            L.append(f"| {_cell(k['name'])} | `{k['child_table']}.{k['child_column']}` -> "
                     f"`{k['parent_table']}.{k['parent_column']}` | {_cell(k['enforced'])} | "
                     f"{_cell(k['child_rows'])} | {_cell(k['matched_rows'])} | "
                     f"{_cell(k['orphan_rows'])} | {_cell(k['orphan_values'])} |")
        L.append("")
    elif isinstance(links, str):
        L += ["## Links between sources", "", f"Couldn't read `table_relationships`: {links}", ""]

    L += ["## Details", ""]
    for i, r in enumerate(results, 1):
        L += [f"### {i}. {r.planned.step.title}: {r.status}", "", f"`{r.planned.display()}`", ""]
        if r.status == "not run":
            L += ["Not run: an earlier step didn't succeed.", ""]
            continue
        if r.summary:
            L += ["```text", *r.summary, "```", ""]
        if r.error_tail:
            L += [f"Error output (last {ERROR_TAIL} lines at most):", "", "```text",
                  *r.error_tail, "```", ""]
    L += ["---", "",
          "Generated by `scripts/pipeline.py`. Only summary lines are copied here, because "
          "dry runs print sample rows that can include personal data. The scripts' full "
          "output " + ("is in the log file linked above." if meta.get("log") else
                       "went to the terminal; `--save-log` keeps it in a file.")]
    return "\n".join(L) + "\n"


LOG_GITIGNORE = "# Written by scripts/pipeline.py: full logs can hold personal data.\n*\n"


def open_log(path: Path):
    """Create the log file, readable only by you, in a folder git ignores."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text(LOG_GITIGNORE, encoding="utf-8")
    fh = open(path, "w", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # e.g. some Windows file systems: the .gitignore still applies
        pass
    return fh


# --------------------------------------------------------------------- main ---

def main(argv: list[str] | None = None, runner=run_command) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for key in INGEST_KEYS:
        ap.add_argument(f"--{key}", metavar="PATH",
                        help=f"input for {BY_KEY[key].script} (default {DEFAULT_INPUTS[key]})")
    ap.add_argument("--only", metavar="STEPS",
                    help=f"comma-separated steps to run, from: {', '.join(BY_KEY)}")
    ap.add_argument("--recreate", action="store_true",
                    help="pass --recreate to the ingest scripts (first load, schema change)")
    ap.add_argument("--foreign-keys", action="store_true",
                    help="drop relationships.py's foreign keys before loading, add them after")
    ap.add_argument("--mermaid", metavar="PATH", default="docs/er_diagram.md",
                    help="ER diagram written by relationships.py (default %(default)s)")
    ap.add_argument("--no-mermaid", action="store_true", help="don't rewrite the ER diagram")
    ap.add_argument("--agent", choices=["check", "setup"],
                    help="also run agent_user.py: --check only, or full setup + check")
    ap.add_argument("--keep-going", action="store_true",
                    help="run the remaining steps after a failure")
    ap.add_argument("--report", metavar="PATH",
                    help="report file (default reports/pipeline_<date>-<time>.md)")
    ap.add_argument("--save-log", action="store_true",
                    help="also save everything shown on screen to logs/<report name>.log next "
                         "to the report (git-ignored: it can hold personal data)")
    add_common_args(ap)
    a = ap.parse_args(argv)

    cwd = Path.cwd()
    try:
        only = parse_only(a.only)
    except ValueError as e:
        ap.error(str(e))
    inputs = {k: rel(getattr(a, k), cwd) if getattr(a, k) else DEFAULT_INPUTS[k]
              for k in INGEST_KEYS}
    env_file = str(Path(a.env_file).resolve()) if a.env_file else None
    mermaid = None if a.no_mermaid else (rel(a.mermaid, cwd) if a.mermaid != "docs/er_diagram.md"
                                         else a.mermaid)
    plan = build_plan(inputs=inputs, dry_run=a.dry_run, recreate=a.recreate,
                      foreign_keys=a.foreign_keys, mermaid=mermaid, agent=a.agent,
                      env_file=env_file, only=only)

    started = datetime.now().astimezone()
    stamp = f"{started:%Y%m%d-%H%M%S}" + ("_dry-run" if a.dry_run else "")
    report = Path(a.report) if a.report else ROOT / "reports" / f"pipeline_{stamp}.md"
    options = [o for o, on in [("--dry-run", a.dry_run), ("--recreate", a.recreate),
                               ("--foreign-keys", a.foreign_keys), ("--keep-going", a.keep_going),
                               (f"--agent {a.agent}", a.agent), (f"--only {a.only}", a.only)] if on]
    if a.dry_run:
        database = "not used (dry run)"
    else:
        try:
            database = load_db_config(a.env_file).describe()
        except (ValueError, FileNotFoundError) as e:
            database = f"not configured: {e}"

    inputs_info = describe_inputs(plan)
    log_path, log = None, None
    if a.save_log:
        log_path = report.parent / "logs" / f"{report.stem}.log"
        log = open_log(log_path)
        log.write(f"# Pipeline log, started {started:%Y-%m-%d %H:%M:%S %z}\n"
                  f"# Command: python scripts/pipeline.py {' '.join(argv if argv is not None else sys.argv[1:])}\n"
                  f"# Everything the steps printed, as it happened ({STDERR_PREFIX.strip()} = "
                  f"error output). Employee names appear masked (last 4 characters); still keep "
                  f"this file private: don't commit or share it.\n")
    try:
        step_runner = (lambda cmd: runner(cmd, log=log)) if log else runner
        results = run_pipeline(plan, step_runner, a.keep_going, log=log)
    finally:
        finished = datetime.now().astimezone()
        if log:
            log.write(f"\n# Finished {finished:%Y-%m-%d %H:%M:%S %z}\n")
            log.close()

    runs, links = {}, None
    if not a.dry_run:
        ids = [r.run_id for r in results if r.run_id is not None]
        want_links = any(r.planned.step.key == "relationships" and r.status == "ok"
                         for r in results)
        if ids or want_links:
            try:
                runs, links = fetch_db(a.env_file, ids, want_links)
            except Exception as e:  # noqa: BLE001 - the report must still be written
                links = f"{type(e).__name__} (counts below are from the scripts' summaries)"
    meta = {"started": started, "finished": finished, "dry_run": a.dry_run, "options": options,
            "database": database, "git_commit": git_commit(), "git_dirty": git_dirty(),
            "log": (rel(log_path), f"logs/{log_path.name}") if log_path else None}

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(meta, results, inputs_info, runs, links), encoding="utf-8")
    ok = all(r.status == "ok" for r in results)
    print(f"\n{'Succeeded' if ok else 'FAILED'}: {sum(r.status == 'ok' for r in results)}"
          f"/{len(results)} steps ok. Report written to {rel(report)}"
          + (f"; full log in {rel(log_path)}" if log_path else ""))
    if meta["git_dirty"] and not a.dry_run:
        print("Note: uncommitted code changes; commit before a load that matters.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())