# Trap list: data-quality problems found so far

Week 1 checkpoint (due Thu 5 Nov 2026). Every problem we have found in the client's data, with the evidence to find it again. Feeds the one-page data quality report and the client questions in [`README.md`](../README.md#data-concerns-and-open-questions).

**Status key:** **Handled** = an ingest rule deals with it (named) · **Client** = needs an answer from Jurong Marine · **Us** = still to check or decide ourselves.

## Scope: what this covers

| Source | Audited | Rows / size |
|---|---|---|
| `sql/jurongmarine_cmms.sql` | Whole dump, read in full | 12 machines, 32 employees, 1,603 work orders (10001–11603), 85 incidents (900–984) |
| `tabular/critical_spares.xlsx` | Every cell (openpyxl) | 1 sheet `Spares`, A1:H13, 10 parts, rev 11 |
| `logs/vibration_temp_2026H1.csv` | Every row (`scripts/audit_sensor_csv.py`) | 106,384 readings + header, 4.6 MB, SHA-256 `05c18527…6bd023` |
| `images/` (12 nameplates, 30 incident reports) | **All 30 incident forms loaded** and linked to the CMMS by `relationships.py`: 0 orphans after the I10 fix (was 1). All 30 forms compared field by field with the CMMS (`incident_report_vs_cmms`). Checked by eye: `IR-2024-900.jpg`, `IR-2025-904.jpg`, `IR-2025-906.jpg`, `IR-2025-914.jpg`, `nameplate_BLAST-01.jpg`. The 12 nameplates aren't reviewed yet | 30 incident forms (21 old-scale, 9 new-scale); nameplates not yet reviewed |
| `docs/sops/` | **Not yet** | |

`vibration_temp_sample.csv` is exactly the first 7 lines of the full file, so it adds nothing of its own. The rest of the images and the SOPs will add traps; the brief already warns the nameplate photos are bad.

---

## CMMS dump (`jurongmarine_cmms.sql`)

