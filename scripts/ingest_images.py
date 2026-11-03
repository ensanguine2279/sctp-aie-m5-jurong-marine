#!/usr/bin/env python3
"""Ingest the scanned images (machine nameplates, WSH-F02 incident report forms), with lineage.

Every image in the folder is read (OCR), classified, and its fields parsed, cleaned and
checked, like the other sources:

    ingest_runs              one row per load: the folder, one SHA-256 over every image in
                             it, git commit, counts, status
    img_documents_raw        one row per image: file name, its own SHA-256, document type,
                             OCR engine, each field exactly as read (JSON), where the OCR
                             readings disagreed, the OCR text, status (loaded / rejected),
                             reject reason, warnings
    img_nameplates           cleaned nameplates, one per machine: asset_tag = CMMS machine_id
    img_incident_reports     cleaned incident report forms, one per incident:
                             report no IR-2024-900 -> incident_id 900 = CMMS incident_id

    img_nameplates_audit, img_incident_reports_audit (views)  clean + raw + run, for citations

Pipeline (each step is a small function you can test on its own):
    folder_files           every .jpg / .png / .tif ... in the folder, by name
      -> extract           OCR. tesseract (default): three preprocessed versions of each
                           image, read separately; the class LLM endpoint with --engine llm
      -> classify          incident_report | nameplate | unknown
      -> parse_*_text      label -> value, as written
      -> vote              per field, the value most versions agree on, checked against the
                           field's expected format; disagreements are kept and flagged
      -> clean_*           ids, numbers, units, dates; severity on the scale PRINTED ON THE
                           FORM, not the one its date implies
      -> split_rejects     unreadable image, unknown type, missing key, duplicate image or key
      -> add_*_warnings    loaded, but something is missing, unsure or doesn't fit
      -> load              one transaction

Why the printed scale matters: the CMMS severity flipped from 1 = worst to 5 = worst in
July 2025 and old records were never converted (trap list C1-C3, docs/trap_list.md). ingest_cmms.py converts
by date. Each form says which scale it used, so a form can confirm the date rule, contradict
it, or settle an incident the date rule leaves ambiguous (July 2025). Forms whose printed
scale disagrees with the date rule are flagged form_scale_contradicts_date_rule.

Comparing with the CMMS: relationships.py links the tables (asset_tag -> machine_id,
incident_id -> incident_id) and makes the views incident_report_vs_cmms (field by field) and
machine_nameplate. Views, so they stay current when either source is reloaded.

Re-loading a folder replaces every row loaded from that folder (it is the complete set of
scans, like the spares sheet); other folders are kept. OCR results are cached by image
SHA-256 in .cache/ingest_images/ (git-ignored), so re-runs don't re-read unchanged images.

Personal data (PDPA): the forms carry an employee code and a handwritten signature. Only the
code is stored; the signature is reduced to signature_present (1/0). A "person involved"
that isn't an EMP code (e.g. a name) is NOT stored in the clean table, and the raw table
(which keeps it as read) is not granted to the app user.

Usage
    python scripts/ingest_images.py data/images --dry-run
    python scripts/ingest_images.py data/images --recreate        # first time
    python scripts/ingest_images.py data/images
    python scripts/ingest_images.py data/images --engine llm      # vision model via the class proxy
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from db_config import add_common_args, load_db_config, safe_identifier, to_records
from ingest_cmms import SEVERITY_AMBIGUOUS_UNTIL, SEVERITY_CUTOVER, _text, parse_date
from lineage import RUNS_DDL, file_sha256, finish_run, folder_files, folder_sha256, start_run

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = "data/images"
DEFAULT_CACHE = ROOT / ".cache" / "ingest_images"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp")
DOC_TYPES = ("incident_report", "nameplate")
TESSERACT_TAG = "tesseract-v1"     # change when preprocessing changes: invalidates the cache
DEFAULT_LLM_MODEL = "gpt-4o-mini"
TABLES = {"nameplate": "nameplates", "incident_report": "incident_reports"}
KEYS = {"nameplate": "asset_tag", "incident_report": "incident_id"}


# ============================================================== reading images ===

@dataclass
class Extraction:
    """What the engine read from one image, before any parsing."""
    engine: str                       # 'tesseract-v1' | 'llm:<model>'
    texts: dict | None = None         # tesseract: preprocessing variant -> OCR text
    fields: dict | None = None        # llm: field -> value as written (+ doc_type)
    signature: int | None = None      # 1 = ink in the signature box, 0 = none, None = unsure
    error: str | None = None


def read_image(path: Path):
    """BGR image. cv2.imread can't open non-ASCII paths on Windows; decoding bytes can."""
    import cv2

    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"not an image, or unreadable: {path.name}")
    return img


def preprocess_variants(img) -> dict:
    """Three versions of the scan. None reads every field right on its own (on the BLAST-01
    plate, Otsu reads '1000 h' as '1660 h', plain grey reads 'M68' as 'M66'), so each is
    OCR'd and the fields are voted. Order = tie-break priority."""
    import cv2

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    up = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    otsu = cv2.threshold(cv2.fastNlMeansDenoising(up, h=15), 0, 255,
                         cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    adaptive = cv2.adaptiveThreshold(cv2.GaussianBlur(up, (3, 3), 0), 255,
                                     cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)
    return {"gray": up, "otsu": otsu, "adaptive": adaptive}


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def find_tesseract() -> str:
    """TESSERACT_CMD, then PATH, then the default Windows install folders."""
    import shutil

    candidates = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"],
                                                                  "Programs")):
        if base:
            candidates.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    if _is_wsl():
        raise RuntimeError("Tesseract not found in WSL: sudo apt install tesseract-ocr "
                           "(a Windows install can't be used from WSL)")
    raise RuntimeError("Tesseract not found. Install it (Windows: "
                       "https://github.com/UB-Mannheim/tesseract/wiki, macOS: brew install "
                       "tesseract, Linux: apt install tesseract-ocr) or set TESSERACT_CMD.")


def signature_ink(bw) -> int | None:
    """1 if there is ink right of the 'signature' label, 0 if blank, None if the label
    can't be found. Says nothing about WHO signed."""
    import pytesseract

    data = pytesseract.image_to_data(bw, config="--psm 6", output_type=pytesseract.Output.DICT)
    for i, word in enumerate(data["text"]):
        if word.lower().startswith("signature"):
            x, y = data["left"][i] + data["width"][i], data["top"][i]
            h = max(data["height"][i], 10)
            region = bw[max(0, y - 3 * h): y + 2 * h, x + 20: x + 20 + 40 * h]
            return int(region.size > 0 and float((region < 128).mean()) > 0.005)
    return None


def extract_tesseract(path: Path) -> Extraction:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = find_tesseract()
    variants = preprocess_variants(read_image(path))
    texts = {k: pytesseract.image_to_string(im, config="--psm 6") for k, im in variants.items()}
    sig = signature_ink(variants["otsu"]) if classify("\n".join(texts.values())) == \
        "incident_report" else None
    return Extraction(TESSERACT_TAG, texts=texts, signature=sig)


