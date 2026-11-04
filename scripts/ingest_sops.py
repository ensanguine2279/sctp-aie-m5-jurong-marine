#!/usr/bin/env python3
"""Ingest the SOP documents (docs/sops/) into MySQL, with lineage.

Each SOP is a short text file: a header line, then numbered steps or bullets.

    JME SOP-LOTO-01 — LOCKOUT/TAGOUT PROCEDURE (rev 4, Jan 2026)
    1. Identify all energy sources (electrical, hydraulic, pneumatic, gravity).
    ...

Tables, so every quoted step can be traced to the file and line it came from:

    ingest_runs          one row per load: folder, one SHA-256 over its files, git commit
    sop_documents_raw    one row per file, its text exactly as read, status (loaded /
                         rejected), reject reason, warnings
    sops                 one row per SOP revision: doc no, title, revision, month,
                         'applies to', is_current, the whole text (FULLTEXT index)
    sop_steps            one row per step or bullet, with its line number (FULLTEXT)
    sop_machines         SOP -> machine: ids named in the text, CMMS machine names, aliases

    sops_audit, sop_steps_audit (views)   clean + file + its SHA-256 + run, for citations

Pipeline (each step is a small function you can test on its own):
    read_folder        every .txt / .md file, decoded as UTF-8 (never the OS default)
      -> parse_document   header -> doc no, title, revision, month; lines -> steps
      -> split_rejects    not an SOP, not UTF-8, same file twice, same doc + revision twice
      -> machine_links    'CRN-01/CRN-02', 'blasting chamber (BLAST-01)', 'test pump sump'
      -> load             full refresh in one transaction

The folder is the complete set of SOPs, so each load replaces every SOP row.

Answering from the SOPs: pick the SOP by keyword (search_sops, or MySQL MATCH ... AGAINST
on sops), then quote it WHOLE from sop_steps, citing doc no, revision and line numbers.
Never quote one step on its own: a safety step out of its procedure is how someone gets hurt.

Usage
    python scripts/ingest_sops.py data/sops --dry-run
    python scripts/ingest_sops.py data/sops --recreate     # first time
    python scripts/ingest_sops.py data/sops
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from db_config import add_common_args, load_db_config, to_records
from lineage import RUNS_DDL, finish_run, folder_files, folder_sha256, start_run

EXTENSIONS = {".txt", ".md"}
RAW_ID_STRIDE = 1000  # raw_id = run_id * 1000 + file number (1-based, by name)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# 'JME SOP-LOTO-01 — LOCKOUT/TAGOUT PROCEDURE (rev 4, Jan 2026)'. Any dash between number
# and title: an em dash in the files, but a hyphen if someone retypes it.
HEADER = re.compile(r"^\s*JME\s+(?P<doc_no>SOP-[A-Z]+-\d+)\s*[—–-]+\s*(?P<title>.+?)\s*"
                    r"(?:\(\s*rev\.?\s*(?P<rev>\d+)\s*,\s*(?P<month>[A-Za-z]{3,9})\.?\s*"
                    r"(?P<year>\d{4})\s*\))?\s*$", re.I)
SOP_START = re.compile(r"^\s*JME\s+SOP-", re.I)

# Machine ids as the CMMS writes them (same rule as ingest_images.TAG).
TAG = re.compile(r"\b[A-Z]{2,6}-[A-HJKMNPR-Z]?\d{2,3}\b")
# Phrases the SOPs use for machines, where neither the id nor the CMMS name appears.
# A judgement, not data: each link records how it was made (how = 'alias').
ALIASES = {
    "test pump sump": ["PMP-T01"],             # SOP-CONFINED-03; CMMS name 'Test Pump Rig'
    "cnc machines": ["CNC-01", "CNC-02", "CNC-03"],   # SOP-LOTO-01 step 6
}
# Documents an SOP points to (trap list D8). None of them is in the data package.
REFERENCES = {
    "machine manual": r"machine\s+manual",
    "certified operator list (HR)": r"list\s+with\s+HR",
    "lifting plan (signed by PE)": r"lifting\s+plan",
    "entry permit": r"\bpermit\b",
}
# Personal data that must not sit in a citable SOP step (PDPA).
PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\b65[\s-]?\d{4}[\s-]?\d{4}\b|\b[689]\d{3}\s?\d{4}\b")

RAW_COLUMNS = ["raw_id", "run_id", "source_folder", "source_file", "file_sha256", "file_bytes",
               "header_line", "doc_no", "revision", "line_count", "raw_text", "status",
               "reject_reason", "duplicate_of_raw_id", "warnings"]
SOP_COLUMNS = ["doc_no", "revision", "raw_id", "title", "rev_month", "rev_label", "applies_to",
               "is_current", "step_count", "full_text"]
STEP_COLUMNS = ["doc_no", "revision", "step_no", "line_no", "marker", "step_text", "refers_to"]
LINK_COLUMNS = ["doc_no", "revision", "machine_id", "how", "evidence"]

DDL = [
    """
    CREATE TABLE IF NOT EXISTS sop_documents_raw (
        raw_id              BIGINT       NOT NULL PRIMARY KEY COMMENT 'run_id * 1000 + file number',
        run_id              BIGINT       NULL,
        source_folder       VARCHAR(255) NOT NULL,
        source_file         VARCHAR(255) NOT NULL,
        file_sha256         CHAR(64)     NOT NULL,
        file_bytes          INT          NOT NULL,
        header_line         VARCHAR(255) NULL COMMENT 'first line, as read',
        doc_no              VARCHAR(30)  NULL,
        revision            INT          NULL,
        line_count          INT          NOT NULL,
        raw_text            TEXT         NULL COMMENT 'the whole file as read (UTF-8)',
        status              VARCHAR(10)  NOT NULL COMMENT 'loaded | rejected',
        reject_reason       VARCHAR(40)  NULL,
        duplicate_of_raw_id BIGINT       NULL,
        warnings            VARCHAR(255) NULL,
        KEY ix_sop_raw_status (status, reject_reason),
        CONSTRAINT fk_sop_raw_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
            ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS sops (
        doc_no      VARCHAR(30)  NOT NULL,
        revision    INT          NOT NULL DEFAULT 0 COMMENT '0 = not stated in the header',
        raw_id      BIGINT       NOT NULL,
        title       VARCHAR(200) NOT NULL,
        rev_month   CHAR(7)      NULL COMMENT 'YYYY-MM from the header; no day is given',
        rev_label   VARCHAR(30)  NULL COMMENT 'as written, e.g. rev 4, Jan 2026',
        applies_to  VARCHAR(255) NULL COMMENT 'the Applies to: line, if any',
        is_current  TINYINT(1)   NOT NULL COMMENT '1 = highest revision of this doc no loaded',
        step_count  INT          NOT NULL,
        full_text   TEXT         NOT NULL COMMENT 'title + every step, for search',
        PRIMARY KEY (doc_no, revision),
        UNIQUE KEY uq_sops_raw (raw_id),
        FULLTEXT KEY ft_sops (title, full_text),
        CONSTRAINT fk_sops_raw FOREIGN KEY (raw_id) REFERENCES sop_documents_raw (raw_id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS sop_steps (
        doc_no     VARCHAR(30)  NOT NULL,
        revision   INT          NOT NULL,
        step_no    INT          NOT NULL COMMENT 'order in the document, from 1',
        line_no    INT          NOT NULL COMMENT 'line in the file (header = line 1)',
        marker     VARCHAR(10)  NOT NULL COMMENT 'the step number (1.), - for a bullet, or empty',
        step_text  VARCHAR(1000) NOT NULL COMMENT 'as written, marker removed',
        refers_to  VARCHAR(255) NULL COMMENT 'documents it points to, not in the data',
        PRIMARY KEY (doc_no, revision, step_no),
        FULLTEXT KEY ft_sop_steps (step_text),
        CONSTRAINT fk_sop_steps FOREIGN KEY (doc_no, revision) REFERENCES sops (doc_no, revision)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS sop_machines (
        doc_no     VARCHAR(30)  NOT NULL,
        revision   INT          NOT NULL,
        machine_id VARCHAR(20)  NOT NULL,
        how        VARCHAR(10)  NOT NULL COMMENT 'id | name (CMMS machine name) | alias',
        evidence   VARCHAR(100) NOT NULL COMMENT 'the words in the SOP that name it',
        PRIMARY KEY (doc_no, revision, machine_id),
        KEY ix_sop_machines_machine (machine_id),
        CONSTRAINT fk_sop_machines FOREIGN KEY (doc_no, revision) REFERENCES sops (doc_no, revision)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE OR REPLACE VIEW sops_audit AS
    SELECT s.doc_no, s.revision, s.title, s.rev_month, s.rev_label, s.applies_to, s.is_current,
           s.step_count, s.raw_id, r.source_folder, r.source_file, r.file_sha256, r.warnings,
           r.run_id, u.git_commit, u.started_at AS loaded_at
    FROM sops s
    JOIN sop_documents_raw r ON r.raw_id = s.raw_id
    LEFT JOIN ingest_runs u ON u.run_id = r.run_id
    """,
    """
    CREATE OR REPLACE VIEW sop_steps_audit AS
    SELECT t.doc_no, t.revision, s.title, s.rev_label, s.is_current, t.step_no, t.line_no,
           t.marker, t.step_text, t.refers_to, r.source_file, r.file_sha256, r.run_id
    FROM sop_steps t
    JOIN sops s ON s.doc_no = t.doc_no AND s.revision = t.revision
    JOIN sop_documents_raw r ON r.raw_id = s.raw_id
    """,
]


# ------------------------------------------------------------------- reading ---

@dataclass
class SopFile:
    source_file: str
    data: bytes
    file_sha256: str = ""

    def __post_init__(self):
        self.file_sha256 = self.file_sha256 or hashlib.sha256(self.data).hexdigest()


def read_folder(folder: str | Path) -> list[SopFile]:
    """Every .txt / .md file directly in the folder, by name, as bytes."""
    return [SopFile(p.name, p.read_bytes()) for p in folder_files(folder, EXTENSIONS)]


def decode(data: bytes) -> str | None:
    """UTF-8 (with or without a BOM), line endings normalised; None if not UTF-8.

    Never the OS default: on Windows that is cp1252, which turns the em dash in every
    SOP title into 'â€”' (trap list D2)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


# ------------------------------------------------------------------- parsing ---

@dataclass
class Step:
    step_no: int
    line_no: int
    marker: str
    text: str
    refers_to: str | None = None


@dataclass
class Document:
    source_file: str
    file_sha256: str
    file_bytes: int
    text: str | None
    header_line: str | None = None
    doc_no: str | None = None
    title: str | None = None
    revision: int | None = None
    rev_month: str | None = None
    rev_label: str | None = None
    applies_to: str | None = None
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reject_reason: str | None = None
    duplicate_of: int | None = None      # file number of the copy that was kept
    links: list[tuple[str, str, str]] = field(default_factory=list)   # (machine, how, evidence)

    @property
    def line_count(self) -> int:
        return len(self.text.split("\n")) if self.text else 0


def references(text: str) -> str | None:
    found = [name for name, rx in REFERENCES.items() if re.search(rx, text, re.I)]
    return ", ".join(found) or None


def parse_steps(lines: list[str]) -> tuple[str | None, list[Step]]:
    """(applies_to, steps) from the lines after the header. Line numbers count the header
    as line 1. '1. ...' / '1) ...' keep their number as the marker, '- ...' is a bullet,
    anything else is a step with no marker (e.g. LOTO's closing 'VIOLATION = ...')."""
    applies_to, steps = None, []
    for i, line in enumerate(lines, start=2):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"applies\s+to\s*:\s*(.*)$", s, re.I)
        if m and applies_to is None and not steps:
            applies_to = m.group(1).strip() or None
            continue
        m = re.match(r"^(\d+[.)])\s+(.*)$", s) or re.match(r"^([-*•])\s+(.*)$", s)
        marker, body = (m.group(1), m.group(2).strip()) if m else ("", s)
        if marker in ("*", "•"):
            marker = "-"
        steps.append(Step(len(steps) + 1, i, marker, body, references(body)))
    return applies_to, steps


def parse_document(f: SopFile) -> Document:
    text = decode(f.data)
    doc = Document(f.source_file, f.file_sha256, len(f.data), text)
    if text is None:
        doc.reject_reason = "not_utf8"
        return doc
    lines = text.split("\n")
    first = next((ln for ln in lines if ln.strip()), "")
    doc.header_line = first.strip()[:255] or None
    if not SOP_START.match(first):
        doc.reject_reason = "not_an_sop"          # e.g. the GM's email (trap list D1)
        return doc
    m = HEADER.match(first)
    if not m:
        doc.reject_reason = "bad_header"
        return doc
    doc.doc_no = m.group("doc_no").upper()
    doc.title = m.group("title").strip()
    if m.group("rev"):
        doc.revision = int(m.group("rev"))
        month = MONTHS.get(m.group("month")[:3].lower())
        if month:
            doc.rev_month = f"{m.group('year')}-{month:02d}"
            doc.rev_label = f"rev {doc.revision}, {m.group('month')[:3].title()} {m.group('year')}"
        else:
            doc.warnings.append("revision_month_unparsed")
    else:
        doc.warnings.append("no_revision_in_header")
    doc.applies_to, doc.steps = parse_steps(lines[lines.index(first) + 1:])
    if not doc.steps:
        doc.warnings.append("no_steps")
    if Path(f.source_file).stem.upper() != doc.doc_no:
        doc.warnings.append("doc_no_differs_from_file_name")
    if any(st.refers_to for st in doc.steps):
        doc.warnings.append("refers_to_document_not_in_data")
    if PERSONAL.search("\n".join(st.text for st in doc.steps) + (doc.applies_to or "")):
        doc.warnings.append("personal_data_in_text")
    return doc


def split_rejects(docs: list[Document]) -> list[Document]:
    """Same bytes as an earlier file: duplicate_file. Same doc no and revision as an
    earlier file: duplicate_revision (or ..._conflict if the text differs). First file
    by name wins. Different revisions of one SOP are all kept; is_current marks the
    highest (trap list D9)."""
    seen_bytes: dict[str, int] = {}
    seen_rev: dict[tuple[str, int], int] = {}
    for n, d in enumerate(docs, start=1):
        if d.reject_reason:
            continue
        if d.file_sha256 in seen_bytes:
            d.reject_reason, d.duplicate_of = "duplicate_file", seen_bytes[d.file_sha256]
            continue
        seen_bytes[d.file_sha256] = n
        key = (d.doc_no, d.revision or 0)
        if key in seen_rev:
            kept = docs[seen_rev[key] - 1]
            same = [s.text for s in kept.steps] == [s.text for s in d.steps]
            d.reject_reason = "duplicate_revision" if same else "duplicate_revision_conflict"
            d.duplicate_of = seen_rev[key]
            continue
        seen_rev[key] = n
    return docs


def machine_links(doc: Document, machines: dict[str, str] | None = None) -> list[tuple]:
    """(machine_id, how, evidence) for every machine the SOP names.

    id: an id in the text ('CRN-01/CRN-02' gives both). name: a CMMS machine name in the
    text ('blasting chamber'), only when the CMMS is loaded (`machines` = {id: name}).
    alias: a phrase in ALIASES ('test pump sump' -> PMP-T01)."""
    text = " ".join([doc.title or "", doc.applies_to or ""] + [s.text for s in doc.steps])
    low = text.lower()
    out: dict[str, tuple[str, str]] = {}
    expanded = re.sub(r"\b([A-Z]{2,6}-)(\d{2,3})((?:/\d{2,3})+)\b",
                      lambda m: "/".join(m.group(1) + p for p in
                                         [m.group(2)] + m.group(3).strip("/").split("/")), text)
    for m in TAG.finditer(expanded):
        out.setdefault(m.group(0), ("id", m.group(0)))
    for mid, name in (machines or {}).items():
        if name and re.search(rf"\b{re.escape(name.lower())}\b", low):
            out.setdefault(mid, ("name", name.lower()))
    for phrase, ids in ALIASES.items():
        if phrase in low:
            for mid in ids:
                out.setdefault(mid, ("alias", phrase))
    return [(mid, how, ev[:100]) for mid, (how, ev) in sorted(out.items())]


def add_machine_warnings(docs: list[Document], machines: dict[str, str] | None) -> None:
    if not machines:
        return
    for d in docs:
        if not d.reject_reason and any(mid not in machines for mid, _, _ in d.links):
            d.warnings.append("unknown_machine")


# ------------------------------------------------------------------ prepare ---

def process(files: list[SopFile], *, source_folder: str = "sops", run_id: int = 0,
            machines: dict[str, str] | None = None) -> dict:
    """Parse, check and link. Returns DataFrames: raw, sops, steps, links."""
    docs = split_rejects([parse_document(f) for f in files])
    for d in docs:
        if not d.reject_reason:
            d.links = machine_links(d, machines)
    add_machine_warnings(docs, machines)

    def raw_id(n):
        return run_id * RAW_ID_STRIDE + n

    loaded = [(n, d) for n, d in enumerate(docs, start=1) if not d.reject_reason]
    current = {}
    for _, d in loaded:
        current[d.doc_no] = max(current.get(d.doc_no, -1), d.revision or 0)

    raw = pd.DataFrame([{
        "raw_id": raw_id(n), "run_id": run_id or None, "source_folder": source_folder,
        "source_file": d.source_file, "file_sha256": d.file_sha256, "file_bytes": d.file_bytes,
        "header_line": d.header_line, "doc_no": d.doc_no, "revision": d.revision,
        "line_count": d.line_count, "raw_text": d.text,
        "status": "rejected" if d.reject_reason else "loaded", "reject_reason": d.reject_reason,
        "duplicate_of_raw_id": raw_id(d.duplicate_of) if d.duplicate_of else None,
        "warnings": None if d.reject_reason else (",".join(d.warnings) or None),
    } for n, d in enumerate(docs, start=1)], columns=RAW_COLUMNS)
    sops = pd.DataFrame([{
        "doc_no": d.doc_no, "revision": d.revision or 0, "raw_id": raw_id(n), "title": d.title,
        "rev_month": d.rev_month, "rev_label": d.rev_label, "applies_to": d.applies_to,
        "is_current": int((d.revision or 0) == current[d.doc_no]), "step_count": len(d.steps),
        "full_text": "\n".join([d.title] + ([f"Applies to: {d.applies_to}"] if d.applies_to else [])
                               + [s.text for s in d.steps]),
    } for n, d in loaded], columns=SOP_COLUMNS)
    steps = pd.DataFrame([{
        "doc_no": d.doc_no, "revision": d.revision or 0, "step_no": s.step_no, "line_no": s.line_no,
        "marker": s.marker, "step_text": s.text, "refers_to": s.refers_to,
    } for _, d in loaded for s in d.steps], columns=STEP_COLUMNS)
    links = pd.DataFrame([{
        "doc_no": d.doc_no, "revision": d.revision or 0, "machine_id": mid, "how": how,
        "evidence": ev,
    } for _, d in loaded for mid, how, ev in d.links], columns=LINK_COLUMNS)
    return {"raw": raw, "sops": sops, "steps": steps, "links": links}


# ------------------------------------------------------------------- search ---

_STOP = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "is", "are", "what",
         "whats", "what's", "how", "do", "i", "we", "before", "after", "sop", "procedure", "with",
         "my", "our", "can", "should", "when", "it", "this", "that", "be", "by",
         # words every SOP uses: on their own they say nothing about the topic
         "work", "working", "machine", "machines", "all", "only", "use", "required", "before",
         "after", "every", "per", "person", "personnel"}
MIN_SCORE = 2.0   # one stray common word (1 point) is not a match


def _words(s: str) -> list[str]:
    """Lower-case words, stop words dropped, a plural 's' removed ('tanks' -> 'tank')."""
    out = []
    for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", s.lower()):
        if w in _STOP:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def search_sops(question: str, sops: pd.DataFrame, links: pd.DataFrame | None = None,
                limit: int = 3) -> list[tuple[str, int, float]]:
    """(doc_no, revision, score) of current SOPs, best first; [] if nothing scores at least
    MIN_SCORE, so the
    app can say 'no SOP in the data covers this' (trap list D3). Title words count double;
    a machine the SOP is linked to (id or evidence phrase in the question) counts 3.
    For three short documents this is enough; MySQL MATCH ... AGAINST on sops does the
    same in the database."""
    q = set(_words(question))
    qtext = question.lower()
    out = []
    for r in sops[sops["is_current"] == 1].itertuples():
        title, body = set(_words(r.title)), set(_words(r.full_text))
        score = 2.0 * len(q & title) + len(q & body)
        if links is not None and len(links):
            mine = links[(links["doc_no"] == r.doc_no) & (links["revision"] == r.revision)]
            score += 3.0 * sum(1 for m in mine.itertuples()
                               if m.machine_id.lower() in qtext or m.evidence in qtext)
        if score >= MIN_SCORE:
            out.append((r.doc_no, int(r.revision), score))
    return sorted(out, key=lambda t: (-t[2], t[0]))[:limit]


def quote_sop(doc_no: str, revision: int, sops: pd.DataFrame, steps: pd.DataFrame) -> str:
    """The whole SOP as the app should show it, with its citation."""
    s = sops[(sops["doc_no"] == doc_no) & (sops["revision"] == revision)].iloc[0]
    body = steps[(steps["doc_no"] == doc_no) & (steps["revision"] == revision)]
    lines = [f"{s.doc_no} — {s.title} ({s.rev_label or 'revision not stated'})"]
    if s.applies_to:
        lines.append(f"Applies to: {s.applies_to}")
    lines += [f"{(r.marker + ' ') if r.marker else ''}{r.step_text}" for r in body.itertuples()]
    first, last = int(body["line_no"].min()), int(body["line_no"].max())
    lines.append(f"[Source: {doc_no}, lines {first}-{last}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------- load ---

TABLES = ["sop_machines", "sop_steps", "sops", "sop_documents_raw"]   # children first


def create_tables(engine, recreate: bool) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:  # DDL commits implicitly in MySQL
        conn.execute(text(RUNS_DDL))
        if recreate:
            for v in ["sop_steps_audit", "sops_audit"]:
                conn.execute(text(f"DROP VIEW IF EXISTS {v}"))
            for t in TABLES:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
        for stmt in DDL:
            conn.execute(text(stmt))


def cmms_machines(engine, table: str = "cmms_machines") -> dict[str, str] | None:
    """{machine_id: machine_name} from the CMMS load, or None if it isn't loaded."""
    from sqlalchemy import exc, text

    try:
        with engine.connect() as conn:
            return {mid: name for mid, name in conn.execute(
                text(f"SELECT machine_id, machine_name FROM `{table}`"))}
    except exc.DBAPIError:
        return None


def _insert(table: str, columns: list[str]):
    from sqlalchemy import text

    return text(f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})")


def load(engine, out: dict) -> None:
    """Replace every SOP row with this folder's, in ONE transaction."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sop_documents_raw"))   # cascades to the rest
        for table, key, cols in [("sop_documents_raw", "raw", RAW_COLUMNS),
                                 ("sops", "sops", SOP_COLUMNS), ("sop_steps", "steps", STEP_COLUMNS),
                                 ("sop_machines", "links", LINK_COLUMNS)]:
            if len(out[key]):
                conn.execute(_insert(table, cols), to_records(out[key]))


# ------------------------------------------------------------------- report ---

def summary(out: dict) -> Counter:
    raw, sops, steps, links = out["raw"], out["sops"], out["steps"], out["links"]
    stats = Counter({"rows_read": len(raw), "loaded": int((raw["status"] == "loaded").sum()),
                     "rejected": int((raw["status"] == "rejected").sum()),
                     "steps": len(steps), "machine_links": len(links)})
    stats.update(f"rejected:{r}" for r in raw["reject_reason"].dropna())
    for w in raw["warnings"].dropna():
        stats.update(f"warn:{x}" for x in w.split(","))
    stats.update(f"link:{h}" for h in links["how"])
    for refs in steps["refers_to"].dropna():
        stats.update(f"ref:{x.strip()}" for x in refs.split(","))
    return stats


def print_report(out: dict, stats: Counter, machines_known: bool) -> None:
    print(f"\nRows read: {stats['rows_read']}   loaded: {stats['loaded']}   "
          f"rejected: {stats['rejected']}")
    print("One row is one file.")
    for r in out["sops"].itertuples():
        mids = out["links"][(out["links"]["doc_no"] == r.doc_no)
                            & (out["links"]["revision"] == r.revision)]["machine_id"]
        print(f"  {r.doc_no:<16} {r.rev_label or 'no revision':<18} {r.step_count:>2} steps  "
              f"{'current' if r.is_current else 'superseded'}  machines: {', '.join(mids) or '-'}")
    for prefix, title in [("rejected:", "Rejected files"), ("warn:", "Loaded with warnings"),
                          ("link:", "Machine links by kind"),
                          ("ref:", "Steps referring to documents not in the data")]:
        items = {k[len(prefix):]: v for k, v in stats.items() if k.startswith(prefix)}
        if items:
            print(f"{title}: " + ", ".join(f"{k}={v}" for k, v in sorted(items.items()))
                  + f" (total {sum(items.values())})")
    print("Machine names: " + ("matched against cmms_machines" if machines_known else
                               "not matched (CMMS not loaded); ids and aliases only"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default="data/sops")
    ap.add_argument("--recreate", action="store_true",
                    help="drop and recreate the tables (first run, or after a schema change)")
    ap.add_argument("--cmms-prefix", default="cmms_",
                    help="table prefix used by ingest_cmms.py, to match machine names")
    add_common_args(ap)
    a = ap.parse_args()
    if not Path(a.folder).is_dir():
        sys.exit(f"Folder not found: {a.folder}")
    files = read_folder(a.folder)
    if not files:
        sys.exit(f"No .txt or .md files in {a.folder}")
    folder = Path(a.folder).name

    if a.dry_run:
        out = process(files, source_folder=folder)
        with pd.option_context("display.width", 200, "display.max_colwidth", 70):
            print(out["steps"][["doc_no", "step_no", "line_no", "marker", "step_text"]]
                  .to_string(index=False))
            rej = out["raw"][out["raw"]["status"] == "rejected"]
            if len(rej):   # file name and reason only: a rejected file may hold personal data
                print("\nRejected files:")
                print(rej[["source_file", "reject_reason"]].to_string(index=False))
        print_report(out, summary(out), machines_known=False)
        print("Dry run - nothing written to the database.")
        return

    try:
        cfg = load_db_config(a.env_file)
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    engine = cfg.engine()
    print(f"Loading {a.folder} into {cfg.describe()}")
    create_tables(engine, a.recreate)
    machines = cmms_machines(engine, f"{a.cmms_prefix}machines")
    run_id = start_run(engine, "ingest_sops.py", folder, sha256=folder_sha256(a.folder, EXTENSIONS))
    try:
        out = process(files, source_folder=folder, run_id=run_id, machines=machines)
        load(engine, out)
    except BaseException as e:
        finish_run(engine, run_id, ok=False, error=f"{type(e).__name__}: {e}")
        print(f"Load failed - nothing changed. Recorded as run {run_id} in ingest_runs.",
              file=sys.stderr)
        raise
    stats = summary(out)
    finish_run(engine, run_id, ok=True, rows_read=stats["rows_read"],
               rows_loaded=stats["loaded"], rows_rejected=stats["rejected"])
    print(f"Recorded as run {run_id} in ingest_runs.")
    print_report(out, stats, machines_known=machines is not None)
    print("Done.")


if __name__ == "__main__":
    main()