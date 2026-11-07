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
                           image, read separately; on incident forms the severity value is
                           also read from the blue value ink alone (severity_from_ink); the
                           class LLM endpoint with --engine llm; --engine both reads
                           with both and compares them field by field (interpret_both)
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
    python scripts/ingest_images.py data/images --engine both     # both, compared field by field
    python scripts/ingest_images.py data/images --dry-run --photos-preview .cache/ingest_images/preview
                                                                  # check the redacted forms
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
TESSERACT_TAG = "tesseract-v3"     # change when preprocessing changes: invalidates the cache
                                   # v2: severity also read from the blue value ink
                                   # v3: signature looked for as blue ink, below the label too
DEFAULT_LLM_MODEL = "gpt-4o-mini"
LLM_TAG = "llm-v3"                 # change when LLM_PROMPT changes: invalidates the cache
                                   # v2: severity value vs the number in its label
                                   # v3: power is the whole line; capacity only if labelled
TABLES = {"nameplate": "nameplates", "incident_report": "incident_reports"}
KEYS = {"nameplate": "asset_tag", "incident_report": "incident_id"}


# ============================================================== reading images ===

@dataclass
class Extraction:
    """What the engine read from one image, before any parsing."""
    engine: str                       # 'tesseract-v3' | 'llm-v3:<model>' | 'both:...'
    texts: dict | None = None         # tesseract: preprocessing variant -> OCR text
    fields: dict | None = None        # llm: field -> value as written (+ doc_type)
    signature: int | None = None      # 1 = ink in the signature box, 0 = none, None = unsure
    error: str | None = None
    ink: dict | None = None           # tesseract: fields read from the blue value ink alone,
                                      # e.g. {"severity": "3"}; one more reading in the vote
    parts: dict | None = None         # both: {"tesseract": Extraction, "llm": Extraction}

    def __post_init__(self):
        if self.parts:
            self.parts = {k: v if isinstance(v, Extraction) else Extraction(**v)
                          for k, v in self.parts.items()}


def both_tag(model: str) -> str:
    return f"both:{TESSERACT_TAG}+{LLM_TAG}:{model}"


def combine(tess: Extraction, llm: Extraction, model: str) -> Extraction:
    """One Extraction holding both engines' reads. An error only if BOTH failed; if one
    failed, the other is used and the image is flagged engine_failed_<engine>."""
    error = (f"tesseract: {tess.error}; llm: {llm.error}" if tess.error and llm.error
             else None)
    return Extraction(both_tag(model), error=error, parts={"tesseract": tess, "llm": llm})


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


SIGNATURE_MIN_INK = 30     # blue pixels in the signature area that count as signed. Form
                           # 900's small signature has 94; blank paper and stains have 0


def blue_ink(region) -> int:
    """Pixels of blue (value) ink: blue well above red, not near white. Grey print, paper
    and the beige stains on the scans don't count."""
    if region is None or region.size == 0:
        return 0
    b, g, r = (region[..., i].astype(int) for i in range(3))
    return int(((b - r > 40) & (b + g + r < 600)).sum())


def signature_ink(bw, img=None) -> int | None:
    """1 if the signature area holds blue ink, 0 if it doesn't, None if the 'Signature'
    label can't be found. Says nothing about WHO signed.

    bw: the 2x preprocessed page, to find the label; img: the colour scan, to look for ink.
    The area runs from the end of the label to the right edge, from 1 line above it to 9.5
    below, because a signature is written mostly BELOW its label (the same finding as the
    redaction box). Until 7 Nov 2026 only a band level with the label was checked, in black
    and white: forms 917, 918, 936 and 955 were read as unsigned, and all four are signed
    (checked by eye). Without `img` (old callers) the old black-and-white check is used."""
    import pytesseract

    data = pytesseract.image_to_data(bw, config="--psm 6", output_type=pytesseract.Output.DICT)
    for i, word in enumerate(data["text"]):
        if word.lower().startswith("signature"):
            x, y = data["left"][i] + data["width"][i], data["top"][i]
            h = max(data["height"][i], 10)
            if img is None:
                region = bw[max(0, y - 3 * h): y + 2 * h, x + 20: x + 20 + 40 * h]
                return int(region.size > 0 and float((region < 128).mean()) > 0.005)
            region = img[max(0, int((y - h) / 2)): int((y + 9.5 * h) / 2), int(x / 2) + 5:]
            return int(blue_ink(region) >= SIGNATURE_MIN_INK)
    return None


# ------------------------------------------------- photos for the app (PDPA) ---
# The app shows the photo behind an answer (agent_ui.py), from the database: the hosted
# page has no data folder. Nameplates are stored as they are. An incident form is stored
# ONLY as a redacted copy, with the 'Person involved' value and the signature box covered;
# the original never goes into the database (the scan folder stays the original record).
# Both areas are found with OCR; if either can't be found, the form is not stored at all.

REDACT_TAG = "redact-v2"     # change when redaction changes: invalidates the cached boxes
                             # v2: signature box runs much further down (917, 918, 955, 957)
REDACT_FILL = (225, 225, 225)


def redaction_boxes(img) -> list[tuple[int, int, int, int]] | None:
    """(x0, y0, x1, y1) boxes, in the image's own pixels, covering the 'Person involved'
    value and the signature box; None if either label can't be found (then the form is
    not stored). Generous on purpose: a signature runs above and below its line, and on a
    slightly rotated scan the value sits higher than its label (IR-2024-900: the top of
    'EMP001' and a signature stroke showed above boxes sized to the label). The signature
    is written mostly BELOW its label: on 917, 918, 955 and 957 its tail showed under a box
    3 line-heights deep, so the box now runs 9.5 down and 4 up (4.6 up cut into the
    root-cause value above)."""
    import cv2
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = find_tesseract()
    up = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), None, fx=2, fy=2,
                    interpolation=cv2.INTER_CUBIC)
    d = pytesseract.image_to_data(up, config="--psm 6", output_type=pytesseract.Output.DICT)
    words = [(w.strip().lower(), d["left"][i], d["top"][i], d["width"][i], d["height"][i])
             for i, w in enumerate(d["text"]) if w.strip()]
    H, W = img.shape[:2]

    def box(x_end, y, h, above, below):
        return (max(0, int(x_end / 2) + 2), max(0, int((y - above * h) / 2)),
                W, min(H, int((y + below * h) / 2)))

    person = signature = None
    for i, (w, x, y, wd, h) in enumerate(words):
        nxt = words[i + 1][0] if i + 1 < len(words) else ""
        if person is None and w.startswith("person") and nxt.startswith("involved"):
            _, x2, y2, wd2, h2 = words[i + 1]
            person = box(x2 + wd2, y2, max(h2, 10), 2.2, 1.8)
        if signature is None and w.startswith("signature"):
            signature = box(x + wd, y, max(h, 10), 4.0, 9.5)
    if person is None or signature is None:
        return None
    return [person, signature]