LLM_PROMPT = """You are reading one scanned document from a ship-repair workshop.
Return ONLY a JSON object. Copy every value exactly as written: do not convert dates, units
or numbers, do not fix spelling, and use null for anything blank or unreadable. Never guess.

Incident report form:
{"doc_type": "incident_report", "report_no": "", "date": "", "location": "", "person": "",
 "description": "", "severity": "", "severity_label": "<text in brackets after Severity>",
 "lost_work_days": "", "reporting": "", "immediate_action": "", "root_cause": "",
 "signature_present": true}
Machine nameplate:
{"doc_type": "nameplate", "manufacturer": "<large maker name at the top>", "model": "",
 "serial_no": "", "mfg_date": "", "power": "", "capacity": "", "weight": "", "lubricant": "",
 "grease_interval": "", "asset_tag": ""}
Anything else: {"doc_type": "unknown"}"""

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif"}


def extract_llm(path: Path, model: str) -> Extraction:
    """Vision model through the class LLM endpoint (OpenAI-compatible): OPENAI_BASE_URL and
    OPENAI_API_KEY from .env, as in docs/llm-budget-proxy-setup.md. No other AI service."""
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv()
    mime = _MIME.get(path.suffix.lower())
    if mime is None:  # .tif / .bmp: send as PNG
        import cv2

        ok, buf = cv2.imencode(".png", read_image(path))
        data, mime = buf.tobytes(), "image/png"
    else:
        data = path.read_bytes()
    resp = OpenAI().chat.completions.create(
        model=model, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": LLM_PROMPT},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}]}])
    raw = json.loads(resp.choices[0].message.content or "{}")
    sig = raw.pop("signature_present", None)
    fields = {k: None if v is None else str(v) for k, v in raw.items()}
    return Extraction(f"llm:{model}", fields=fields,
                      signature=None if sig is None else int(bool(sig)))


def cache_path(cache_dir: Path, sha: str, engine: str) -> Path:
    return cache_dir / f"{sha[:16]}_{re.sub(r'[^A-Za-z0-9.-]+', '-', engine)}.json"


def extract(path: Path, sha: str, engine: str, model: str,
            cache_dir: Path | None) -> Extraction:
    """Read one image, from the cache when this exact image was read the same way before.
    Errors are returned (not raised) so one bad image doesn't stop the load."""
    tag = TESSERACT_TAG if engine == "tesseract" else f"llm:{model}"
    cached = cache_path(cache_dir, sha, tag) if cache_dir else None
    if cached and cached.is_file():
        try:
            return Extraction(**json.loads(cached.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            pass
    try:
        ex = extract_tesseract(path) if engine == "tesseract" else extract_llm(path, model)
    except RuntimeError:
        raise                                   # setup problem (no Tesseract): stop
    except Exception as e:  # noqa: BLE001 - recorded as a rejected row
        return Extraction(tag, error=f"{type(e).__name__}: {e}")
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        ignore = cached.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# OCR cache: can hold employee codes from incident forms.\n*\n",
                              encoding="utf-8")
        cached.write_text(json.dumps(asdict(ex), ensure_ascii=False), encoding="utf-8")
    return ex


# ========================================================== parsing OCR text ===

def clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s{2,}", " ", ln).strip(" |_") for ln in text.splitlines() if ln.strip()]


def fuzzy(label: str) -> str:
    """Let OCR insert a space inside a word of a label regex ('Loc ation'), but not inside
    a [character class] or after a backslash."""
    out, depth, prev = [], 0, ""
    for ch in label:
        if ch == "[" and prev != "\\":
            depth += 1
        elif ch == "]" and prev != "\\":
            depth -= 1
        if depth == 0 and ch.isalpha() and prev.isalpha() and len(out) > 1 and out[-2] != "\\":
            out.append(r"\s?")
        out.append(ch)
        prev = ch
    return "".join(out)


def field_after(lines: list[str], label: str) -> str | None:
    """The value after `label` on its line, else the next line."""
    pat = re.compile(fuzzy(label) + r"\s*[:;.]?\s*(.*)$", re.I)
    for i, ln in enumerate(lines):
        m = pat.search(ln)
        if m:
            val = m.group(1).strip()
            if val:
                return val
            return lines[i + 1].strip() if i + 1 < len(lines) else None
    return None


_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "Q": "0", "I": "1", "l": "1", "|": "1",
                            "S": "5", "s": "5"})


def fix_digits(s: str) -> str:
    """Common OCR confusions, for parts of a value that can only be digits."""
    return s.translate(_DIGIT_FIX)


def norm_id(value, prefix_len: int) -> str | None:
    """Upper-case, no spaces; letters->digits only after the alphabetic prefix:
    'IR-2024-9OO' -> 'IR-2024-900', 'EMPOO1' -> 'EMP001'."""
    s = _text(value)
    if not s:
        return None
    v = re.sub(r"\s+", "", s).upper()
    return v[:prefix_len] + fix_digits(v[prefix_len:])


def classify(text: str) -> str:
    t = text.upper()
    if "INCIDENT REPORT" in t or "WSH-F02" in t or re.search(r"REPORT\s*N[O0]", t):
        return "incident_report"
    if "SERIAL" in t or "ASSET TAG" in t or "MFG DATE" in t or "NAMEPLATE" in t:
        return "nameplate"
    return "unknown"


def parse_incident_text(text: str) -> dict:
    L = clean_lines(text)
    label = re.search(fuzzy("Severity") + r"\s*\(([^)]*)\)", text, re.I)
    return {
        "report_no": field_after(L, r"Report\s*N[o0]"),
        "date": field_after(L, r"Date\s*of\s*incident"),
        "location": field_after(L, r"Location\s*/?\s*machine"),
        "person": field_after(L, r"Person\s*involved"),
        "description": field_after(L, r"Description"),
        "severity": field_after(L, r"Severity\s*\(.*?\)"),
        "severity_label": label.group(1).strip() if label else None,
        "lost_work_days": field_after(L, r"Lost\s*work\s*days"),
        "reporting": field_after(L, r"Reporting"),
        "immediate_action": field_after(L, r"Immediate\s*action\s*taken"),
        "root_cause": field_after(L, r"Root\s*cause\s*\(.*?\)"),
    }


def clean_lubricant(v) -> str | None:
    s = _text(v)
    return re.sub(r"\$(?=\d)", "S", s) if s else None     # OCR: 'Tonna $3' -> 'Tonna S3'


# The red italic '(JME)' after ASSET TAG, as OCR reads it: '(JME):', 'UME):', '{ME}:',
# 'GME):', '(JME:'. Only stripped when a bracket or colon closes it, so a tag that starts
# with 'ME' is left alone.
_ASSET_LABEL = re.compile(r"^[(\[{]?\s*[A-Z]?ME\s*(?:[)\]}]\s*[:;.]?|[:;.])\s*", re.I)