| ID | Trap | Evidence | Why it matters | Status |
|---|---|---|---|---|
| C1 | **Severity scale flipped in July 2025; old records never converted** | Line 2 comment: 1 = worst until 30 Jun 2025, 5 = worst from 1 Jul 2025. The data agrees. Before the cutover (48 incidents) severities are 5:18, 4:18, 3:6, 2:4, 1:2. After (37) they are 1:9, 2:15, 3:8, 4:3, 5:2. Converting old values with `6 − s` gives 1:18, 2:18, 3:6, 4:4, 5:2, the same shape as the new period. | A naive average says severity "fell" from 3.96 to 2.30; on one scale it is 2.04 → 2.30. The auditor will test this. | Handled: `normalise_severity` (`severity_5_worst`). The scanned forms print their scale: see I5 |
| C2 | **Exact switch-over day unconfirmed** | Only two incidents fall in July 2025: **906** (11 Jul, logged 3, same on both scales) and **907** (4 Jul, logged 2: either 2 or 4 on the new scale). | 907 is the one record whose meaning depends on the answer. | Client (Q1); July left `ambiguous`. There is no form for 907. Form 906 (11 Jul) already prints the new scale, so the switch happened between 1 and 11 Jul 2025; only 907 falls in that window (I5) |
| C3 | **Severity doesn't track lost days** | Old-scale 5 (least severe) with 5 lost days: **901, 919, 920, 937**. **982** logged 4 (minor on the old scale) has **14** lost days, the highest in the dump. New scale: **933** severity 5 with 0 days, **944** severity 4 with 5 days. | Either lost days aren't tied to severity, or some old records already used the new scale, which would break the C1 rule per record. | Client (Q2). **Not a scale mix-up:** the forms for 919, 937 and 982 print the old scale (1 = most severe) and agree with the CMMS, so the mismatch is on the original paper. 901 and 920 have no form (I5) |
| C4 | **`lost_days` only ever takes 0, 1, 2, 5 or 14** | No incident has 3, 4 or 6–13 lost days across all 85. | Looks like a coded band, not a count of days. "Total lost days" may be meaningless. | Client (Q10) |
| C5 | **`incident_id` is not in date order** | 900 = 2024-07-01, 902 = 2024-04-16, 904 = 2025-12-29. The earliest incident is **917** (2024-01-24). | "First/latest incident" by id is wrong. Always order by `incident_date`. | Us: rule for the agent's SQL |
| C6 | **Descriptions don't fit the machine** | Work orders: 10005 `spindle vibration high` on WELD-02, 10006 `tool changer jam` on CRN-02 (jib crane), 10018 `crane limit switch fault` on PMP-T01, 10029 `weld porosity complaints` on CNC-02, 10045 `weld porosity` on CRN-01, 10228 `crane limit switch fault` on CNC-02. Incidents: 903 `weld flash burn` on CNC-02, 923 `finger caught in chuck` on CRN-01, 951 the same on BLAST-01. | The descriptions look drawn independently of the machine. CNC-02's history (the night-shift question) is affected. The app must cite descriptions, not reason from them. | Client (Q3). The paper form for 900 has the same mismatch (I6) |
| C7 | **Employee trade doesn't fit the work** | 11 of 32 staff are QA Inspectors, and they do repairs: 10001 EMP002 (QA) on a CRN-01 gearbox. Crane operators work on CNCs: 10029 and 10042 EMP005 (Crane Op) on CNC-02. | "Who fixed it / was it a qualified tech" answers will look absurd. | Client (Q11) |
| C8 | **One name on two employee codes** | `Nurul Aisyah binte Hassan` is both EMP015 and EMP026, both QA Inspector, night. | Per-person counts may double or split. | Client (Q5) |
| C9 | **Likely double-entered work orders** | Same machine, day and type: **10727 + 10728** (CNC-02, PM, 2025-02-01, both EMP009). Also 10171/10172 (LTH-01 PM 2024-03-27), 11122/11123 (BLAST-01 PM 2025-09-10), 11189/11190 (CNC-01 PM 2025-10-21), 11429/11430 (CNC-01 PM 2026-03-14), all by different staff. | Different `wo_id`s, so the key-based duplicate check won't catch them. Inflates PM counts and costs. | Us: count them with a query; Client (Q12) for the rule |
| C10 | **Open work orders already carry hours and costs** | 34 WOs are not closed (22 `OPEN`, 12 `AWAITING PARTS`), all in June 2026 (11550–11601), and every one has `duration_hrs` and `cost_sgd`, e.g. 11550 OPEN S$941.13. Every WO before 11550 is `CLOSED`. 11594 is a *preventive* WO awaiting parts. | Cost totals mix estimates with actual spend. The status field only means something for the last month. | Client (Q7) |
| C11 | **Work orders don't record parts** | No part column. WO **11570** (CRN-01, `crane limit switch fault`, 2026-06-10) is `AWAITING PARTS` while spare **LS-CXT-11** (crane limit switch, CRN-01) is at qty 0 < min 1 with a 4-week lead time (xlsx row 11). | The obvious link can only be guessed from text. The app can't answer "what is WO X waiting for". | Client (Q9) |
| C12 | **`CORRECTIVE` vs `BREAKDOWN` undefined** | Both are unplanned repairs on the same kinds of fault, e.g. `coolant leak` appears as both. | "Breakdowns per machine" depends on the definition. | Client (Q6) |
| C13 | **Outlier duration** | WO **11512** (MILL-01, PM, 2026-05-05) takes 24.1 h. The next longest is 18.9 h (10401). | Plausible (a long PM) or a typo (2.41). | Client (Q13) |
| C14 | **The dump is destructive if executed** | `DROP TABLE IF EXISTS` for each table, `CREATE DATABASE`, `SET FOREIGN_KEY_CHECKS = 0`. No foreign keys are declared. | Running it can overwrite tables. Integrity was never enforced at source. | Handled: `ingest_cmms.py` parses, never executes. `relationships.py` checks the links |
| C15 | **The severity rule exists only as an SQL comment** | Line 2. | Any loader that drops comments loses the only statement of the rule. | Handled: documented in `ingest_cmms.md` and the run summary |
| C16 | **Personal data (PDPA)** | `emp_name` holds full names of all 32 staff. In a 32-person shop, `emp_code` + trade + shift may still identify a person. | Names must never reach prompts or answers. | Handled: `agent_user.py` blocks names. Us: decide on `emp_code` in views |

---