def redact(img, boxes):
    """A copy with each box filled grey and labelled, so it's plain something was removed."""
    import cv2

    out = img.copy()
    for x0, y0, x1, y1 in boxes:
        cv2.rectangle(out, (x0, y0), (x1, y1), REDACT_FILL, -1)
        cv2.putText(out, "removed (PDPA)", (x0 + 6, min(y1 - 6, y0 + 22)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (90, 90, 90), 1, cv2.LINE_AA)
    return out


def encode_jpeg(img, quality: int = 85) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("could not encode the image")
    return buf.tobytes()


def cached_boxes(path: Path, sha: str, cache_dir: Path | None):
    """redaction_boxes, cached like the OCR reads. Returns (boxes or None, reason)."""
    cached = cache_path(cache_dir, sha, REDACT_TAG) if cache_dir else None
    if cached and cached.is_file():
        try:
            got = json.loads(cached.read_text(encoding="utf-8"))
            return (None if got["boxes"] is None else [tuple(b) for b in got["boxes"]]), \
                got.get("reason")
        except (ValueError, KeyError, TypeError):
            pass
    try:
        boxes = redaction_boxes(read_image(path))
        reason = None if boxes else "labels_not_found"
    except RuntimeError:
        return None, "no_tesseract"               # not cached: install it and re-run
    except Exception as e:  # noqa: BLE001 - the form is just not stored
        return None, f"error: {type(e).__name__}"
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"boxes": boxes, "reason": reason}), encoding="utf-8")
    return boxes, reason


FILE_COLUMNS = ["image_sha256", "variant", "mime", "bytes_sha256", "width", "height",
                "image_bytes"]
_EXT_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp"}


def image_files(folder: Path, docs: pd.DataFrame, stats: Counter,
                cache_dir: Path | None = None, boxes_fn=None,
                preview_dir: Path | None = None) -> tuple[pd.DataFrame, dict]:
    """Rows for {prefix}files: every loaded nameplate as it is, every loaded form redacted.
    Returns (rows, {raw_id: reason}) for forms that were NOT stored. preview_dir: also
    write each redacted form there, to check by eye before loading."""
    import hashlib

    boxes_fn = boxes_fn or cached_boxes
    rows, skipped = [], {}
    loaded = docs[(docs["status"] == "loaded") & docs["doc_type"].isin(DOC_TYPES)]
    for r in loaded.itertuples():
        path = folder / r.source_file
        try:
            if r.doc_type == "nameplate":
                data = path.read_bytes()
                mime = _EXT_MIME.get(path.suffix.lower())
                img = read_image(path)
                if mime is None:                      # .tif / .bmp: browsers can't show them
                    data, mime = encode_jpeg(img), "image/jpeg"
                variant = "original"
            else:
                boxes, reason = boxes_fn(path, r.image_sha256, cache_dir)
                if not boxes:
                    skipped[r.raw_id] = reason or "labels_not_found"
                    stats[f"files:not_stored:{skipped[r.raw_id]}"] += 1
                    continue
                img = redact(read_image(path), boxes)
                data, mime, variant = encode_jpeg(img), "image/jpeg", "redacted"
        except Exception as e:  # noqa: BLE001 - one unreadable file doesn't stop the load
            skipped[r.raw_id] = f"error: {type(e).__name__}"
            stats[f"files:not_stored:{skipped[r.raw_id]}"] += 1
            continue
        if preview_dir is not None and variant == "redacted":
            preview_dir.mkdir(parents=True, exist_ok=True)
            (preview_dir / f"{Path(r.source_file).stem}_redacted.jpg").write_bytes(data)
        rows.append({"image_sha256": r.image_sha256, "variant": variant, "mime": mime,
                     "bytes_sha256": hashlib.sha256(data).hexdigest(),
                     "width": int(img.shape[1]), "height": int(img.shape[0]),
                     "image_bytes": data})
        stats[f"files:stored:{variant}"] += 1
    return pd.DataFrame(rows, columns=FILE_COLUMNS), skipped


def flag_unstored(docs: pd.DataFrame, skipped: dict) -> pd.DataFrame:
    """Add form_image_not_stored to the warnings of forms with no redacted copy."""
    docs = docs.copy()
    for rid, _ in skipped.items():
        i = docs.index[docs["raw_id"] == rid]
        old = docs.loc[i, "warnings"].iloc[0] if len(i) else None
        docs.loc[i, "warnings"] = ",".join(x for x in [old, "form_image_not_stored"] if x)
    return docs


# --------------------------------------------------- severity from the value ink ---
# On the new WSH-F02 form (from July 2025) the label "Severity (5 = MOST SEVERE, new WSH
# matrix):" runs into the value column, and the severity number is printed in blue over its
# "WSH". OCR then reads the line as 'new W8H matrix' and the number is lost (trap list I12).
# Every value on the form is printed in blue; every label is grey/black. Keeping only blue
# pixels leaves the number on its own. One mask doesn't suit every scan (a low threshold
# keeps thin strokes where blue crosses grey, but merges a bold digit with the print), so
# the digit is read through several masks and voted, like the three page versions.

INK_DIGITS = {"1", "2", "3", "4", "5"}
INK_OCR = "--psm 7 -c tessedit_char_whitelist=12345"
# (kind, scale, threshold, blur, closing kernel). 'excess': blue minus red above the
# threshold, which still holds where blue ink overlaps grey print. 'hsv': blue hue with at
# least the threshold's saturation.
INK_MASKS = [("excess", 1, 10, 0, 1), ("excess", 1, 15, 0, 1), ("excess", 1, 20, 0, 1),
             ("excess", 4, 8, 0, 3), ("excess", 4, 12, 5, 3), ("excess", 4, 16, 5, 3),
             ("hsv", 1, 40, 0, 1), ("hsv", 1, 60, 0, 1), ("hsv", 4, 40, 5, 3),
             ("hsv", 4, 60, 5, 3)]