def asset_tag_value(v) -> str | None:
    """The value after ASSET TAG with the '(JME)' label removed, however OCR garbled it:
    '{ME}: CNC-01' -> 'CNC-01', 'GME): WELD-02' -> 'WELD-02', 'UME): BLAST!' -> 'BLAST!'."""
    s = _text(v)
    return _text(_ASSET_LABEL.sub("", s)) if s else None


def parse_nameplate_text(text: str) -> dict:
    L = clean_lines(text)
    maker = next((ln for ln in L if re.fullmatch(r"[A-Z][A-Z &.\-]{2,}", ln)), None)
    return {
        "manufacturer": maker,
        "model": field_after(L, r"MODEL"),
        "serial_no": field_after(L, r"SERIAL\s*N[O0]"),
        "mfg_date": field_after(L, r"MFG\s*DATE"),
        "power": field_after(L, r"POWER"),
        "capacity": field_after(L, r"CAPACITY"),
        "weight": field_after(L, r"WEIGHT"),
        "lubricant": clean_lubricant(field_after(L, r"LUBRICANT")),
        "grease_interval": field_after(L, r"GREASE\s*INTERVAL"),
        "asset_tag": asset_tag_value(field_after(L, r"ASSET\s*TAG")),
    }


PARSERS = {"incident_report": parse_incident_text, "nameplate": parse_nameplate_text}


# ------------------------------------------------------------------ voting ---

def scale_from_label(label) -> str | None:
    """'1 = MOST SEVERE' -> '1_worst'; '5 = most severe' -> '5_worst';
    '1 = least severe' -> '5_worst'. Unreadable -> None."""
    s = _text(label)
    if not s:
        return None
    m = re.search(r"([15lI|])\s*[=:\-]*\s*(most|worst|least|lowest|highest)", s, re.I)
    if not m:
        return None
    n = 1 if m.group(1) in "1lI|" else 5
    worst = m.group(2).lower() in ("most", "worst", "highest")
    return f"{n if worst else 6 - n}_worst"


ISO_VG = {2, 3, 5, 7, 10, 15, 22, 32, 46, 68, 100, 150, 220, 320, 460, 680, 1000, 1500}

def has_viscosity_grade(v) -> bool:
    """Industrial oils end in an ISO VG grade: 'Shell Tonna S3 M68' -> 68."""
    m = re.search(r"(\d+)\s*$", _text(v) or "")
    return bool(m) and int(m.group(1)) in ISO_VG

# Machine ids: letters, dash, digits, with an optional letter before the digits (PMP-T01).
TAG = r"[A-Z]{2,6}-[A-HJKMNPR-Z]?\d{2,3}"   # PMP-T01 yes; CRN-O02, CRN-G2, BLAST-O1 no
VALIDATORS = {
    "incident_report": {
        "report_no": lambda v: bool(re.fullmatch(r"IR-\d{4}-\d{1,5}", norm_id(v, 2) or "")),
        "date": lambda v: parse_date(v)[0] is not None,
        "person": lambda v: bool(re.fullmatch(r"EMP\d{3,}", norm_id(v, 3) or "")),
        "severity": lambda v: v.strip() in {"1", "2", "3", "4", "5"},
        "severity_label": lambda v: scale_from_label(v) is not None,
        "lost_work_days": lambda v: v.strip().isdigit(),
        "location": lambda v: bool(re.search(rf"\b{TAG}\b", v.upper())),
    },
    "nameplate": {
        "asset_tag": lambda v: bool(re.fullmatch(TAG, v.strip().upper())),
        "serial_no": lambda v: bool(re.fullmatch(r"[A-Z]{2}\d{6,}", norm_id(v, 2) or "")),
        "mfg_date": lambda v: bool(re.fullmatch(r"\d{1,2}\s*/\s*\d{4}", v.strip())),
        "lubricant": has_viscosity_grade,
        "grease_interval": lambda v: bool(re.fullmatch(r"\d[\d,]*\s*(h|hrs?|hours?)", v.strip(),
                                                       re.I)),
    },
}


def _same(v) -> str:
    """Values that differ only in case, spaces or punctuation count as the same reading."""
    return re.sub(r"[\W_]", "", str(v).lower())


def vote(candidates: dict[str, dict], doc_type: str) -> tuple[dict, list[dict]]:
    """candidates: {variant: parsed fields}. Per field: keep the values that pass the field's
    format check (if any do), take the reading most variants agree on, ties to the first
    variant. Returns (fields, notes); a note records a disagreement or a failed check."""
    checks = VALIDATORS.get(doc_type, {})
    order = list(candidates)
    merged, notes = {}, []
    for fld in dict.fromkeys(k for d in candidates.values() for k in d):
        vals = [(n, _text(candidates[n].get(fld))) for n in order]
        vals = [(n, v) for n, v in vals if v is not None]
        if not vals:
            merged[fld] = None
            continue
        ok = checks.get(fld)
        pool = [(n, v) for n, v in vals if ok is None or ok(v)] or vals
        groups: dict = {}
        for n, v in pool:
            groups.setdefault(_same(v), []).append((n, v))
        best = max(groups.values(), key=lambda g: (len(g), -order.index(g[0][0])))[0][1]
        merged[fld] = best
        if len({_same(v) for _, v in vals}) > 1:
            notes.append({"field": fld, "issue": "disagreed", "chose": best,
                          "readings": {n: v for n, v in vals}})
        if ok and not ok(best):
            notes.append({"field": fld, "issue": "failed_format", "chose": best})
    return merged, notes


# ============================================================ cleaning steps ===

def _bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


def _num(pattern: str, v, cast=float):
    m = re.search(pattern, _text(v) or "", re.I)
    return cast(m.group(1).replace(",", "")) if m else None


def _int_text(v) -> int | None:
    """'4' -> 4, 'O' (OCR for 0) -> 0, '4.5' / 'abc' -> None."""
    s = _text(v)
    if s is None:
        return None
    s = fix_digits(s)
    return int(s) if re.fullmatch(r"\d+", s) else None


# ------------------------------------------------------------ incident forms ---

REPORT_NO = re.compile(r"^IR-?(\d{4})-?(\d{1,5})$")
# The optional letter before the digits is lazy, so 'LTH-O1' is LTH-01 (O read for 0)
# while 'PMP-T01' keeps its T.
LOCATION_TAG = re.compile(r"\b([A-Za-z]{2,6})\s*-\s*([A-Za-z]??)([0-9OoIlSs]{1,3})\b")


def machine_tag(m: re.Match | None) -> str | None:
    """'pmp-tO1' -> 'PMP-T01': letters upper-cased, OCR slips fixed in the digits only."""
    return f"{m.group(1).upper()}-{m.group(2).upper()}{fix_digits(m.group(3))}" if m else None


