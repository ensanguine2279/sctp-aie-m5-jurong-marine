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
| `extract` | Tesseract reads **three versions** of the page (below). On incident forms it also reads the severity digit from the **blue value ink** alone (below). Results are cached by the image's SHA-256 in `.cache/ingest_images/` (git-ignored), so unchanged images aren't read again; `--no-cache` turns it off |
| `classify` | `incident_report` (title, `WSH-F02`, `Report No`), `nameplate` (`SERIAL`, `ASSET TAG`, `MFG DATE`), or `unknown` |
| `parse_*_text` | Label -> value, tolerant of OCR splitting a word (`Loc ation/machine`). On nameplates the maker is the bold capitals line above `MODEL`, never a label line (`plate_maker`), and oil names are cleaned **before** the vote (`clean_lubricant`: `IS0`/`1S0`/`[S0`/`SO` -> `ISO`, `vVG` -> `VG`, `Tonna S3 N68` -> `M68`, trailing `;` dropped) so the three readings of a plate can agree |
| `vote` | Per field, up to **four readings** are voted: the three page versions plus, for severity on a form, the blue-ink reading (below) |
| `clean_*` | Ids (`IR-2024-9OO` -> `IR-2024-900`, `EMPOO1` -> `EMP001`; letters->digits only where digits belong, so `BLAST` stays `BLAST`), numbers and units (weight in lb or t -> kg, flagged), dates (same day-first parser as `ingest_cmms.py`), severity (below) |
| `split_rejects` | Image rejected: `unreadable_image`, `unknown_document_type`, `duplicate_image` (same bytes as another image), `missing_key` (no readable tag or report no), `duplicate_key` / `duplicate_key_conflict` (a second image for the same machine or incident; conflict = its values differ, so review it) |
| `add_*_warnings` | Loaded, but flagged (table below) |
| `load` | One transaction; a failed load changes nothing |

### Four-pass voting

No single reading gets every field right, so each image is read several ways and the
fields are voted (`preprocess_variants`, `vote`):

| Pass | How the image is prepared | Why it's there |
|---|---|---|
| 1. `gray` | Grey, upscaled 2x (cubic) | Keeps thin and faint strokes. Wins ties |
| 2. `otsu` | Denoised (`fastNlMeansDenoising`), then one global Otsu threshold | Cleanest on evenly lit scans. Also used to find signature ink |
| 3. `adaptive` | Light blur, then a local (Gaussian, 31 px) threshold | Copes with shadows and uneven lighting on photos |
| 4. `blue_ink` | Incident forms only: the severity digit read from the blue value ink (next section) | Recovers the severity when the label print covers it |

Each page pass is OCR'd in full (`--psm 6`) and parsed on its own. Then, per field:

1. **Format check first.** Only readings that pass the field's check are counted
   (`IR-yyyy-nnn`, `EMP001`, a machine id like `BLAST-01`, an ISO VG oil grade, `1000 h`).
   If none passes, all are counted and the result is flagged `failed_format_<field>`.
2. **Majority.** Readings that differ only in case, spaces or punctuation count as the same.
   The value most passes agree on wins.
3. **Ties** go to the earlier pass: gray, otsu, adaptive, then blue ink. The blue-ink pass
   is last on purpose, so it **can never outvote page readings that agree**. It only
   decides the severity when the page passes lost it.
4. **Nothing is thrown away.** Every disagreement is kept in `ocr_notes` with each pass's
   reading and the one chosen, flagged `ocr_disagreed_<field>`.
5. **No majority is flagged separately.** When no two readings agree, the value is only a
   tie-break, so it is also flagged `ocr_no_majority_<field>`. On the client's plates this
   caught CRN-02's grease interval (`509 h` / `566 h` / `500 h`: stored 509, plate says 500)
   and CNC-03's (`590 h` / `5@8 4` / `509 4`: stored 590, plate says 500); see
   [trap list I15](trap_list.md). Treat these values as unread and cite the photo.