## Critical spares sheet (`critical_spares.xlsx`)

| ID | Trap | Evidence | Why it matters | Status |
|---|---|---|---|---|
| S1 | **Header isn't on row 1** | A1 is a title, row 2 is blank, headers are on row 3. The title holds the revision (`rev 11`) and a person's name (`storeman: Halim`). | `read_excel` defaults read the title as the header. The name is personal data. | Handled: header search on `Part No`. The title is stored only in the raw table |
| S2 | **Costs are text, no currency, mixed per-unit** | F4 `$12.50` … all `$` strings. F10 `$38/spool` is per spool, not per piece. F5 `$48` is a bearing sourced from Germany. | Can't sum or compare without parsing. The currency (SGD? USD? EUR?) is unknown. | Handled: `parse_cost` (`cost_per`). Client (Q8): currency |
| S3 | **Lead time is free text in seven forms** | G4 `3 days`, G5 `2-3 weeks (Germany)`, G6 `1 wk`, G7 `6-8 weeks!!`, G11 `4 wks`, G13 `2 wks`. G8, G9, G10, G12 say `local`, which is not a duration. | "Which part takes longest" needs day ranges. `local` has no number. | Handled: `parse_lead_time` (`is_local_supply`) |
| S4 | **Metric and imperial mixed in descriptions** | Inches: B6 `85 in`, B7 `1-3/8 inch`, B10 `0.045in`, B13 `10in`. Millimetres: B9 `2.4mm`. Bearings B4 `6205`, B5 `6310` are metric only by designation (25 mm and 50 mm bore), with no unit written. | The brief's named landmine. Size comparisons and searches fail without conversion. | Handled: `parse_size` → `size_mm` (bearing designations deliberately not decoded) |
| S5 | **Part number encodes size differently from the description** | A6 `BELT-5V-850` vs B6 `85 in`. 5V belt numbers give length in tenths of an inch, so 850 = 85.0 in, **not 850 mm**. | A model or a human reading the part number gets the size wrong by about 2.5×. | Us: note for the agent; size from the description only |
| S6 | **One cell lists several machines** | C9 `CNC-01/02/03`, C10 `WELD-01/02`, C12 `ALL`. | A plain join on machine misses these parts. | Handled: `split_machines`. `machine_spares` expands `ALL` |
| S7 | **Quantity units are implicit** | D9 44 (inserts), D10 12 (spools of wire), D12 9 (20 L drums of oil, per B12). | "How much hydraulic oil" = 180 L, not 9. | Us: unit-of-issue column or agent note |
| S8 | **Location format inconsistent** | H12 `DRUM YD` vs the `S1-A2` pattern everywhere else. | Pattern-based parsing of shelf/bin fails. | Us: low priority |
| S9 | **Three machines have no spares listed** | Nothing for MILL-01, CRN-02 or BLAST-01 (only `ALL` hydraulic oil). | The app must say "no critical spares recorded", not guess. | Us: eval case. BLAST-01's nameplate names a different oil (I3) |

Not errors, but the app must get them right: below minimum stock are **BRG-6310** (CNC-02, 2 < 4, lead 2–3 weeks), **SEAL-KIT-KSB** (PMP-T01, 1 < 2, lead 6–8 weeks) and **LS-CXT-11** (CRN-01, 0 < 1, lead 4 weeks, see C11). **CHUCK-JAW-10** is exactly at minimum.

---

## Sensor log (`vibration_temp_2026H1.csv`)

Line numbers count the header as line 1. Reproduce with `python scripts/audit_sensor_csv.py data/logs/vibration_temp_2026H1.csv` (about 40 s).

**Shape:** 5 machines (CMP-01, CNC-01, CNC-02, CNC-03, PMP-T01), about 21,270 readings each, one reading per machine every 12 minutes from 2026-01-01 00:00 to 2026-06-30 23:48 SGT. Every timestamp sits exactly on the 12-minute grid, and the file is in time order throughout.

