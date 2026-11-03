# ingest_images.py: scanned nameplates and incident forms

`scripts/ingest_images.py` reads every image in a folder (by default `data/images/`), works
out whether it is a **machine nameplate** or a **WSH-F02 incident report form**, reads its
fields, cleans them, and loads them with lineage, like the other sources.

The scans add what the CMMS doesn't have:

| Scan | Joins to | Adds |
|---|---|---|
| Nameplate photo | `cmms_machines` by asset tag (`BLAST-01` = `machine_id`) | serial no, manufacture month, power, phases, voltage, rated current and duty cycle, weight, **lubricant**, **grease interval** (Ask 1: "what oil", "what grease interval") |
| Incident report form | `cmms_safety_incidents` by report number (`IR-2024-900` -> `incident_id` 900) | immediate action, root cause (or a note that it's in a 5-Why attachment), whether the safety officer signed, and **the severity scale printed on the form** |

```bash
python scripts/ingest_images.py data/images --dry-run      # read and check; no database needed
python scripts/ingest_images.py data/images --recreate     # first load
python scripts/ingest_images.py data/images                # later loads
python scripts/ingest_images.py data/images --engine llm   # vision model via the class endpoint
```

With uv (personal repo): `uv run python scripts/ingest_images.py data/images --dry-run`.
`pipeline.py` runs it as step 4, after the CMMS load (so it can check machine ids) and before
`relationships.py`.

## Setup: Tesseract

The default OCR engine is [Tesseract](https://github.com/tesseract-ocr/tesseract), a free
program that runs offline. Install it once, then the Python packages
(`pytesseract`, `opencv-python-headless`; add them to `requirements.txt`):

| System | Install |
|---|---|
| Windows | installer from [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki); keep the default folder, the script finds it there |
| WSL / Ubuntu | `sudo apt install tesseract-ocr` (a Windows install can't be used from WSL) |
| macOS | `brew install tesseract` |

Elsewhere, set `TESSERACT_CMD` to the full path of the program. If Tesseract is missing,
the script stops with these instructions instead of rejecting every image.

**`--engine llm`** sends each image to the vision model behind the class LLM endpoint
(`OPENAI_BASE_URL` and `OPENAI_API_KEY` in `.env`, see
[`llm-budget-proxy-setup.md`](llm-budget-proxy-setup.md)); the model is `--llm-model`
(default `gpt-4o-mini`, or `IMAGE_LLM_MODEL`). No other AI service is used, as the brief
requires. The model is asked to copy values exactly as written, and its answers go through
the same parsing and checks as Tesseract's. It costs budget per image and sends the scans,
employee codes included, through the proxy: say so if asked about PDPA.

## How an image is read

| Step | What it does |
|---|---|
| `folder_files` | Every `.jpg .jpeg .png .tif .tiff .bmp .webp` directly in the folder, by name |
| `extract` | Tesseract reads **three versions** of the image: plain grey, Otsu threshold, adaptive threshold. No single version reads every field right: on the BLAST-01 plate Otsu reads `1000 h` as `1660 h` and the tag as `BLAST!`, plain grey reads `M68` as `M66`. Results are cached by the image's SHA-256 in `.cache/ingest_images/` (git-ignored), so unchanged images aren't read again; `--no-cache` turns it off |
| `classify` | `incident_report` (title, `WSH-F02`, `Report No`), `nameplate` (`SERIAL`, `ASSET TAG`, `MFG DATE`), or `unknown` |
| `parse_*_text` | Label -> value, tolerant of OCR splitting a word (`Loc ation/machine`) |
| `vote` | Per field, the reading most versions agree on, among those that pass the field's format check (`IR-yyyy-nnn`, `EMP001`, a tag like `BLAST-01`, an ISO VG oil grade, `1000 h`). Ties go to plain grey. Every disagreement is kept in `ocr_notes` and flagged `ocr_disagreed_<field>`; a value that fails its check is kept and flagged `failed_format_<field>` |
| `clean_*` | Ids (`IR-2024-9OO` -> `IR-2024-900`, `EMPOO1` -> `EMP001`; letters->digits only where digits belong, so `BLAST` stays `BLAST`), numbers and units (weight in lb or t -> kg, flagged), dates (same day-first parser as `ingest_cmms.py`), severity (below) |
| `split_rejects` | Image rejected: `unreadable_image`, `unknown_document_type`, `duplicate_image` (same bytes as another image), `missing_key` (no readable tag or report no), `duplicate_key` / `duplicate_key_conflict` (a second image for the same machine or incident; conflict = its values differ, so review it) |
| `add_*_warnings` | Loaded, but flagged (table below) |
| `load` | One transaction; a failed load changes nothing |

## Severity: the scale printed on the form

The CMMS severity scale flipped in July 2025 and old records were never converted
([trap list C1-C3](trap_list.md#cmms-dump-jurongmarine_cmmssql)). `ingest_cmms.py` converts **by date**. The forms print
their scale next to the field: `Severity (1 = MOST SEVERE): 4`. So for every incident with a
scanned form, the form says which scale that number is on:

| Column | Meaning |
|---|---|
| `severity_as_logged` | the number written on the form |
| `severity_scale` | `1_worst` / `5_worst` from the **printed label**; `unknown` if unreadable (never guessed from the date) |
| `severity_5_worst` | converted with the printed scale. **Query this one** |

Warnings that matter for the audit:

| Warning | Meaning |
|---|---|
| `form_scale_contradicts_date_rule` | The form's printed scale is not the one its date implies. Evidence for C3 (some old records already on the new scale?). The form's value is kept; the CMMS value is not changed |
| `form_scale_in_cutover_month` | A July 2025 form: the date rule leaves it unconverted (`ambiguous`); the form settles it. If incident 907's form is among the scans, this answers client question Q1 for it |
| `severity_scale_not_on_form` | The label couldn't be read: `severity_5_worst` is NULL |

`incident_report_vs_cmms` (made by `relationships.py`) compares the form's
`severity_5_worst` with the CMMS's, field by field.

## Tables

| Table / view | What it holds |
|---|---|
| `img_documents_raw` | One row per image: `source_folder`, `source_file`, `image_sha256`, `doc_type`, `ocr_engine`, `fields_raw` (JSON, each field as read), `ocr_notes` (JSON, disagreements), `ocr_text` (everything Tesseract read, all three versions), `status`, `reject_reason`, `duplicate_of_raw_id`, `warnings` |
| `img_nameplates` | One per machine, key `asset_tag`: manufacturer, model, serial_no, mfg_month, power_kw, phases, voltage_min_v / max, capacity_raw, rated_current_a, duty_cycle_pct, weight_kg, lubricant, grease_interval_h |
| `img_incident_reports` | One per incident, key `incident_id`: report_no, incident_date, machine_id, location_raw, emp_code, description, severity (3 columns above), lost_days, reporting, immediate_action, root_cause, root_cause_attached, signature_present |
| `img_nameplates_audit`, `img_incident_reports_audit` (views) | Clean + image file + its SHA-256 + run. **Cite these**: "nameplate photo `nameplate_BLAST-01.jpg`" |
| `machine_nameplate` (view, `relationships.py`) | Every CMMS machine with its plate's values; `has_nameplate = 0` if there's no photo, so the app can say so instead of guessing; `model_agrees`, `made_by_install_year` |
| `incident_report_vs_cmms` (view, `relationships.py`) | One row per form and field: `form_value`, `logged_value`, `agrees` (1 / 0 / NULL when either side is missing or the CMMS scale is `ambiguous`) |

Re-loading a folder replaces everything loaded from that folder: it is the complete set of
scans, like the spares sheet. `ingest_runs` records the folder name and one SHA-256 over all
its images (`lineage.folder_sha256`: name + hash of each file), so adding, removing or
changing any scan changes the hash. `pipeline.py` computes the same hash for its "Same file
as loaded" column.

## Warnings

| Warning | On | Meaning |
|---|---|---|
| `ocr_disagreed_<field>` | both | The three readings differed; see `ocr_notes` for each one and which was kept |
| `failed_format_<field>` | both | No reading passed the field's format check; the value is kept as read |
| `unknown_machine` | both | Tag / machine not in `cmms_machines` (only checked when the CMMS is loaded; `relationships.py` checks again) |
| `placeholder_manufacturer` | plate | `CUSTOM`, `GENERIC`, ... : the real maker is unknown |
| `duty_cycle_rating_on_non_welder` | plate | A rating like `450 A @ 60%` (how welding power sources are rated) on a machine that isn't a `WELD-*` |
| `lubricant_without_grade`, `missing_lubricant`, `missing_grease_interval`, `bad_*` | plate | Ask 1 fields missing or unreadable |
| `weight_converted_to_kg` | plate | Weight was printed in lb or t |
| `bad_report_no`, `report_year_differs_from_date` | form | Report number unreadable, or its year isn't the incident's year |
| `date_assumed_day_first`, `missing_date`, `bad_date` | form | As in `ingest_cmms.py` |
| `person_not_an_emp_code` | form | "Person involved" isn't an `EMP` code (e.g. a name): **not stored** in the clean table |
| `root_cause_in_attachment` | form | Root cause says "see attached": the 5-Why sheet isn't in the scan or the data package |
| `signature_missing`, `signature_unknown` | form | No ink in the signature box / box not found |
| severity warnings | form | See above |

## Personal data (PDPA)

- The forms carry an employee code and a handwritten signature. Only the code is stored;
  the signature becomes `signature_present` (1/0). Who signed is not read.
- A "person involved" that isn't an `EMP` code is left out of `img_incident_reports`. It is
  still in `img_documents_raw` (`fields_raw`, `ocr_text`) as read, which the app user can't
  read (`agent_user.py` grants only the `*_audit` views and the linking views, never `*_raw`).
- The audit views give the file name and hash for citations, not the OCR text.
- The OCR cache (`.cache/ingest_images/`) holds the same text as the raw table; it is
  git-ignored. Delete it with the rest of the data after the project.
- `--engine llm` sends the images through the class proxy (Singapore VM) to the model.

## Run summary

```
Rows read: 2   loaded: 2   rejected: 0
Document types: incident_report=1, nameplate=1 (total 2)
OCR engine: tesseract-v1=2 (total 2)

nameplates: loaded 1
  Loaded with warnings: duty_cycle_rating_on_non_welder=1, ocr_disagreed_asset_tag=1, ocr_disagreed_grease_interval=1, ocr_disagreed_lubricant=1, ocr_disagreed_power=1, placeholder_manufacturer=1 (total 6)

incident_reports: loaded 1
  Loaded with warnings: date_assumed_day_first=1, root_cause_in_attachment=1 (total 2)
  Severity scale on forms: 1_worst=1 (total 1)

Machine ids: checked against cmms_machines (12 machines)
```

One row is one image. `Rows read` = loaded + rejected.

## Audit queries

```sql
-- What does BLAST-01's nameplate say, and where is the photo?
SELECT asset_tag, lubricant, grease_interval_h, power_kw, source_file, image_sha256
FROM img_nameplates_audit WHERE asset_tag = 'BLAST-01';

-- Machines with no nameplate photo (the app must say "no nameplate on file")
SELECT machine_id, machine_name FROM machine_nameplate WHERE has_nameplate = 0;

-- Where scanned forms and the CMMS disagree
SELECT incident_id, field, form_value, logged_value FROM incident_report_vs_cmms
WHERE agrees = 0 ORDER BY incident_id, field;

-- Forms whose printed scale contradicts the date rule, or settle July 2025
SELECT r.source_file, r.incident_id, r.incident_date, r.severity_as_logged, r.severity_scale,
       r.severity_5_worst, d.warnings
FROM img_incident_reports_audit r JOIN img_documents_raw d ON d.raw_id = r.raw_id
WHERE d.warnings LIKE '%form_scale%';

-- Every reading of every field OCR was unsure about (as the ingest user)
SELECT source_file, warnings, ocr_notes FROM img_documents_raw WHERE ocr_notes IS NOT NULL;

-- Rejected images
SELECT source_file, doc_type, reject_reason, duplicate_of_raw_id FROM img_documents_raw
WHERE status = 'rejected';
```

## Tests

`tests/test_images.py` (35 tests, no database, Tesseract or LLM needed): the OCR text the
two sample scans actually produced, with Tesseract's mistakes, is passed in directly.
Covers classifying, label parsing (`Loc ation`), the printed-scale label in every form
(`1 = MOST SEVERE`, `5=WORST`, `1 = least severe`, unreadable), voting (majority, format
checks, ties, spacing-only differences), id fixes that keep `BLAST` as `BLAST`, both
document types end to end, a form contradicting the date rule, a July 2025 form settling
the ambiguity, an unreadable scale never guessed, a name instead of an `EMP` code never
stored, welding ratings, lb -> kg, every reject reason, duplicates against earlier folders,
the LLM engine's fields, the cache, the folder hash, the audit views leaving OCR text out,
and a dry run the pipeline can parse. `test_the_real_scans_read_correctly` OCRs the two
real scans when Tesseract and `data/images/` are present, and is skipped otherwise.

**Not covered:** the MySQL DDL, the load and the two views in `relationships.py` against a
real server. Check with `pipeline.py --recreate`, then the audit queries above.