A unanimous vote isn't proof either: on WELD-02 all three passes read `Tonna S3 M58`
where the plate says M68. The format checks are the second line of defence: 58 isn't an
ISO VG grade, so it is flagged `failed_format_lubricant`. The manufacture date must be a
real month and a year from 1950 to now (`plausible_mfg_date`), so `06/2820` and `62/2016`
lose the vote to `06/2020` and `02/2016`.

Example from the BLAST-01 plate: Otsu reads `1000 h` as `1660 h` and the tag as `BLAST!`,
gray reads `M68` as `M66`. The vote gets all three right: `1000 h` (2 of 3), `BLAST-01`
(Otsu's `BLAST!` fails the tag check, so it isn't counted), `M68` (2 of 3).

### Colour separation: severity from the blue value ink

On the new WSH-F02 form (from July 2025) the label `Severity (5 = MOST SEVERE, new WSH
matrix):` runs into the value column, and the severity number is printed **in blue over the
grey word "WSH"**. All three page passes then read the line as something like
`new W8H matrix`, and the number is lost ([trap list I12](trap_list.md)).

The form prints every **value in blue** and every **label in grey/black**, so separating by
colour leaves the digit on its own (`severity_from_ink`):

1. **Find the line.** `severity_band` locates the word "Severity" with
   `image_to_data` and crops its line, from the label to the right edge.
2. **Keep only blue ink.** `value_ink_mask` turns blue pixels black and everything else
   (grey print, paper) white. Two kinds of mask:
   - **`excess`:** blue channel minus red channel above a threshold (and not near white).
     Grey print has blue ≈ red, so it drops out, and blue ink still shows **where it
     overlaps the grey letters**, which a hue test misses.
   - **`hsv`:** hue 95–145 (blue) with at least a set saturation. Cleaner strokes on parts
     of the digit that don't overlap the print.
3. **Read each mask.** Tesseract in single-line mode (`--psm 7`) with only `12345` allowed,
   so it can't return a letter or a digit outside the scale.
4. **Vote across 10 masks** (`INK_MASKS`: both kinds, at 1x and 4x scale, with different
   thresholds, blur and closing). One mask doesn't suit every scan: a low threshold keeps
   thin strokes where blue crosses grey but merges a bold digit with the print, and a high
   one breaks strokes. `ink_majority` accepts a digit only if **at least 3 masks** read it
   **and more than twice as many as any other digit**. Otherwise it **abstains**: the
   severity stays unread and flagged, never guessed.
5. The digit joins the field vote as the fourth pass. When it supplies a value no page pass
   read, the note `read_from_value_ink` is added, flagged `read_from_value_ink_severity`.

Result on the 9 new-scale forms: 6 read, **0 wrong**. 904, 913, 939, 953 and 975 agree
with the CMMS; 906 reads 3 (its CMMS row is `ambiguous`, July 2025). The reader abstained
on 911, 922 and 955, which stay NULL. Query: [trap list I12](trap_list.md).

The cache file name carries the engine tag (`tesseract-v2` since the blue-ink pass was
added). Change `TESSERACT_TAG` whenever preprocessing changes, so old cached reads are
ignored; old-tag files can then be deleted.

## Severity: the scale printed on the form

The CMMS severity scale flipped in July 2025 and old records were never converted
([trap list C1-C3](trap_list.md)). `ingest_cmms.py` converts **by date**. The forms print
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
| `read_from_value_ink_severity` | The number came from the blue-ink pass only; the page passes lost it |

`incident_report_vs_cmms` (made by `relationships.py`) compares the form's
`severity_5_worst` with the CMMS's, field by field.

## Tables

| Table / view | What it holds |
|---|---|
| `img_documents_raw` | One row per image: `source_folder`, `source_file`, `image_sha256`, `doc_type`, `ocr_engine`, `fields_raw` (JSON, each field as read), `ocr_notes` (JSON, disagreements and blue-ink reads), `ocr_text` (everything Tesseract read, all three page versions), `status`, `reject_reason`, `duplicate_of_raw_id`, `warnings` |
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
| `ocr_disagreed_<field>` | both | The passes' readings differed; see `ocr_notes` for each one and which was kept |
| `failed_format_<field>` | both | No reading passed the field's format check; the value is kept as read |
| `ocr_no_majority_<field>` | both | No two readings agreed; the value is a tie-break. Treat it as unread and check the photo |
| `read_from_value_ink_<field>` | form | The value came only from the blue-ink pass (so far only `severity`) |
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
  git-ignored. Delete files from old engine tags once the new ones exist, and the whole
  cache with the rest of the data after the project.