| ID | Trap | Evidence | Why it matters | Status |
|---|---|---|---|---|
| L1 | **Two timestamp formats, mixed row by row** | 79,596 rows (74.8%) are ISO with `+08:00`, e.g. line 2. 26,788 rows (25.2%) are epoch ms, e.g. line 4 `1767196800000`. Both appear in every month and for every machine; at line 32 a CNC-02 reading switches format mid-stream. Epoch 1767196800000 = 2025-12-31 16:00 UTC = 2026-01-01 00:00 SGT, **the same instant** as line 2. | If epoch is read as UTC and ISO as local wall time, a quarter of all readings land 8 h away from their neighbours (this bug existed in the original script). | Handled: `parse_timestamps` → `epoch_ms` + `sampled_at`. Test `test_iso_with_offset_and_epoch_give_same_time` |
| L2 | **Celsius and Fahrenheit mixed, reading by reading** | 90,578 `C`, 15,806 `F` (14.9%), spread evenly over all five machines and all months. CNC-02 changes unit 5,165 times; its longest Fahrenheit run is 5 readings (lines 22–37). Raw F values run 88.5–165.6. | Unconverted, a fifth of readings look 60–110 °C hotter: CNC-02 would show "134.6 °C" at line 22. Any average or chart over the raw column is wrong. | Handled: `clean_temperatures` → `temp_c` |
| L3 | **The F labels can be trusted** (checked, no trap) | After conversion, F readings average 51.9 °C (sd 5.6) against 52.3 °C (sd 6.0) for C readings. No F reading is below 80 and no C reading is above 80, so no reading looks mislabelled. | Confirms converting by the label is right; there's no need to guess units from values. | Checked |
| L4 | **Small gaps: 2,216 missing readings, none longer than 24 minutes** | Each machine should have 21,720 readings and is missing 429–456 (about 2%). Each gap is one or two missed slots: CMP-01 has 428 steps of 24 min and 8 of 36 min. Example: CNC-02 jumps from line 13 (00:24) to line 22 (00:48). Gaps are spread evenly by month. All five machines are present in 19,581 of 21,720 slots. | Not the long outages the brief implies. Hourly or daily averages are unaffected, but code that assumes "row n+1 is 12 min later" or "every slot has all 5 machines" breaks. | Us: state in the data quality report; use time-based resampling, not row offsets |
| L5 | **Only 5 of the 12 CMMS machines have sensors** | No readings for MILL-01, WELD-01, WELD-02, CRN-01, CRN-02, BLAST-01 or LTH-01. | "Is the crane running hot?" has no data; the app must say so, not guess. | Us: eval case |
| L6 | **Temperature is very noisy reading to reading** | CNC-02 goes 36.5 °C (line 47, 01:48) → 65.7 °C (line 52, 02:00) in 12 minutes. The overall sd is about 6 °C for a mean of 52 °C. | A raw line chart is unreadable and single readings prove nothing. The CNC-02 chart must use daily or weekly means. | Us: smoothing in the analysis |
| L7 | **Isolated extreme readings** | Highest temperature: CNC-02 79.8 °C (line 97967, 2026-06-16). Lowest vibration: CNC-03 0.2 mm/s (line 59744). CMP-01 76.4 °C (line 72055). Only 9 readings in the whole file are more than 4 sd from their machine's mean. | Single spikes, plausible as real or as sensor glitches; too few to change any average. | Us: report, don't drop |
| L8 | **Windows line endings (CRLF)** | All 106,385 lines end `\r\n`. | A reader that splits on `\n` only leaves `\r` on the last field: the header becomes `temp_unit\r`, so the load stops with "missing column", and any value read past that would carry `C\r`. | Handled by pandas (and `normalise_unit` strips whitespace). Tests `test_crlf_file_keeps_every_temperature`, `test_crlf_and_lf_give_the_same_result` |

**Checked and not present:** blank or unparseable timestamps, ISO times without an offset or with `Z`, machine id variants (`CNC2`, lower case, spaces), other unit spellings (`°C`, `degF`, `K`, blank), non-numeric or negative readings, and duplicate readings (same machine and instant in any format). The ingest rules for these stay in place in case a later file has them.

Not a data error, but the headline for Ask 2: **CNC-02's vibration rises from 2.31 mm/s (Jan–Mar monthly mean) to 2.71 (Apr), 3.56 (May) and 4.41 (Jun)**, and its temperature from 52.0 to 56.0 °C, while the other four machines stay flat to within 0.02 mm/s. Day and night shifts read the same, so it is the machine, not the shift. The CMMS shows a CNC-02 `spindle vibration high` breakdown on 2026-05-15 (WO 11529) and an open `bearing noise - gearbox` WO on 2026-06-19 (11591), and the spares sheet has CNC-02's bearing BRG-6310 below minimum (2 < 4, lead 2–3 weeks).