def clean_report_ids(df: pd.DataFrame) -> pd.DataFrame:
    """'IR-2024-9OO' -> report_no 'IR-2024-900', incident_id 900, report_year 2024."""
    df = df.copy()
    df["report_no"] = df["report_no"].map(lambda v: norm_id(v, 2))
    m = df["report_no"].map(lambda s: REPORT_NO.match(s) if s else None)
    df["incident_id"] = m.map(lambda x: int(x.group(2)) if x else None).astype("Int64")
    df["report_year"] = m.map(lambda x: int(x.group(1)) if x else None).astype("Int64")
    return df


def clean_incident_fields(df: pd.DataFrame) -> pd.DataFrame:
    """machine_id from the location ('LTH-01 — workshop bay' -> 'LTH-01'); emp_code only if
    it IS an employee code (anything else, e.g. a name, is not stored: PDPA); texts trimmed."""
    df = df.copy()
    df["location_raw"] = df["location"].map(_text)

    def machine(v):
        return machine_tag(LOCATION_TAG.search(_text(v) or ""))
    df["machine_id"] = df["location"].map(machine)
    code = df["person"].map(lambda v: norm_id(v, 3))
    is_code = code.map(lambda c: bool(c and re.fullmatch(r"EMP\d{3,}", c)))
    df["emp_code"] = code.where(is_code, None)
    df["person_given"] = df["person"].map(_text).notna()
    df["person_is_code"] = is_code
    for c in ("description", "reporting", "immediate_action", "root_cause"):
        df[c] = df[c].map(_text)
    df["root_cause_attached"] = df["root_cause"].map(
        lambda v: None if v is None else int(bool(re.search(r"attach", v, re.I))))
    return df