- `--engine llm` sends the images through the class proxy (Singapore VM) to the model.

## Run summary

```
Rows read: 2   loaded: 2   rejected: 0
Document types: incident_report=1, nameplate=1 (total 2)
OCR engine: tesseract-v2=2 (total 2)

nameplates: loaded 1
  Loaded with warnings: duty_cycle_rating_on_non_welder=1, ocr_disagreed_asset_tag=1, ocr_disagreed_grease_interval=1, ocr_disagreed_lubricant=1, ocr_disagreed_power=1, placeholder_manufacturer=1 (total 6)

incident_reports: loaded 1
  Loaded with warnings: date_assumed_day_first=1, root_cause_in_attachment=1 (total 2)
  Severity scale on forms: 1_worst=1 (total 1)

Machine ids: checked against cmms_machines (12 machines)
```

One row is one image. `Rows read` = loaded + rejected. (Sample run on the first two scans;
the full folder is 42 images: 30 forms, 12 nameplates.)

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

-- Severities recovered from the blue value ink, and how they compare with the CMMS
SELECT r.incident_id, r.severity_5_worst AS form, c.logged_value AS cmms, c.agrees
FROM img_incident_reports_audit r
JOIN img_documents_raw d ON d.raw_id = r.raw_id
LEFT JOIN incident_report_vs_cmms c
       ON c.incident_id = r.incident_id AND c.field = 'severity_5_worst'
WHERE d.warnings LIKE '%read_from_value_ink%';

-- Every reading of every field OCR was unsure about (as the ingest user)
SELECT source_file, warnings, ocr_notes FROM img_documents_raw WHERE ocr_notes IS NOT NULL;

-- Rejected images
SELECT source_file, doc_type, reject_reason, duplicate_of_raw_id FROM img_documents_raw
WHERE status = 'rejected';
```

## Tests

`tests/test_images.py` (87 tests, no database, Tesseract or LLM needed): the OCR text the
sample scans actually produced, with Tesseract's mistakes, is passed in directly.
Covers classifying, label parsing (`Loc ation`), the printed-scale label in every form
(`1 = MOST SEVERE`, `5=WORST`, `1 = least severe`, unreadable), voting (majority, format
checks, ties, spacing-only differences, the only reading shaped like a machine id), id fixes
that keep `BLAST` as `BLAST`, both document types end to end, a form contradicting the date
rule, a July 2025 form settling the ambiguity, an unreadable scale never guessed, a name
instead of an `EMP` code never stored, welding ratings, lb -> kg, every reject reason,
duplicates against earlier folders, the LLM engine's fields, the cache, the folder hash, the
audit views leaving OCR text out, and a dry run the pipeline can parse.

Nameplates (from the 12-plate audit, with the readings Tesseract actually made): oil-name
misreadings undone and grouped in the vote, a grade that doesn't exist still flagged,
`ocr_no_majority_*` only when no two readings agree, impossible manufacture dates rejected
so the real date wins, and a label line never taken as the maker.

Blue-ink pass: it recovers a severity the page text lost (and flags it), it never outvotes
page readings that agree, without it the severity stays unread, `ink_majority` abstains on
weak or split votes, and on a drawn mock of the overlapping line it returns the right digit
or nothing, never another digit.

`test_the_real_scans_read_correctly` and `test_the_real_new_forms_give_their_severity_from_the_value_ink`
(904 -> 2, 906 -> 3) OCR the real scans when Tesseract and `data/images/` are present, and
are skipped otherwise.

**Not covered:** the MySQL DDL, the load and the two views in `relationships.py` against a
real server. Check with `pipeline.py --recreate`, then the audit queries above.