---

## Images (`images/`): first two scans

Read with `python scripts/ingest_images.py data/images --dry-run` ([`ingest_images.md`](ingest_images.md)), which reads each image three ways (Tesseract on three preprocessed versions) and votes per field. The scans below were also checked by eye. All 30 forms are loaded and compared with the CMMS field by field: the only disagreements are five truncated descriptions (I11), and the only fields that couldn't be compared are the severity numbers on three new-scale forms (911, 922, 955) and on 906, whose CMMS value is `ambiguous` (I12).

**Incident form `IR-2024-900.jpg` vs CMMS incident 900:** all seven shared fields agree (date 2024-07-01, LTH-01, EMP001, `weld flash burn`, severity 4, 0 lost days, `found during audit`). The form adds immediate action, root cause, signature and the printed severity scale.
**Nameplate `nameplate_BLAST-01.jpg` vs CMMS machine BLAST-01:** model `Custom SG-Fab` and the 2014 date agree. The plate adds serial, power, weight, lubricant and grease interval, which no other source has.

| ID | Trap | Evidence | Why it matters | Status |
|---|---|---|---|---|
| I1 | **OCR reads the same photo differently each time** | On `nameplate_BLAST-01.jpg`, three preprocessings of one photo read the grease interval as `1000 h` / `1660 h` / `1000 h`, the oil as `Shell Tonna S3 M66` / `M68` / `(68`, the voltage as `389-440V` / `380-440V`, the tag as `BLAST-01` / `BLAST!`, and `S3` as `$3`. The incident form: `IR-2024-9OO`, `EMPOO1`, `Loc ation`. | One OCR pass would have stored a grease interval 66% too long, the kind of number a tech acts on. | Handled: `vote` (majority + format checks), every disagreement in `ocr_notes` and flagged `ocr_disagreed_<field>`. Test `test_vote_takes_the_majority_reading_that_passes_its_check` |
| I2 | **BLAST-01's plate carries a welding rating** | `CAPACITY 450 A @ 60%` is a current at a duty cycle, how welding power sources are rated, on a blasting chamber. | Either the plate is on the wrong machine, or the capacity line is wrong. Questions about the chamber's capacity get a welder's answer. | Flagged `duty_cycle_rating_on_non_welder`. Client (Q15) |
| I3 | **Two sources for "what oil does BLAST-01 take", which may disagree** | The plate says `Shell Tonna S3 M68`, a slideway oil sold for machine-tool guideways. The spares sheet lists only the `ALL` hydraulic oil for BLAST-01 (S9). | A cross-source question the client lists in Ask 1. The app must cite both, not pick one. | Client (Q15). Us: eval case |
| I4 | **Manufacturer is a placeholder** | Plate says `CUSTOM`; CMMS model `Custom SG-Fab`. They agree, but the real maker isn't named anywhere. | "Who made it / who do we call" has no answer. | Flagged `placeholder_manufacturer` |
| I5 | **Each form prints the severity scale it used** | `Severity (1 = MOST SEVERE): 4` on IR-2024-900 (Jul 2024): the old scale, as the date rule says, so 4 → 2 on the 5 = worst scale, matching the CMMS. | Per-record evidence for C1–C3: the forms can confirm the date rule, catch old records already on the new scale (C3), and settle 907 (C2). | Handled: `severity_scale` from the label, `severity_5_worst`; flags `form_scale_contradicts_date_rule`, `form_scale_in_cutover_month`; view `incident_report_vs_cmms`. **Checked on all 30 forms:** the 21 dated before July 2025 print the old scale, the 9 after it print the new one (`5 = MOST SEVERE, new WSH matrix`); no form contradicts the date rule, and every old-scale form's converted severity matches the CMMS. Form 906 (11 Jul 2025, CMMS `ambiguous`) prints the new scale: evidence for Q1. No form for 907 |
| I6 | **The paper form repeats the description mismatch** | Form 900 says `weld flash burn` on `LTH-01 — workshop bay` (a manual lathe), like the CMMS. | For 900 at least, C6 isn't a CMMS typing error: the mismatch was on the original form. | Client (Q3) |
| I7 | **Root cause is in an attachment we don't have** | `Root cause (5-Why attached?): see attached`; no 5-Why sheet is in the scan or the data package. | "Why did incident 900 happen" can't be answered; the app must say the 5-Why isn't in the data. | Flagged `root_cause_in_attachment`. Client (Q14) |
| I8 | **Slash dates are day first** (checked, no trap) | Form 900 `01/07/2024` = CMMS `2024-07-01`; form 914 `03/01/2025` = CMMS `2025-01-03`. Both would be wrong read month-first. | Confirms the day-first assumption used by `ingest_cmms.py` and `ingest_images.py`. | Checked on two forms |
| I9 | **Personal data on the forms** | Employee code and a handwritten signature on every form. | Signatures are personal data; a form may name a person instead of a code. | Handled: only `signature_present` stored; a person that isn't an `EMP` code is not stored in the clean table; raw OCR not granted to the app user |
| I10 | **A too-loose machine-id check let an OCR misread win the vote** | `IR-2025-914.jpg` (CMMS incident 914, CRN-02). The location was read three ways: `CRN-O02` (gray), `CRN-G2` (otsu), `CRN-02` (adaptive). The loaded row had `machine_id = CRN-002`. The format check `TAG = [A-Z]{2,6}-[A-Z]?\d{1,3}` had been loosened so `PMP-T01` would pass, which let all three readings pass too; with no two agreeing, the tie went to the first pass (gray), and the id clean-up turned its letter O into a zero. The stricter `[A-Z]{2,6}-\d{1,3}` rejects the real id `PMP-T01`. Every other field matches incident 914 (date, EMP029, `weld flash burn`, severity 5 on the printed old scale, 0 lost days, `reported same day`); the garbled severity (`3)` / `5` / `5`) was fixed by majority. | It was the only orphan in `table_relationships` (`report_machine`, 1 of 30). A wrong machine on an incident sends "incidents on CRN-02" answers astray, and with `--foreign-keys` the link can't be enforced. | Handled: `TAG = [A-Z]{2,6}-[A-HJKMNPR-Z]?\d{2,3}` (a letter after the dash, but not I, L, O, Q or S, which OCR confuses with digits; then 2-3 digits): passes `PMP-T01`, `CRN-02`, `BLAST-01`; rejects `CRN-O02`, `CRN-G2`, `BLAST-O1`. Tests `test_vote_keeps_the_only_reading_shaped_like_a_machine_id`, `test_machine_id_check_allows_a_letter_but_not_a_misread_digit`. Flagged `ocr_disagreed_location`; the CMMS value is untouched. Reloaded and re-linked 4 Nov 2026: 211 tests pass, 0 orphans; form 914 now has `CRN-02`. Forms naming `PMP-T01` (941, 955) get their machine (test `test_incident_form_on_a_machine_with_a_letter_in_its_id`) |
| I11 | **The longest description is cut off on the paper forms** | Forms 924, 936, 954, 968 and 982 read `falling object from crane path - helmet cracke`; the CMMS has `...cracked`. It is the longest description in the data (47 characters) and every form that has it loses the last letter. All three OCR passes read `cracke` (no `description` note in `ocr_notes` for 924), so the letter is missing from the image itself, not misread. | These are the only five disagreements across all 30 forms. A search or exact match on the description fails between form and CMMS. | Us: compare descriptions by prefix, or treat a form value that is a prefix of the CMMS value as agreeing; report it, don't fix the scan |
| I12 | **The new form hides the severity number under its own label** | The new-scale label `Severity (5 = MOST SEVERE, new WSH matrix):` runs into the value column, and the number is printed in blue over its `WSH` (seen on `IR-2025-904.jpg`, `IR-2025-906.jpg`). OCR reads the line as `new W8H matrix` and the number is lost. Before the fix this happened on **all 9** new-scale forms (904, 906, 911, 913, 922, 939, 953, 955, 975) and on none of the 21 old ones, so their severity couldn't be checked against the CMMS. | On exactly the forms that use the new scale, the paper couldn't confirm the CMMS severity. | Handled for 6 of 9, with 0 wrong readings. Every value on the form is blue and every label grey, so `severity_from_ink` keeps only the blue pixels on the severity line and reads the digit through 10 masks, voting (abstains unless at least 3 agree and no other digit comes close). It is a fourth reading in the vote, never outvotes page readings that agree, and flags `read_from_value_ink_severity`. Cache tag `tesseract-v2`; 10 tests, including one on the real scans 904 and 906. **Reloaded 4 Nov 2026 (221 tests pass, 0 orphans):** 904 → 2, 913 → 5, 939 → 3, 953 → 3, 975 → 2, all `agrees = 1`; 906 → 3 (CMMS `ambiguous`, so not compared: evidence for Q1). The reader abstained on **911** (CMMS 2), **922** (CMMS 4) and **955** (CMMS 1): their severity stays unread and unchecked, never guessed. Us (optional): look at those three scans; only change the reader if the other six still read right |
| I13 | **A lost-day injury recorded as a near miss** | Form 924: root cause `N/A near miss`, but the description is a cracked helmet and the CMMS has 1 lost day. | "Near misses this year" and "injuries this year" counts depend on which field you trust. | Client (ask with Q3); one form so far: check the other forms' root-cause text |