def value_ink_mask(band, kind: str, scale: int, thr: int, blur: int, close: int):
    """Blue value ink -> black on white; grey/black print and paper -> white."""
    import cv2

    up = band if scale == 1 else cv2.resize(band, None, fx=scale, fy=scale,
                                            interpolation=cv2.INTER_CUBIC)
    if blur:
        up = cv2.GaussianBlur(up, (blur, blur), 0)
    if kind == "excess":
        b, g, r = (c.astype(int) for c in cv2.split(up))
        mask = (((b - r) > thr) & ((b + g + r) < 600)).astype(np.uint8) * 255
    else:
        mask = cv2.inRange(cv2.cvtColor(up, cv2.COLOR_BGR2HSV), (95, thr, 30), (145, 255, 255))
    if close > 1:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((close, close), np.uint8))
    if scale == 1:
        mask = cv2.resize(mask, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    return cv2.copyMakeBorder(255 - mask, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)


def severity_band(img):
    """The line of the 'Severity' label, from the label to the right edge; None if the
    label can't be found."""
    import cv2
    import pytesseract

    data = pytesseract.image_to_data(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    for i, word in enumerate(data["text"]):
        if word.lower().startswith("severity"):
            y, h = data["top"][i], max(data["height"][i], 10)
            return img[max(0, y - h): y + 2 * h, data["left"][i]:]
    return None


def ink_majority(readings: dict, need: int = 3) -> str | None:
    """The digit at least `need` masks read, and more than twice as often as any other;
    otherwise None. Abstaining is safe: the value then stays unread and is flagged."""
    counts = Counter(readings.values()).most_common()
    if not counts or counts[0][1] < need or (len(counts) > 1 and counts[1][1] * 2 > counts[0][1]):
        return None
    return counts[0][0]


def severity_from_ink(img) -> str | None:
    """The severity digit read from the blue value ink on its line, or None."""
    import pytesseract

    band = severity_band(img)
    if band is None or band.size == 0:
        return None
    readings = {}
    for i, params in enumerate(INK_MASKS):
        d = pytesseract.image_to_string(value_ink_mask(band, *params), config=INK_OCR).strip()
        if d in INK_DIGITS:
            readings[i] = d
    return ink_majority(readings)


def extract_tesseract(path: Path) -> Extraction:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = find_tesseract()
    img = read_image(path)
    variants = preprocess_variants(img)
    texts = {k: pytesseract.image_to_string(im, config="--psm 6") for k, im in variants.items()}
    if classify("\n".join(texts.values())) != "incident_report":
        return Extraction(TESSERACT_TAG, texts=texts)
    severity = severity_from_ink(img)
    return Extraction(TESSERACT_TAG, texts=texts, signature=signature_ink(variants["otsu"], img),
                      ink={"severity": severity} if severity else None)


LLM_PROMPT = """You are reading one scanned document from a ship-repair workshop.
Return ONLY a JSON object. Copy every value exactly as written: do not convert dates, units
or numbers, do not fix spelling, and use null for anything blank or unreadable. Never guess.

Incident report form:
{"doc_type": "incident_report", "report_no": "", "date": "", "location": "", "person": "",
 "description": "", "severity": "", "severity_label": "", "lost_work_days": "",
 "reporting": "", "immediate_action": "", "root_cause": "", "signature_present": true}
On the form, labels are printed in grey/black and the values filled in are in blue.
- severity_label: the WHOLE text in the brackets after "Severity", including its number,
  e.g. "1 = MOST SEVERE" or "5 = MOST SEVERE, new WSH matrix".
- severity: only the blue number filled in as the answer. It is often printed over the
  label text. It is NOT the number inside the label: "5 = MOST SEVERE" is the scale, not
  the value. If you cannot see a separate blue number, use null.
- signature_present: true only if there is handwriting in the safety officer's signature box.
Machine nameplate:
{"doc_type": "nameplate", "manufacturer": "<large maker name at the top>", "model": "",
 "serial_no": "", "mfg_date": "", "power": "", "capacity": "", "weight": "", "lubricant": "",
 "grease_interval": "", "asset_tag": ""}
- power: EVERYTHING after "POWER" on that line, e.g. "22 kW 3PH 415V 50Hz". Don't split it.
- capacity: only the text after a "CAPACITY" label; null if the plate has no CAPACITY line.
  Never put part of another line here.
Anything else: {"doc_type": "unknown"}"""

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif"}


def llm_client():
    """OpenAI client for the class endpoint. Setup problems raise RuntimeError, which stops
    the run instead of rejecting (or, with --engine both, silently skipping) every image."""
    try:
        from dotenv import load_dotenv
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(f"--engine llm/both needs the openai and python-dotenv packages "
                           f"(pip install openai python-dotenv): {e}") from e
    load_dotenv()
    missing = [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Set {' and '.join(missing)} in .env (the class LLM endpoint, "
                           f"docs/llm-budget-proxy-setup.md); no other AI service is used.")
    return OpenAI()


def extract_llm(path: Path, model: str) -> Extraction:
    """Vision model through the class LLM endpoint (OpenAI-compatible): OPENAI_BASE_URL and
    OPENAI_API_KEY from .env, as in docs/llm-budget-proxy-setup.md. No other AI service."""
    client = llm_client()
    mime = _MIME.get(path.suffix.lower())
    if mime is None:  # .tif / .bmp: send as PNG
        import cv2

        ok, buf = cv2.imencode(".png", read_image(path))
        data, mime = buf.tobytes(), "image/png"
    else:
        data = path.read_bytes()
    import openai

    try:
        resp = client.chat.completions.create(
            model=model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": LLM_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}]}])
    except (openai.AuthenticationError, openai.PermissionDeniedError,
            openai.NotFoundError) as e:
        # Wrong key, no budget left or no such model: every image would fail the same way
        raise RuntimeError(f"LLM endpoint refused the request ({type(e).__name__}): "
                           f"check OPENAI_API_KEY and --llm-model {model}. {e}") from e
    raw = json.loads(resp.choices[0].message.content or "{}")
    sig = raw.pop("signature_present", None)
    fields = {k: None if v is None else str(v) for k, v in raw.items()}
    return Extraction(f"{LLM_TAG}:{model}", fields=fields,
                      signature=None if sig is None else int(bool(sig)))


def cache_path(cache_dir: Path, sha: str, engine: str) -> Path:
    return cache_dir / f"{sha[:16]}_{re.sub(r'[^A-Za-z0-9.-]+', '-', engine)}.json"


def extract(path: Path, sha: str, engine: str, model: str,
            cache_dir: Path | None) -> Extraction:
    """Read one image, from the cache when this exact image was read the same way before.
    Errors are returned (not raised) so one bad image doesn't stop the load.
    engine 'both': each engine reads (and caches) on its own; they are compared in interpret."""
    if engine == "both":
        return combine(extract(path, sha, "tesseract", model, cache_dir),
                       extract(path, sha, "llm", model, cache_dir), model)
    tag = TESSERACT_TAG if engine == "tesseract" else f"{LLM_TAG}:{model}"
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


def norm_serial(value) -> str | None:
    """A plate serial: 2 letters, then digits. norm_id fixes letters read for digits in the
    digits; this also fixes the reverse in the prefix, but only a zero right after the
    first letter, read as the letter O: 'K04778956' -> 'KO4778956'. A zero first could be
    a D or an O (CNC-03's '0M4958733' is DM...), so it isn't guessed.

    Checked by eye, 7 Nov 2026: CRN-01's plate says KO4778956 and LTH-01's CO5612371; this
    font dots its zeros and the serials' second character has no dot. Before this, the
    check rejected the right reading ('K04778956', 2 of 3 passes and the model) and kept
    the only one that passed it, 'KO04778956', which has an extra 0 (trap list I21)."""
    s = norm_id(value, 2)
    if s and len(s) > 2 and s[0].isalpha() and s[1] == "0":
        s = s[0] + "O" + s[2:]
    return s


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
    """Undo OCR's usual misreadings of oil names (trap list I14, I18), before the vote, so
    the three readings of one plate group together:
    'Tonna $3' -> 'Tonna S3', 'IS0'/'1S0'/'[S0'/'SO VG32' -> 'ISO VG32', 'vVG68' -> 'VG68',
    'Tonna S3 N68' -> 'Tonna S3 M68' (Shell Tonna S3 only comes as M), trailing ' ;' dropped.
    A misread digit ('M58') is not guessed: it fails the grade check and is flagged."""
    s = _text(v)
    if not s:
        return None
    s = re.sub(r"\$(?=\d)", "S", s)
    s = re.sub(r"[\s;:,.|'`]+$", "", s)
    s = re.sub(r"(?<![A-Za-z])[vV]+(?=VG\s*\d)", "", s)
    s = re.sub(r"(?<![A-Za-z])[\[(|1Il]?S[0O](?=\s*VG\s*\d)", "ISO", s)
    s = re.sub(r"(Tonna\s+S3\s+)[NH](?=\d)", r"\1M", s, flags=re.I)
    return _text(s)


# The red italic '(JME)' after ASSET TAG, as OCR reads it: '(JME):', 'UME):', '{ME}:',
# 'GME):', '(JME:'. Only stripped when a bracket or colon closes it, so a tag that starts
# with 'ME' is left alone.
_ASSET_LABEL = re.compile(r"^[(\[{]?\s*[A-Z]?ME\s*(?:[)\]}]\s*[:;.]?|[:;.])\s*", re.I)


def asset_tag_value(v) -> str | None:
    """The value after ASSET TAG with the '(JME)' label removed, however OCR garbled it:
    '{ME}: CNC-01' -> 'CNC-01', 'GME): WELD-02' -> 'WELD-02', 'UME): BLAST!' -> 'BLAST!'."""
    s = _text(v)
    return _text(_ASSET_LABEL.sub("", s)) if s else None


# Lines that start with a field label: never the maker's name (trap list I17).
_PLATE_LABEL = re.compile(r"^(MODEL|SERIAL|MFG|POWER|FLA|MAX|CAPACITY|WEIGHT|LUBRICANT|"
                          r"GREASE|ASSET|RATED|DUTY)\b", re.I)


def plate_maker(lines: list[str]) -> str | None:
    """The manufacturer: printed in bold capitals above the MODEL line. An all-capitals
    line that isn't a label; otherwise the first line above MODEL that is mostly capitals,
    with OCR junk around it removed. Was: the first all-capitals line, which took
    'MODEL CXT' on CRN-01 and found nothing on MILL-01 and LTH-01."""
    body = [ln for ln in lines if not _PLATE_LABEL.match(ln)]
    exact = next((ln for ln in body if re.fullmatch(r"[A-Z][A-Z &.\-]{2,}", ln)), None)
    if exact:
        return exact
    model_at = next((i for i, ln in enumerate(lines) if re.match(r"MODEL\b", ln, re.I)), None)
    for ln in lines[:model_at] if model_at is not None else []:
        letters = re.sub(r"[^A-Za-z]", "", ln)
        if len(letters) >= 3 and sum(c.isupper() for c in letters) >= 0.6 * len(letters):
            name = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", ln)
            return _text(re.sub(r"[^A-Za-z &.\-]", "", name)).upper()
    return None


def parse_nameplate_text(text: str) -> dict:
    L = clean_lines(text)
    maker = plate_maker(L)
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

def plausible_mfg_date(v) -> bool:
    """'MM/YYYY' with a real month and a year from 1950 to now; trailing OCR junk
    ('10/2018 a') is ignored. Rejects '62/2016' and '06/2820' (trap list I16)."""
    m = re.match(r"\s*(\d{1,2})\s*/\s*(\d{4})(?!\d)", v or "")
    return bool(m) and 1 <= int(m.group(1)) <= 12 and 1950 <= int(m.group(2)) <= date.today().year


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
        "serial_no": lambda v: bool(re.fullmatch(r"[A-Z]{2}\d{6,}", norm_serial(v) or "")),
        "mfg_date": plausible_mfg_date,
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
            if len(vals) > 1 and len({_same(v) for _, v in vals}) == len(vals):
                # No two readings agree: the value is a tie-break, not a vote (I15)
                notes.append({"field": fld, "issue": "no_majority", "chose": best})
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
        # No date_assumed_day_first here (unlike ingest_cmms.py): every WSH-F02 form writes
        # dd/mm/yyyy, so it would fire on every form. The template is shown day-first by
        # dates like 24/08/2024, and a misread date shows in incident_report_vs_cmms
        # (incident_date, agrees = 0).
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
    df["serial_no"] = df["serial_no"].map(norm_serial)
    for c in ("manufacturer", "model", "lubricant"):
        df[c] = df[c].map(_text)
    df["lubricant"] = df["lubricant"].map(clean_lubricant)
    return df


def month_of(v) -> str | None:
    """'10/2018 a' -> '2018-10'."""
    m = re.search(r"(\d{1,2})\s*/\s*(\d{4})", fix_digits(_text(v) or ""))
    return f"{m.group(2)}-{int(m.group(1)):02d}" if m and 1 <= int(m.group(1)) <= 12 else None


def weight_of(v) -> tuple[float | None, str | None]:
    """'2,000 lb' -> (907.18, 'lb'); '3588 kg ,' -> (3588.0, 'kg'); OCR's 'ka' / 'kq' / 'k9'
    for 'kg' too (CMP-01 read '3588 ka', 6 Nov 2026)."""
    s = re.sub(r"(?<=\d)\s*k[aq9]\b", " kg", _text(v) or "", flags=re.I)
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(kg|lbs?|t)\b", s, re.I)
    if not m:
        return None, None
    n, unit = float(m.group(1).replace(",", "")), m.group(2).lower()
    factor = {"kg": 1.0, "t": 1000.0}.get(unit, 0.45359237)
    return round(n * factor, 2), unit


def hours_of(v) -> int | None:
    """'1000 h' / '1000 hrs' / '1O00 h' -> 1000. Letter-for-digit fixes in the number only:
    applied to the whole text they turned 'hrs' into 'hr5', so '1000 hrs' was unreadable."""
    m = re.search(r"(\d[\dOoIl,]*)\s*(?:h|hrs?|hours?)\b", _text(v) or "", re.I)
    if not m:
        return None
    n = fix_digits(m.group(1)).replace(",", "")
    return int(n) if n.isdigit() else None


def voltages_of(v) -> tuple[int | None, int | None]:
    """'380-440V' -> (380, 440); a single '415V' -> (415, 415)."""
    lo = _num(r"(\d{3})\s*[-–]\s*\d{3}\s*V", v, int)
    hi = _num(r"\d{3}\s*[-–]\s*(\d{3})\s*V", v, int)
    if lo is None:
        lo = hi = _num(r"(?<![\d-])(\d{3})\s*V\b", v, int)
    return lo, hi


# A CAPACITY value that is really the rest of the POWER line ('3PH 415V 50Hz'): the vision
# model split CNC-01's power line this way (6 Nov 2026, trap list I25).
_POWER_LINE = re.compile(r"\b\d\s*PH\b|\b\d{2,3}\s*Hz\b|\b\d{3}\s*V\b", re.I)
_CAPACITY_UNIT = re.compile(r"\b(kg|t|A|cfm|l/min|m3|bar|%|mm)\b", re.I)


def looks_like_power_line(v) -> bool:
    s = _text(v) or ""
    return bool(_POWER_LINE.search(s)) and not _CAPACITY_UNIT.search(s)


def parse_nameplate_values(df: pd.DataFrame) -> pd.DataFrame:
    """Numbers and units. Weight in lb or t is converted to kg (flagged), like the spares
    sheet's inches; the text as printed stays in the raw row."""
    df = df.copy()
    for c in ("mfg_date", "power", "capacity", "weight", "grease_interval"):
        df[f"{c}_given"] = df[c].map(_text).notna()

    df["mfg_month"] = df["mfg_date"].map(month_of)
    df["power_kw"] = df["power"].map(lambda v: _num(r"(\d+(?:\.\d+)?)\s*kW", v))
    df["phases"] = df["power"].map(lambda v: _num(r"(\d)\s*PH", v, int)).astype("Int64")
    volts = df["power"].map(voltages_of)
    df["voltage_min_v"] = volts.map(lambda t: t[0]).astype("Int64")
    df["voltage_max_v"] = volts.map(lambda t: t[1]).astype("Int64")
    df["capacity_raw"] = df["capacity"].map(_text)
    df["rated_current_a"] = df["capacity"].map(lambda v: _num(r"(\d+(?:\.\d+)?)\s*A\b", v))
    df["duty_cycle_pct"] = df["capacity"].map(lambda v: _num(r"@\s*(\d+(?:\.\d+)?)\s*%", v))
    w = df["weight"].map(weight_of)
    df["weight_kg"] = w.map(lambda t: t[0])
    df["weight_unit"] = w.map(lambda t: t[1])
    df["grease_interval_h"] = df["grease_interval"].map(hours_of).astype("Int64")
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
        "capacity_looks_like_power_line": df["capacity"].map(looks_like_power_line),
    }
    return _with_warnings(df, flags)


# ----------------------------------------------------------------- common ---

NOTE_WARNINGS = {"disagreed": "ocr_disagreed", "failed_format": "failed_format",
                 "read_from_value_ink": "read_from_value_ink", "no_majority": "ocr_no_majority",
                 "engines_disagree": "engines_disagree", "one_engine_only": "one_engine_only",
                 "engine_failed": "engine_failed", "not_recognised": "not_recognised",
                 "llm_read_scale_digit": "llm_read_scale_digit",
                 "checked_by_eye": "checked_by_eye"}


def _with_warnings(df: pd.DataFrame, flags: dict) -> pd.DataFrame:
    """Comma-separated warning names; OCR notes become ocr_disagreed_<field> /
    failed_format_<field> / read_from_value_ink_<field> ... (not notes marked warn=False)."""
    f = pd.DataFrame({k: _bool(pd.Series(v, index=df.index)) for k, v in flags.items()},
                     index=df.index)
    ocr = df["ocr_notes"].map(lambda notes: [
        f"{NOTE_WARNINGS.get(n['issue'], n['issue'])}_{n['field']}" for n in (notes or [])
        if n.get("warn", True)])
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
    files = f"{prefix}files"
    out += [f"""
    CREATE TABLE IF NOT EXISTS {files} (
        image_sha256  CHAR(64)    NOT NULL COMMENT 'the scan, byte for byte (= {raw}.image_sha256)',
        variant       VARCHAR(10) NOT NULL COMMENT 'original (nameplates) | redacted (forms)',
        mime          VARCHAR(20) NOT NULL,
        bytes_sha256  CHAR(64)    NOT NULL COMMENT 'of image_bytes: the app checks it before showing',
        width         INT         NOT NULL,
        height        INT         NOT NULL,
        image_bytes   MEDIUMBLOB  NOT NULL,
        PRIMARY KEY (image_sha256, variant)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      COMMENT 'photos the app may show; incident forms only redacted (PDPA)'
    """, f"""
    CREATE OR REPLACE VIEW {files}_public AS
    SELECT f.image_sha256, f.variant, f.mime, f.bytes_sha256, f.width, f.height,
           f.image_bytes, r.source_file, r.doc_type
    FROM {files} f
    JOIN (SELECT image_sha256, MIN(source_file) AS source_file, MIN(doc_type) AS doc_type
          FROM {raw} WHERE status = 'loaded' GROUP BY image_sha256) r
      ON r.image_sha256 = f.image_sha256
    WHERE (r.doc_type = 'nameplate' AND f.variant = 'original')
       OR (r.doc_type = 'incident_report' AND f.variant = 'redacted')
    """]
    return out


# ==================================================================== process ===

@dataclass
class ImageRead:
    source_file: str
    image_sha256: str
    extraction: Extraction


def normalise_llm_fields(fields: dict, doc_type: str) -> dict:
    """The same clean-ups the Tesseract parser applies while parsing, so both engines'
    values are compared like for like ('IS0 VG32', '(JME): CNC-01')."""
    f = dict(fields)
    if doc_type == "nameplate":
        if "lubricant" in f:
            f["lubricant"] = clean_lubricant(f["lubricant"])
        if "asset_tag" in f:
            f["asset_tag"] = asset_tag_value(f["asset_tag"])
    return f


def _engine_key(field: str, v) -> str:
    """What has to match for the two engines to agree. The severity label only has to give
    the same scale: 'new W8H matrix' vs 'new WSH matrix' is not a disagreement."""
    if field == "severity_label" and scale_from_label(v):
        return scale_from_label(v)
    cleaned = CLEANED_KEY.get(field, lambda _: None)(v)
    return f"={cleaned}" if cleaned is not None else _same(v)


# Fields compared on the value that is stored, not the text as read: '10/2018 a' and
# '10/2018' are the same month, '3588 ka' and '3588 kg' the same weight (6 Nov 2026).
CLEANED_KEY = {"mfg_date": month_of, "weight": lambda v: weight_of(v)[0],
               "grease_interval": hours_of}


def power_parts(v) -> dict:
    """What is stored from a POWER line: kW, phases, voltage."""
    lo, hi = voltages_of(v)
    return {"kw": _num(r"(\d+(?:\.\d+)?)\s*kW", v), "phases": _num(r"(\d)\s*PH", v, int),
            "volts": (lo, hi) if lo is not None else None}


def powers_agree(a, b) -> bool:
    """Two readings of a POWER line give the same stored values. kW and phases must match
    (a part one reading lacks is a disagreement: CNC-01's '22 kW' alone vs the whole line).
    A voltage one reading couldn't parse ('415y', OCR for '415V') isn't a disagreement if
    that reading still has a 3-digit number where the voltage goes. The frequency ('SOHz'
    vs '50Hz') isn't stored, so it doesn't count. (CNC-01, PMP-T01, 6 Nov 2026)"""
    pa, pb = power_parts(a), power_parts(b)
    if pa["kw"] is None or pa["kw"] != pb["kw"] or pa["phases"] != pb["phases"]:
        return False
    if pa["volts"] is not None and pb["volts"] is not None:
        return pa["volts"] == pb["volts"]
    garbled = re.compile(r"(?<!\d)\d{3}\s*[A-Za-z]")
    return all(p["volts"] is not None or garbled.search(_text(t) or "")
               for p, t in ((pa, a), (pb, b)))


def engines_agree(field: str, a, b) -> bool:
    if field == "power":
        return powers_agree(a, b)
    return _engine_key(field, a) == _engine_key(field, b)


def signature_of(ex: Extraction) -> int | None:
    """With both engines: their answer if they agree or only one has one; None (unsure,
    flagged signature_unknown) if one sees ink and the other doesn't."""
    if not ex.parts:
        return ex.signature
    sigs = {p.signature for p in ex.parts.values() if not p.error and p.signature is not None}
    return sigs.pop() if len(sigs) == 1 else None


def tess_support(tess: Extraction, doc_type: str, fld: str, value) -> int:
    """How many of Tesseract's readings (page passes + blue ink) gave this value."""
    reads = [PARSERS[doc_type](t).get(fld) for t in (tess.texts or {}).values()]
    reads.append((tess.ink or {}).get(fld))
    return sum(1 for r in reads if r is not None and _same(r) == _same(value))


def scale_digit(*labels) -> str | None:
    """The number the form's severity label prints as its scale: '5' for '5 = MOST SEVERE',
    '1' for '1 = MOST SEVERE'; None if no label gives a scale."""
    for label in labels:
        scale = scale_from_label(label)
        if scale:
            return scale[0]
    return None


def choose(fld: str, tv: str, lv: str, *, ok, tess: Extraction, doc_type: str) -> tuple:
    """Two different readings of one field -> (value kept, rule that decided). The order is
    decision tree 2 in docs/ingest_images.md."""
    if ok and ok(tv) != ok(lv):
        return (tv, "only_tesseract_passes_check") if ok(tv) else (lv, "only_llm_passes_check")
    ink = (tess.ink or {}).get(fld)
    if ink is not None and _same(ink) == _same(tv):
        # The blue-ink digit is itself a vote of 10 masks (>= 3 agreeing, twice any other):
        # on the 42 scans it was never wrong, the model was wrong on 4 of the same 6 forms.
        return tv, "tesseract_from_blue_ink"
    if ok is None:
        t, l = _same(tv), _same(lv)
        if len(l) < len(t) and t.startswith(l):
            # The model's reading is the start of Tesseract's: it cut the line short and
            # put the rest elsewhere (CNC-01: power '22 kW', capacity '3PH 415V 50Hz';
            # Tesseract '22 kW 3PH 415y SOHz'). The longer reading keeps what's on the line.
            return tv, "llm_reading_cut_short"
        # Free text (description, power, capacity, ...): OCR typos ('toalbox', '56Hz'), and
        # no check to tell them apart. On the 42 scans the model was right every time.
        return lv, "free_text_llm"
    if tess_support(tess, doc_type, fld, tv) >= 2:
        return tv, "tesseract_majority"
    return lv, "llm_over_tesseract_minority"


def interpret_both(ex: Extraction) -> tuple[str, dict, list, str | None]:
    """Tesseract (three passes + blue ink, voted) and the vision model, compared per field.
    Agree: kept, and Tesseract's own pass-to-pass doubts on that field are dropped (a second,
    independent engine confirmed it). Disagree: `choose` (decision tree 2 in
    docs/ingest_images.md), flagged engines_disagree_<field> with both readings and the rule
    that decided in ocr_notes. A field only one engine read is kept and flagged
    one_engine_only_<field>. A model severity that is just the number printed in the label
    ('5' from '5 = MOST SEVERE') is never used on its own: flagged llm_read_scale_digit."""
    parts = ex.parts or {}
    tess, llm = parts.get("tesseract"), parts.get("llm")
    reads = {}
    for name, part in ((n, p) for n, p in (("tesseract", tess), ("llm", llm)) if p):
        if not part.error:
            reads[name] = interpret(part)
    failed = [{"field": n, "issue": "engine_failed", "error": p.error}
              for n, p in parts.items() if p.error]
    texts = [t for _, _, _, t in reads.values() if t]
    if llm and not llm.error and llm.fields:
        texts.append(f"=== {llm.engine} ===\n" + json.dumps(llm.fields, ensure_ascii=False))
    text = "\n\n".join(texts) or None

    types = {n: r[0] for n, r in reads.items() if r[0] in PARSERS}
    if not types:
        return next((r[0] for r in reads.values()), "unknown"), {}, failed, text
    if len(set(types.values())) > 1:      # one says form, the other nameplate: trust neither
        note = {"field": "doc_type", "issue": "engines_disagree", "readings": types}
        return "unknown", {}, [*failed, note], text
    doc_type = next(iter(types.values()))
    if len(types) == 1:                   # only one engine recognised the document
        name = next(iter(types))
        _, fields, notes, _ = reads[name]
        other = "llm" if name == "tesseract" else "tesseract"
        if other in reads:
            notes = [*notes, {"field": "by_" + other, "issue": "not_recognised",
                              "readings": {other: reads[other][0], name: doc_type}}]
        return doc_type, fields, [*failed, *notes], text

    t_fields, t_notes = reads["tesseract"][1], reads["tesseract"][2]
    l_fields = dict(reads["llm"][1])
    checks = VALIDATORS.get(doc_type, {})
    fields, notes, agreed = {}, list(failed), set()
    set_aside = set()        # fields whose model reading was dropped: already flagged
    if doc_type == "incident_report":
        digit = scale_digit(t_fields.get("severity_label"), l_fields.get("severity_label"))
        tv, lv = _text(t_fields.get("severity")), _text(l_fields.get("severity"))
        if digit and lv == digit and (tv is None or _same(tv) != _same(lv)):
            notes.append({"field": "severity", "issue": "llm_read_scale_digit",
                          "readings": {"tesseract": tv, "llm": lv}})
            l_fields["severity"] = None             # the model's reading isn't used
            set_aside.add("severity")
    for fld in dict.fromkeys([*t_fields, *l_fields]):
        tv, lv = _text(t_fields.get(fld)), _text(l_fields.get(fld))
        if tv is None or lv is None:
            fields[fld] = tv if lv is None else lv
            if fields[fld] is not None and fld not in set_aside:
                notes.append({"field": fld, "issue": "one_engine_only",
                              "chose": fields[fld], "engine": "llm" if tv is None else "tesseract"})
        elif engines_agree(fld, tv, lv):
            ok = checks.get(fld)
            # Same stored value; keep the cleaner text, the model's, unless only Tesseract's
            # passes its check ('10/2018' over '10/2018 a', '3588 kg' over '3588 ka')
            fields[fld] = tv if ok and ok(tv) and not ok(lv) else lv
            agreed.add(fld)
        else:
            fields[fld], rule = choose(fld, tv, lv, ok=checks.get(fld), tess=tess,
                                       doc_type=doc_type)
            notes.append({"field": fld, "issue": "engines_disagree", "chose": fields[fld],
                          "rule": rule, "readings": {"tesseract": tv, "llm": lv}})
    disputed = {n["field"] for n in notes if n["issue"] == "engines_disagree"}
    for n in t_notes:
        fld = n["field"]
        if n["issue"] == "failed_format":
            continue                                   # re-checked below on the final value
        if n["issue"] in ("disagreed", "no_majority") and fld in agreed:
            continue                                   # confirmed by the other engine
        if n["issue"] == "read_from_value_ink" and fields.get(fld) != n.get("chose"):
            continue
        if n["issue"] in ("disagreed", "no_majority") and fld in disputed:
            # Kept in ocr_notes as context, but engines_disagree_<field> already says
            # "check the photo": one warning per field, not three.
            n = {**n, "warn": False}
        notes.append(n)
    for fld, v in fields.items():
        ok = checks.get(fld)
        if ok and v is not None and not ok(v):
            notes.append({"field": fld, "issue": "failed_format", "chose": v})
    sig = {n: p.signature for n, p in parts.items() if not p.error and p.signature is not None}
    if len(set(sig.values())) > 1:
        notes.append({"field": "signature", "issue": "engines_disagree", "chose": None,
                      "readings": sig})
    return doc_type, fields, notes, text


# Values a person read off the scan where OCR couldn't, applied after the engines' vote.
# The stored value comes from here; fields_raw keeps what OCR read, and the note in
# ocr_notes says who/when, so a reviewed value is never mistaken for an OCR reading.
# (doc_type, report no or asset tag, field) -> (value as written, checked, how)
REVIEWED = {
    # I12: the new form prints the severity over its own label; both engines fail. Read
    # from the scans enlarged, 7 Nov 2026; all three agree with the CMMS (2, 4, 1).
    ("incident_report", "IR-2025-911", "severity"): ("2", "7 Nov 2026", "enlarged scan"),
    ("incident_report", "IR-2026-922", "severity"): ("4", "7 Nov 2026", "enlarged scan"),
    ("incident_report", "IR-2026-955", "severity"): ("1", "7 Nov 2026", "enlarged scan"),
}
_REVIEW_KEY = {"incident_report": ("report_no", lambda v: norm_id(v, 2)),
               "nameplate": ("asset_tag", lambda v: machine_tag(re.match(
                   r"\s*([A-Za-z]{2,6})\s*-?\s*([A-Za-z]??)([0-9OoIlSs]{1,3})\b", _text(v) or "")))}


def apply_reviewed(doc_type: str, fields: dict, notes: list,
                   reviewed: dict | None = None) -> tuple[dict, list]:
    """Put in the values a person read off the scan (REVIEWED), where they differ from what
    OCR gave. Each one replaces the OCR value, drops that field's failed_format note (the
    value is no longer OCR's) and adds a checked_by_eye note: flagged
    checked_by_eye_<field>."""
    reviewed = REVIEWED if reviewed is None else reviewed
    if doc_type not in _REVIEW_KEY or not fields:
        return fields, notes
    key_field, norm = _REVIEW_KEY[doc_type]
    key = norm(fields.get(key_field))
    todo = {f: v for (d, k, f), v in reviewed.items() if d == doc_type and k == key}
    if not todo:
        return fields, notes
    fields, notes = dict(fields), list(notes)
    for fld, (value, when, how) in todo.items():
        old = _text(fields.get(fld))
        if old is not None and _same(old) == _same(value):
            continue                                       # OCR already reads it
        fields[fld] = value
        notes = [n for n in notes if not (n["field"] == fld and n["issue"] == "failed_format")]
        notes.append({"field": fld, "issue": "checked_by_eye", "chose": value, "was": old,
                      "checked": when, "how": how})
    return fields, notes


def interpret(ex: Extraction) -> tuple[str, dict, list, str | None]:
    """(doc_type, fields as read, OCR notes, OCR text) from what the engine returned."""
    if ex.error:
        return "unknown", {}, [], None
    if ex.parts:
        return interpret_both(ex)
    if ex.texts is not None:
        joined = "\n\n".join(f"=== {k} ===\n{t}" for k, t in ex.texts.items())
        doc_type = classify(joined)
        if doc_type not in PARSERS:
            return doc_type, {}, [], joined
        candidates = {k: PARSERS[doc_type](t) for k, t in ex.texts.items()}
        if ex.ink and doc_type == "incident_report":
            candidates["blue_ink"] = dict(ex.ink)        # last: loses ties to the page reads
        fields, notes = vote(candidates, doc_type)
        for fld, value in (ex.ink or {}).items():
            page = {_same(c.get(fld)) for n, c in candidates.items() if n != "blue_ink"}
            if fields.get(fld) == value and _same(value) not in page:
                notes.append({"field": fld, "issue": "read_from_value_ink", "chose": value})
        return doc_type, fields, notes, joined
    fields = dict(ex.fields or {})
    raw_type = (fields.pop("doc_type", None) or "").lower().replace(" ", "_")
    doc_type = ("incident_report" if raw_type.startswith("incident") else
                "nameplate" if raw_type.startswith("nameplate") else "unknown")
    if doc_type in PARSERS:
        fields, notes = vote({"llm": normalise_llm_fields(fields, doc_type)},
                             doc_type)                     # format checks only
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
        as_read = fields                                  # fields_raw keeps the OCR values
        fields, notes = apply_reviewed(doc_type, fields, notes)
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
                     "fields": fields, "notes": notes, "signature": signature_of(r.extraction),
                     "fields_raw": json.dumps(as_read, ensure_ascii=False) if as_read else None,
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
            conn.execute(text(f"DROP VIEW IF EXISTS {prefix}files_public"))
            conn.execute(text(f"DROP TABLE IF EXISTS {prefix}files"))
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
         run_id: int, cmms_prefix: str = "cmms_", files_fn=None, **options) -> dict:
    """Replace this folder's rows in ONE transaction; other folders are kept.
    files_fn(docs) -> (rows for {prefix}files, {raw_id: reason not stored}), or None to
    store no photos."""
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
        files = None
        if files_fn is not None:
            files, skipped = files_fn(docs)
            docs = tables["documents"] = flag_unstored(docs, skipped)
        if len(docs):
            conn.execute(_insert(raw, RAW_COLUMNS), to_records(docs))
        for d in DOC_TYPES:
            if len(tables[d]):
                conn.execute(_insert(prefix + TABLES[d], CLEAN_COLUMNS[d]), to_records(tables[d]))
        if files is not None:
            store = f"{prefix}files"
            shas = sorted(set(files["image_sha256"]))
            for sha in shas:
                conn.execute(text(f"DELETE FROM {store} WHERE image_sha256 = :s"), {"s": sha})
            if len(files):
                conn.execute(_insert(store, FILE_COLUMNS), to_records(files))
            # photos of scans no longer loaded from any folder
            conn.execute(text(f"DELETE FROM {store} WHERE image_sha256 NOT IN "
                              f"(SELECT image_sha256 FROM {raw} WHERE status = 'loaded')"))
        print(f"  {raw}: {len(docs)} rows; " + ", ".join(
            f"{prefix}{TABLES[d]}: {len(tables[d])}" for d in DOC_TYPES)
            + (f"; {prefix}files: {len(files)}" if files is not None else ""))
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
    if stored := _items(stats, "files:stored:"):
        print(f"Photos for the app: {stored}")
    if not_stored := _items(stats, "files:not_stored:"):
        print(f"Forms with no photo (redaction not possible): {not_stored}")
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
    ap.add_argument("--engine", choices=["tesseract", "llm", "both"],
                    default=os.getenv("IMAGE_ENGINE", "tesseract"),
                    help="tesseract (offline), llm (the vision model behind OPENAI_BASE_URL, "
                         "the class endpoint), or both: each image read by both and compared "
                         "field by field (default %(default)s, or IMAGE_ENGINE)")
    ap.add_argument("--llm-model", default=os.getenv("IMAGE_LLM_MODEL", DEFAULT_LLM_MODEL),
                    help="model for --engine llm / both (default %(default)s)")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE),
                    help="OCR cache, keyed by image SHA-256 (default %(default)s)")
    ap.add_argument("--no-cache", action="store_true", help="always re-read every image")
    ap.add_argument("--photos-preview", metavar="DIR",
                    help="also write every redacted form here, to check by eye; inside "
                         ".cache/ingest_images/ it is git-ignored")
    ap.add_argument("--no-photos", action="store_true",
                    help="don't store the photos for the app (nameplates as they are, "
                         "incident forms redacted)")
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
    for t in [*TABLES.values(), "documents_raw", "files"]:
        safe_identifier(f"{a.prefix}{t}_public")
    safe_identifier(f"{a.cmms_prefix}machines")
    files = folder_files(folder, IMAGE_EXTS)
    if not files:
        sys.exit(f"No images ({', '.join(IMAGE_EXTS)}) in {a.folder}")
    options = {"cutover": a.severity_cutover, "ambiguous_until": a.severity_ambiguous_until}
    cache = None if a.no_cache else Path(a.cache_dir)

    print(f"Reading {len(files)} image(s) in {a.folder} with {a.engine}"
          + (f" ({a.llm_model})" if a.engine != "tesseract" else ""))
    reads = []
    try:
        for i, p in enumerate(files, 1):
            sha = file_sha256(str(p))
            ex = extract(p, sha, a.engine, a.llm_model, cache)
            reads.append(ImageRead(p.name, sha, ex))
            failed = [f"{n} failed: {x.error}" for n, x in (ex.parts or {}).items()
                      if x.error and not ex.error]
            print(f"  [{i}/{len(files)}] {p.name}: "
                  + (f"ERROR {ex.error}" if ex.error else interpret(ex)[0])
                  + (f" ({'; '.join(failed)})" if failed else ""))
    except RuntimeError as e:
        sys.exit(str(e))
    stats: Counter = Counter()

    preview = Path(a.photos_preview) if a.photos_preview else None

    def files_fn(docs):
        return image_files(folder, docs, stats, cache, preview_dir=preview)

    if a.dry_run:
        tables = process(reads, stats, source_folder=folder.name, **options)
        if not a.no_photos:          # prepared and counted, not stored
            _, skipped = files_fn(tables["documents"])
            tables["documents"] = flag_unstored(tables["documents"], skipped)
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
                          a.cmms_prefix, files_fn=None if a.no_photos else files_fn,
                          **options)
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