def parse_incident_numbers(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for src, dst in (("severity", "severity_as_logged"), ("lost_work_days", "lost_days")):
        df[f"{dst}_given"] = df[src].map(_text).notna()
        df[dst] = df[src].map(_int_text).astype("Int64")
    return df


def parse_incident_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Same parser and day-first rule as ingest_cmms.py: '01/07/2024' = 1 July 2024."""
    df = df.copy()
    parsed = df["date"].map(parse_date)
    df["incident_date"] = parsed.map(lambda t: t[0])
    df["incident_date_format"] = parsed.map(lambda t: t[1])
    return df


def form_severity(df: pd.DataFrame, cutover: date = SEVERITY_CUTOVER,
                  ambiguous_until: date | None = SEVERITY_AMBIGUOUS_UNTIL) -> pd.DataFrame:
    """severity_scale = the scale printed on the form; severity_5_worst converted with it.
    date_rule_scale = what ingest_cmms.py's date rule says for that date, for comparison."""
    df = df.copy()
    df["severity_scale"] = df["severity_label"].map(scale_from_label).fillna("unknown")
    sev = df["severity_as_logged"]
    ok = _bool(sev.between(1, 5))
    five = sev.where(ok & (df["severity_scale"] == "5_worst"))
    one = (6 - sev).where(ok & (df["severity_scale"] == "1_worst"))
    df["severity_5_worst"] = five.fillna(one).astype("Int64")

    when = pd.to_datetime(df["incident_date"])
    ambiguous = (_bool((when >= pd.Timestamp(cutover)) & (when <= pd.Timestamp(ambiguous_until)))
                 if ambiguous_until else pd.Series(False, index=df.index))
    df["date_rule_scale"] = np.select(
        [when.isna(), _bool(when < pd.Timestamp(cutover)), ambiguous],
        ["unknown", "1_worst", "ambiguous"], default="5_worst")
    return df


def add_incident_warnings(df: pd.DataFrame, machine_ids: set | None = None) -> pd.DataFrame:
    df = df.copy()
    known_scale = df["severity_scale"] != "unknown"
    flags = {
        "bad_report_no": df["report_no"].notna() & df["incident_id"].isna(),
        "report_year_differs_from_date": df.apply(
            lambda r: pd.notna(r["report_year"]) and r["incident_date"] is not None
            and r["report_year"] != r["incident_date"].year, axis=1),
        "missing_date": df["incident_date_format"] == "missing",
        "bad_date": df["incident_date_format"].isin(["zero_date", "unparseable"]),
        "date_assumed_day_first": df["incident_date_format"].str.startswith("dmy"),
        "missing_machine_id": df["machine_id"].isna(),
        "unknown_machine": (df["machine_id"].map(lambda m: m not in machine_ids)
                            & df["machine_id"].notna()) if machine_ids is not None else False,
        "missing_emp_code": ~df["person_given"],
        "person_not_an_emp_code": df["person_given"] & ~df["person_is_code"],
        "missing_severity": ~df["severity_as_logged_given"],
        "bad_severity": df["severity_as_logged_given"] & ~_bool(
            df["severity_as_logged"].between(1, 5)),
        "severity_scale_not_on_form": ~known_scale,
        "form_scale_contradicts_date_rule": known_scale
        & df["date_rule_scale"].isin(["1_worst", "5_worst"])
        & (df["severity_scale"] != df["date_rule_scale"]),
        "form_scale_in_cutover_month": known_scale & (df["date_rule_scale"] == "ambiguous"),
        "bad_lost_days": df["lost_days_given"] & df["lost_days"].isna(),
        "root_cause_in_attachment": df["root_cause_attached"] == 1,
        "signature_missing": df["signature_present"] == 0,
        "signature_unknown": df["signature_present"].isna(),
    }
    return _with_warnings(df, flags)


# ---------------------------------------------------------------- nameplates ---

def clean_nameplate_ids(df: pd.DataFrame) -> pd.DataFrame:
    """asset_tag 'blast-O1' -> 'BLAST-01', 'PMP-TO1' -> 'PMP-T01' (letters stay letters);
    serial 'CU67448B4'-style OCR slips fixed only after the 2-letter prefix."""
    df = df.copy()

    def tag(v):
        return machine_tag(re.match(r"\s*([A-Za-z]{2,6})\s*-?\s*([A-Za-z]??)([0-9OoIlSs]{1,3})\b",
                                    _text(v) or ""))
    df["asset_tag"] = df["asset_tag"].map(tag)
    df["serial_no"] = df["serial_no"].map(lambda v: norm_id(v, 2))
    for c in ("manufacturer", "model", "lubricant"):
        df[c] = df[c].map(_text)
    df["lubricant"] = df["lubricant"].map(clean_lubricant)
    return df


def parse_nameplate_values(df: pd.DataFrame) -> pd.DataFrame:
    """Numbers and units. Weight in lb or t is converted to kg (flagged), like the spares
    sheet's inches; the text as printed stays in the raw row."""
    df = df.copy()
    for c in ("mfg_date", "power", "capacity", "weight", "grease_interval"):
        df[f"{c}_given"] = df[c].map(_text).notna()

    def month(v):
        m = re.search(r"(\d{1,2})\s*/\s*(\d{4})", fix_digits(_text(v) or ""))
        return f"{m.group(2)}-{int(m.group(1)):02d}" if m and 1 <= int(m.group(1)) <= 12 else None
    df["mfg_month"] = df["mfg_date"].map(month)
    df["power_kw"] = df["power"].map(lambda v: _num(r"(\d+(?:\.\d+)?)\s*kW", v))
    df["phases"] = df["power"].map(lambda v: _num(r"(\d)\s*PH", v, int)).astype("Int64")
    df["voltage_min_v"] = df["power"].map(
        lambda v: _num(r"(\d{3})\s*[-–]\s*\d{3}\s*V", v, int)).astype("Int64")
    df["voltage_max_v"] = df["power"].map(
        lambda v: _num(r"\d{3}\s*[-–]\s*(\d{3})\s*V", v, int)).astype("Int64")
    df["capacity_raw"] = df["capacity"].map(_text)
    df["rated_current_a"] = df["capacity"].map(lambda v: _num(r"(\d+(?:\.\d+)?)\s*A\b", v))
    df["duty_cycle_pct"] = df["capacity"].map(lambda v: _num(r"@\s*(\d+(?:\.\d+)?)\s*%", v))

    def weight(v):
        m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(kg|lbs?|t)\b", _text(v) or "", re.I)
        if not m:
            return None, None
        n, unit = float(m.group(1).replace(",", "")), m.group(2).lower()
        factor = {"kg": 1.0, "t": 1000.0}.get(unit, 0.45359237)
        return round(n * factor, 2), unit
    w = df["weight"].map(weight)
    df["weight_kg"] = w.map(lambda t: t[0])
    df["weight_unit"] = w.map(lambda t: t[1])
    df["grease_interval_h"] = df["grease_interval"].map(
        lambda v: _num(r"(\d[\d,]*)\s*(?:h|hrs?|hours?)\b", fix_digits(_text(v) or ""), int)
    ).astype("Int64")
    return df


def add_nameplate_warnings(df: pd.DataFrame, machine_ids: set | None = None) -> pd.DataFrame:
    df = df.copy()
    flags = {
        "unknown_machine": (df["asset_tag"].map(lambda m: m not in machine_ids)
                            & df["asset_tag"].notna()) if machine_ids is not None else False,
        "placeholder_manufacturer": df["manufacturer"].str.upper().isin(
            ["CUSTOM", "GENERIC", "N/A", "UNKNOWN"]),
        "bad_mfg_date": df["mfg_date_given"] & df["mfg_month"].isna(),
        "bad_power": df["power_given"] & df["power_kw"].isna(),
        "bad_weight": df["weight_given"] & df["weight_kg"].isna(),
        "weight_converted_to_kg": df["weight_unit"].isin(["lb", "lbs", "t"]),
        "missing_lubricant": df["lubricant"].isna(),
        "lubricant_without_grade": df["lubricant"].notna()
        & ~df["lubricant"].map(has_viscosity_grade),
        "missing_grease_interval": ~df["grease_interval_given"],
        "bad_grease_interval": df["grease_interval_given"] & df["grease_interval_h"].isna(),
        # '450 A @ 60%' is how welding power sources are rated (trap list I2, docs/trap_list.md)
        "duty_cycle_rating_on_non_welder": df["duty_cycle_pct"].notna()
        & ~df["asset_tag"].fillna("").str.startswith("WELD"),
    }
    return _with_warnings(df, flags)


# ----------------------------------------------------------------- common ---

def _with_warnings(df: pd.DataFrame, flags: dict) -> pd.DataFrame:
    """Comma-separated warning names; OCR notes become ocr_disagreed_<field> /
    failed_format_<field>."""
    f = pd.DataFrame({k: _bool(pd.Series(v, index=df.index)) for k, v in flags.items()},
                     index=df.index)
    ocr = df["ocr_notes"].map(lambda notes: [
        f"{'ocr_disagreed' if n['issue'] == 'disagreed' else 'failed_format'}_{n['field']}"
        for n in (notes or [])])
    df["warnings"] = [",".join([*f.columns[row].tolist(), *extra]) or None
                      for row, extra in zip(f.to_numpy(), ocr)]
    return df


def split_rejects(df: pd.DataFrame, key: str, seen: dict, value_cols: list[str]) -> pd.DataFrame:
    """missing_key / duplicate_key / duplicate_key_conflict. The first image with a key
    wins; `seen` (key -> raw_id) also holds keys already loaded from other folders."""
    df = df.copy()
    reason = pd.Series(pd.NA, index=df.index, dtype="string")
    reason[df[key].isna()] = "missing_key"
    signature = df[value_cols].apply(
        lambda r: "|".join("" if pd.isna(v) else str(v) for v in r), axis=1)
    dup_of = pd.Series(pd.NA, index=df.index, dtype="Int64")
    first: dict = {}
    for i, k, raw_id in zip(df.index, df[key], df["raw_id"]):
        if pd.notna(reason[i]):
            continue
        k = int(k) if key == "incident_id" else k
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


# ===================================================================== tables ===

CLEAN_DDL = {
    "nameplate": (
        ("asset_tag", "VARCHAR(20) NOT NULL COMMENT '= cmms machine_id'"),
        ("manufacturer", "VARCHAR(100) NULL"),
        ("model", "VARCHAR(100) NULL"),
        ("serial_no", "VARCHAR(50) NULL"),
        ("mfg_month", "CHAR(7) NULL COMMENT 'YYYY-MM, as on the plate'"),
        ("power_kw", "DECIMAL(8,2) NULL"),
        ("phases", "TINYINT NULL"),
        ("voltage_min_v", "SMALLINT NULL"),
        ("voltage_max_v", "SMALLINT NULL"),
        ("capacity_raw", "VARCHAR(100) NULL COMMENT 'as printed'"),
        ("rated_current_a", "DECIMAL(8,2) NULL"),
        ("duty_cycle_pct", "DECIMAL(5,2) NULL"),
        ("weight_kg", "DECIMAL(10,2) NULL"),
        ("lubricant", "VARCHAR(100) NULL"),
        ("grease_interval_h", "INT NULL"),
    ),
    "incident_report": (
        ("incident_id", "INT NOT NULL COMMENT 'from the report no: IR-2024-900 -> 900'"),
        ("report_no", "VARCHAR(32) NULL"),
        ("incident_date", "DATE NULL"),
        ("machine_id", "VARCHAR(20) NULL COMMENT 'from Location/machine'"),
        ("location_raw", "VARCHAR(255) NULL"),
        ("emp_code", "VARCHAR(20) NULL COMMENT 'only if the form gives an EMP code'"),
        ("description", "VARCHAR(255) NULL"),
        ("severity_as_logged", "INT NULL COMMENT 'as written; meaning depends on severity_scale'"),
        ("severity_scale", "VARCHAR(12) NOT NULL COMMENT 'scale printed on the form: "
                           "1_worst | 5_worst | unknown'"),
        ("severity_5_worst", "TINYINT NULL COMMENT 'on the 5 = worst scale, using the printed scale'"),
        ("lost_days", "INT NULL"),
        ("reporting", "VARCHAR(100) NULL"),
        ("immediate_action", "VARCHAR(500) NULL"),
        ("root_cause", "VARCHAR(500) NULL"),
        ("root_cause_attached", "TINYINT(1) NULL COMMENT '1 = refers to an attachment (5-Why)'"),
        ("signature_present", "TINYINT(1) NULL COMMENT 'ink in the signature box; signer not checked'"),
    ),
}
CLEAN_COLUMNS = {t: ["raw_id", *(c for c, _ in d)] for t, d in CLEAN_DDL.items()}
RAW_COLUMNS = ["raw_id", "run_id", "source_folder", "source_file", "image_sha256", "doc_type",
               "ocr_engine", "fields_raw", "ocr_notes", "ocr_text", "status", "reject_reason",
               "duplicate_of_raw_id", "warnings"]


def ddl(prefix: str) -> list[str]:
    raw = f"{prefix}documents_raw"
    out = [f"""
    CREATE TABLE IF NOT EXISTS {raw} (
        raw_id              BIGINT       NOT NULL PRIMARY KEY,
        run_id              BIGINT       NULL,
        source_folder       VARCHAR(255) NOT NULL,
        source_file         VARCHAR(255) NOT NULL COMMENT 'image file name',
        image_sha256        CHAR(64)     NOT NULL COMMENT 'this image, byte for byte',
        doc_type            VARCHAR(20)  NOT NULL COMMENT 'incident_report | nameplate | unknown',
        ocr_engine          VARCHAR(80)  NOT NULL,
        fields_raw          TEXT         NULL COMMENT 'JSON: each field exactly as read',
        ocr_notes           TEXT         NULL COMMENT 'JSON: OCR disagreements and failed checks',
        ocr_text            MEDIUMTEXT   NULL COMMENT 'everything OCR read (tesseract)',
        status              VARCHAR(10)  NOT NULL COMMENT 'loaded | rejected',
        reject_reason       VARCHAR(40)  NULL,
        duplicate_of_raw_id BIGINT       NULL COMMENT 'for duplicates: the raw row that was kept',
        warnings            VARCHAR(500) NULL,
        KEY ix_raw_folder (source_folder, source_file),
        KEY ix_raw_status (status, reject_reason),
        KEY ix_raw_sha (image_sha256),
        CONSTRAINT fk_{raw}_run FOREIGN KEY (run_id) REFERENCES ingest_runs (run_id)
            ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """]
    for doc_type, cols in CLEAN_DDL.items():
        t = prefix + TABLES[doc_type]
        body = "".join(f"        `{c}` {d},\n" for c, d in cols)
        extra = "        KEY ix_{t}_machine (machine_id),\n".format(t=t) \
            if doc_type == "incident_report" else ""
        out += [f"""
    CREATE TABLE IF NOT EXISTS {t} (
        id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
        raw_id              BIGINT       NOT NULL,
{body}        UNIQUE KEY uq_{t}_raw (raw_id),
        UNIQUE KEY uq_{t}_key (`{KEYS[doc_type]}`),
{extra}        CONSTRAINT fk_{t}_raw FOREIGN KEY (raw_id) REFERENCES {raw} (raw_id)
            ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """, f"""
    CREATE OR REPLACE VIEW {t}_audit AS
    SELECT c.*, r.source_folder, r.source_file, r.image_sha256, r.ocr_engine, r.warnings,
           r.run_id, u.file_sha256 AS folder_sha256, u.started_at AS loaded_at
    FROM {t} c
    JOIN {raw} r ON r.raw_id = c.raw_id
    LEFT JOIN ingest_runs u ON u.run_id = r.run_id
    """]
    return out


# ==================================================================== process ===

@dataclass
class ImageRead:
    source_file: str
    image_sha256: str
    extraction: Extraction


def interpret(ex: Extraction) -> tuple[str, dict, list, str | None]:
    """(doc_type, fields as read, OCR notes, OCR text) from what the engine returned."""
    if ex.error:
        return "unknown", {}, [], None
    if ex.texts is not None:
        joined = "\n\n".join(f"=== {k} ===\n{t}" for k, t in ex.texts.items())
        doc_type = classify(joined)
        if doc_type not in PARSERS:
            return doc_type, {}, [], joined
        fields, notes = vote({k: PARSERS[doc_type](t) for k, t in ex.texts.items()}, doc_type)
        return doc_type, fields, notes, joined
    fields = dict(ex.fields or {})
    raw_type = (fields.pop("doc_type", None) or "").lower().replace(" ", "_")
    doc_type = ("incident_report" if raw_type.startswith("incident") else
                "nameplate" if raw_type.startswith("nameplate") else "unknown")
    if doc_type in PARSERS:
        fields, notes = vote({"llm": fields}, doc_type)   # format checks only
        return doc_type, fields, notes, None
    return doc_type, {}, [], None


def process(reads: list[ImageRead], stats: Counter, *, source_folder: str = "",
            run_id: int | None = None, first_raw_id: int = 1, seen: dict | None = None,
            seen_images: dict | None = None, machine_ids: set | None = None,
            cutover: date = SEVERITY_CUTOVER,
            ambiguous_until: date | None = SEVERITY_AMBIGUOUS_UNTIL) -> dict:
    """Clean every image. Returns {'documents': raw rows, 'nameplate': clean rows,
    'incident_report': clean rows} and fills `stats`.

    For a real load, `seen` ({doc_type: {key: raw_id}}), `seen_images` (sha -> raw_id) and
    `machine_ids` (cmms_machines) come from the database.
    """
    seen = {t: dict((seen or {}).get(t, {})) for t in DOC_TYPES}
    seen_images = dict(seen_images or {})
    rows = []
    for i, r in enumerate(reads):
        doc_type, fields, notes, text = interpret(r.extraction)
        raw_id = first_raw_id + i
        reason, dup = None, None
        if r.extraction.error:
            reason = "unreadable_image"
        elif r.image_sha256 in seen_images:
            reason, dup = "duplicate_image", seen_images[r.image_sha256]
        elif doc_type not in PARSERS:
            reason = "unknown_document_type"
        if reason != "duplicate_image" and not r.extraction.error:
            seen_images.setdefault(r.image_sha256, raw_id)
        rows.append({"raw_id": raw_id, "run_id": run_id, "source_folder": source_folder,
                     "source_file": r.source_file, "image_sha256": r.image_sha256,
                     "doc_type": doc_type, "ocr_engine": r.extraction.engine,
                     "fields": fields, "notes": notes, "signature": r.extraction.signature,
                     "fields_raw": json.dumps(fields, ensure_ascii=False) if fields else None,
                     "ocr_notes": json.dumps(notes, ensure_ascii=False) if notes else None,
                     "ocr_text": text or r.extraction.error, "reject_reason": reason,
                     "duplicate_of_raw_id": dup, "warnings": None})
    docs = pd.DataFrame(rows, columns=[*RAW_COLUMNS, "fields", "notes", "signature"])
    docs = docs.astype({"reject_reason": object, "duplicate_of_raw_id": object,
                        "warnings": object})

    out = {}
    for doc_type in DOC_TYPES:
        sel = docs[(docs["doc_type"] == doc_type) & docs["reject_reason"].isna()]
        frame = pd.DataFrame([{**f, "raw_id": rid, "ocr_notes": n, "signature_present": s}
                              for f, rid, n, s in zip(sel["fields"], sel["raw_id"],
                                                      sel["notes"], sel["signature"])])
        fields = [k for k in (parse_incident_text("") if doc_type == "incident_report"
                              else parse_nameplate_text(""))]
        for c in [*fields, "raw_id", "ocr_notes", "signature_present"]:
            if c not in frame:
                frame[c] = pd.Series(dtype=object)
        if doc_type == "incident_report":
            frame["signature_present"] = frame["signature_present"].astype("Int64")
            cleaned = (frame
                       .pipe(clean_report_ids)
                       .pipe(clean_incident_fields)
                       .pipe(parse_incident_numbers)
                       .pipe(parse_incident_dates)
                       .pipe(form_severity, cutover, ambiguous_until)
                       .pipe(add_incident_warnings, machine_ids))
        else:
            cleaned = (frame
                       .pipe(clean_nameplate_ids)
                       .pipe(parse_nameplate_values)
                       .pipe(add_nameplate_warnings, machine_ids))
        key = KEYS[doc_type]
        values = [c for c in CLEAN_COLUMNS[doc_type] if c not in ("raw_id", key)]
        cleaned = split_rejects(cleaned, key, seen[doc_type], values)
        rejected = cleaned["reject_reason"].notna()
        out[doc_type] = cleaned.loc[~rejected, CLEAN_COLUMNS[doc_type]].reset_index(drop=True)

        idx = docs.set_index("raw_id").index
        for rid, reason, dup, warn in zip(cleaned["raw_id"], cleaned["reject_reason"],
                                          cleaned["duplicate_of_raw_id"], cleaned["warnings"]):
            pos = idx.get_loc(rid)
            if pd.notna(reason):
                docs.iat[pos, docs.columns.get_loc("reject_reason")] = str(reason)
                docs.iat[pos, docs.columns.get_loc("duplicate_of_raw_id")] = \
                    None if pd.isna(dup) else int(dup)
            else:
                docs.iat[pos, docs.columns.get_loc("warnings")] = warn

        t = TABLES[doc_type]
        stats[f"{t}:loaded"] += int((~rejected).sum())
        for w in cleaned.loc[~rejected, "warnings"].dropna():
            stats.update(f"{t}:warn:{x}" for x in w.split(","))
        if doc_type == "incident_report":
            stats.update(f"{t}:scale:{s}" for s in cleaned.loc[~rejected, "severity_scale"])

    rejected = docs["reject_reason"].notna()
    docs["status"] = np.where(rejected, "rejected", "loaded")
    out["documents"] = docs[RAW_COLUMNS]
    stats["rows_read"] += len(docs)
    stats["rows_loaded"] += int((~rejected).sum())
    stats["rows_rejected"] += int(rejected.sum())
    stats.update(f"doc_type:{d}" for d in docs["doc_type"])
    stats.update(f"engine:{e}" for e in docs["ocr_engine"])
    stats.update(f"rejected:{r}" for r in docs.loc[rejected, "reject_reason"])
    stats["machines_checked"] = int(machine_ids is not None)
    stats["machines_known"] = len(machine_ids or ())
    return out


# ======================================================================= load ===

def create_tables(engine, prefix: str, recreate: bool) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:  # DDL commits implicitly in MySQL
        conn.execute(text(RUNS_DDL))
        if recreate:
            for t in TABLES.values():
                conn.execute(text(f"DROP VIEW IF EXISTS {prefix}{t}_audit"))
                conn.execute(text(f"DROP TABLE IF EXISTS {prefix}{t}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {prefix}documents_raw"))
        for stmt in ddl(prefix):
            conn.execute(text(stmt))


def _insert(table: str, columns: list[str]):
    from sqlalchemy import text

    return text(f"INSERT INTO {table} ({', '.join(f'`{c}`' for c in columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})")


def table_exists(conn, table: str) -> bool:
    from sqlalchemy import text

    return bool(conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = :t"), {"t": table}).scalar_one())


def load(engine, prefix: str, reads: list[ImageRead], folder: str, stats: Counter,
         run_id: int, cmms_prefix: str = "cmms_", **options) -> dict:
    """Replace this folder's rows in ONE transaction; other folders are kept."""
    from sqlalchemy import text

    raw = f"{prefix}documents_raw"
    with engine.begin() as conn:
        # Deleting raw rows cascades to their clean rows.
        conn.execute(text(f"DELETE FROM {raw} WHERE source_folder = :f"), {"f": folder})
        first = conn.execute(text(f"SELECT COALESCE(MAX(raw_id), 0) + 1 FROM {raw}")).scalar_one()
        seen = {d: {k: r for k, r in conn.execute(
            text(f"SELECT `{KEYS[d]}`, raw_id FROM {prefix}{TABLES[d]}"))} for d in DOC_TYPES}
        seen_images = {s: r for s, r in conn.execute(
            text(f"SELECT image_sha256, raw_id FROM {raw} WHERE status = 'loaded'"))}
        machines = f"{cmms_prefix}machines"
        machine_ids = ({m for (m,) in conn.execute(text(f"SELECT machine_id FROM {machines}"))}
                       if table_exists(conn, machines) else None)

        tables = process(reads, stats, source_folder=folder, run_id=run_id, first_raw_id=first,
                         seen=seen, seen_images=seen_images, machine_ids=machine_ids, **options)
        docs = tables["documents"]
        if len(docs):
            conn.execute(_insert(raw, RAW_COLUMNS), to_records(docs))
        for d in DOC_TYPES:
            if len(tables[d]):
                conn.execute(_insert(prefix + TABLES[d], CLEAN_COLUMNS[d]), to_records(tables[d]))
        print(f"  {raw}: {len(docs)} rows; " + ", ".join(
            f"{prefix}{TABLES[d]}: {len(tables[d])}" for d in DOC_TYPES))
    return tables


# ===================================================================== report ===

def _items(stats: Counter, prefix: str) -> str:
    items = {k[len(prefix):]: v for k, v in stats.items() if k.startswith(prefix) and v}
    if not items:
        return ""
    return (", ".join(f"{k}={v}" for k, v in sorted(items.items()))
            + f" (total {sum(items.values())})")


def print_report(stats: Counter, cmms_prefix: str = "cmms_") -> None:
    print(f"\nRows read: {stats['rows_read']}   loaded: {stats['rows_loaded']}   "
          f"rejected: {stats['rows_rejected']}")
    print(f"Document types: {_items(stats, 'doc_type:')}")
    print(f"OCR engine: {_items(stats, 'engine:')}")
    if rej := _items(stats, "rejected:"):
        print(f"Rejected rows: {rej}")
    for t in TABLES.values():
        print(f"\n{t}: loaded {stats[f'{t}:loaded']}")
        if w := _items(stats, f"{t}:warn:"):
            print(f"  Loaded with warnings: {w}")
        if s := _items(stats, f"{t}:scale:"):
            print(f"  Severity scale on forms: {s}")
    if stats["machines_checked"]:
        print(f"\nMachine ids: checked against {cmms_prefix}machines "
              f"({stats['machines_known']} machines)")
    else:
        print(f"\nMachine ids: not checked here ({cmms_prefix}machines not loaded, or a dry "
              f"run); relationships.py checks them after the loads")


def for_display(doc_type: str, df: pd.DataFrame) -> pd.DataFrame:
    """Columns worth seeing in a dry run (no long texts)."""
    cols = (["raw_id", "incident_id", "incident_date", "machine_id", "emp_code",
             "severity_as_logged", "severity_scale", "severity_5_worst", "lost_days",
             "signature_present"] if doc_type == "incident_report" else
            ["raw_id", "asset_tag", "manufacturer", "model", "serial_no", "mfg_month",
             "power_kw", "weight_kg", "lubricant", "grease_interval_h"])
    return df[cols]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", nargs="?", default=os.getenv("IMAGES_DIR", DEFAULT_FOLDER),
                    help="folder of scans (default %(default)s)")
    ap.add_argument("--prefix", default="img_", help="table name prefix (default %(default)s)")
    ap.add_argument("--cmms-prefix", default="cmms_",
                    help="ingest_cmms.py's prefix, to check machine ids (default %(default)s)")
    ap.add_argument("--engine", choices=["tesseract", "llm"], default="tesseract",
                    help="tesseract (default, offline) or llm: the vision model behind "
                         "OPENAI_BASE_URL (the class endpoint)")
    ap.add_argument("--llm-model", default=os.getenv("IMAGE_LLM_MODEL", DEFAULT_LLM_MODEL),
                    help="model for --engine llm (default %(default)s)")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE),
                    help="OCR cache, keyed by image SHA-256 (default %(default)s)")
    ap.add_argument("--no-cache", action="store_true", help="always re-read every image")
    ap.add_argument("--severity-cutover", type=date.fromisoformat, default=SEVERITY_CUTOVER,
                    help="for the date-rule comparison; same as ingest_cmms.py "
                         "(default %(default)s)")
    ap.add_argument("--severity-ambiguous-until", default=SEVERITY_AMBIGUOUS_UNTIL,
                    type=lambda s: None if s.lower() == "none" else date.fromisoformat(s),
                    help="same as ingest_cmms.py (default %(default)s)")
    ap.add_argument("--rejects", help="also write rejected images + reason to this CSV")
    ap.add_argument("--recreate", action="store_true",
                    help="drop and recreate the tables (first run, or after a schema change)")
    add_common_args(ap)
    a = ap.parse_args()

    folder = Path(a.folder)
    if not folder.is_dir():
        sys.exit(f"Folder not found: {a.folder}")
    for t in [*TABLES.values(), "documents_raw"]:
        safe_identifier(f"{a.prefix}{t}_audit")
    safe_identifier(f"{a.cmms_prefix}machines")
    files = folder_files(folder, IMAGE_EXTS)
    if not files:
        sys.exit(f"No images ({', '.join(IMAGE_EXTS)}) in {a.folder}")
    options = {"cutover": a.severity_cutover, "ambiguous_until": a.severity_ambiguous_until}
    cache = None if a.no_cache else Path(a.cache_dir)

    print(f"Reading {len(files)} image(s) in {a.folder} with {a.engine}"
          + (f" ({a.llm_model})" if a.engine == "llm" else ""))
    reads = []
    try:
        for i, p in enumerate(files, 1):
            sha = file_sha256(str(p))
            ex = extract(p, sha, a.engine, a.llm_model, cache)
            reads.append(ImageRead(p.name, sha, ex))
            print(f"  [{i}/{len(files)}] {p.name}: "
                  + (f"ERROR {ex.error}" if ex.error else interpret(ex)[0]))
    except RuntimeError as e:
        sys.exit(str(e))
    stats: Counter = Counter()

    if a.dry_run:
        tables = process(reads, stats, source_folder=folder.name, **options)
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            for d in DOC_TYPES:
                if len(tables[d]):
                    print(f"\n{TABLES[d]} (first 5 clean rows):")
                    print(for_display(d, tables[d].head()).to_string(index=False))
    else:
        try:
            cfg = load_db_config(a.env_file)
        except (ValueError, FileNotFoundError) as e:
            sys.exit(str(e))
        engine = cfg.engine()
        print(f"Loading {a.folder} into {cfg.describe()} tables {a.prefix}*")
        create_tables(engine, a.prefix, a.recreate)
        run_id = start_run(engine, "ingest_images.py", str(folder),
                           sha256=folder_sha256(folder, IMAGE_EXTS))
        try:
            tables = load(engine, a.prefix, reads, folder.name, stats, run_id,
                          a.cmms_prefix, **options)
        except BaseException as e:
            finish_run(engine, run_id, ok=False, rows_read=stats["rows_read"] or None,
                       error=f"{type(e).__name__}: {e}")
            print(f"Load failed - nothing changed. Recorded as run {run_id} in ingest_runs.",
                  file=sys.stderr)
            raise
        finish_run(engine, run_id, ok=True, rows_read=stats["rows_read"],
                   rows_loaded=stats["rows_loaded"], rows_rejected=stats["rows_rejected"])
        print(f"Recorded as run {run_id} in ingest_runs.")

    docs = tables["documents"]
    rej = docs[docs["status"] == "rejected"]
    if len(rej):
        print("\nRejected images:")
        print(rej[["source_file", "doc_type", "reject_reason", "duplicate_of_raw_id"]]
              .to_string(index=False))
    print_report(stats, a.cmms_prefix)
    print("\nSeverity: taken from the scale printed on each form, not from its date; forms "
          "that disagree with the date rule are flagged form_scale_contradicts_date_rule.")
    if a.rejects and len(rej):
        Path(a.rejects).parent.mkdir(parents=True, exist_ok=True)
        rej[["source_file", "image_sha256", "doc_type", "reject_reason"]].to_csv(
            a.rejects, index=False)
        print(f"Rejected images written to {a.rejects}")
    print("Dry run - nothing written to the database." if a.dry_run else "Done.")


if __name__ == "__main__":
    main()