---

## Client questions raised by this list

All of them are in one list, Q1–Q15 in [`README.md`](../README.md#needs-the-clients-input). This list added **Q10** (C4, what `lost_days` values mean), **Q11** (C7, why QA inspectors and crane operators log repairs), **Q12** (C9, are same-day PM pairs double entries) and **Q13** (C13, is 24.1 h real), and new evidence for **Q4** (the CNC-02 trend under L-items). The scans added **Q14** (I7, where are the 5-Why sheets) and **Q15** (I2–I3, is BLAST-01's nameplate on the right machine, and which oil does it take), and new evidence for **Q1** (I5: the switch was between 1 and 11 Jul 2025; no form for 907), **Q2** (I5: the old forms confirm the date rule, so C3 is on paper) and **Q3** (I6, I13).

## Reproducing the evidence

```sql
-- C1: severity distribution before / after the cutover, as logged
SELECT incident_date < '2025-07-01' AS old_scale, severity_as_logged, COUNT(*)
FROM cmms_safety_incidents GROUP BY 1, 2 ORDER BY 1 DESC, 2;

-- C4: distinct lost_days values
SELECT lost_days, COUNT(*) FROM cmms_safety_incidents GROUP BY 1 ORDER BY 1;

-- C9: same machine, same day, same type: candidate double entries
SELECT machine_id, wo_date, wo_type, COUNT(*) AS n, GROUP_CONCAT(wo_id) AS wo_ids
FROM cmms_work_orders GROUP BY 1, 2, 3 HAVING n > 1 ORDER BY wo_date;

-- C10: non-closed work orders with costs
SELECT status, COUNT(*), SUM(cost_sgd) FROM cmms_work_orders
WHERE status <> 'closed' GROUP BY 1;

-- I5: scanned forms vs the CMMS, field by field (after the image load + relationships.py)
SELECT incident_id, field, form_value, logged_value, agrees
FROM incident_report_vs_cmms ORDER BY incident_id, field;

-- I10: scanned forms whose machine isn't a CMMS machine
SELECT r.incident_id, r.machine_id AS form_machine, i.machine_id AS cmms_machine, d.source_file, d.ocr_notes
FROM img_incident_reports r JOIN img_documents_raw d ON d.raw_id = r.raw_id
LEFT JOIN cmms_safety_incidents i ON i.incident_id = r.incident_id
LEFT JOIN cmms_machines m ON m.machine_id = r.machine_id WHERE m.machine_id IS NULL;

-- I12: severity on the new-scale forms, form vs CMMS (906 has agrees NULL: CMMS 'ambiguous')
SELECT incident_id, form_value, logged_value, agrees FROM incident_report_vs_cmms
WHERE field = 'severity_5_worst' AND incident_id IN (904,906,911,913,922,939,953,955,975);

-- I1: every field OCR was unsure about, with each reading
SELECT source_file, ocr_notes FROM img_documents_raw WHERE ocr_notes IS NOT NULL;
```