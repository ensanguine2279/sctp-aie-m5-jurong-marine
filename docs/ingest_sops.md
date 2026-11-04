# ingest_sops.py: the SOP documents

`scripts/ingest_sops.py` reads every `.txt` / `.md` file in a folder (by default
`data/sops/`), keeps the ones that are SOPs, splits each into its steps with line
numbers, links it to the machines it names, and loads it with lineage, like the other
sources. It answers Ask 1's third kind of question: "what's the SOP before entering the
blasting chamber?"

```bash
python scripts/ingest_sops.py data/sops --dry-run      # read and check; no database needed
python scripts/ingest_sops.py data/sops --recreate     # first load
python scripts/ingest_sops.py data/sops                # later loads
```

With uv (personal repo): `uv run python scripts/ingest_sops.py data/sops --dry-run`.
Run it after `ingest_cmms.py`, so it can match machine names against `cmms_machines`.

## What the client sent

Three SOPs and one email, plain text, UTF-8, LF line endings ([trap list D1–D10](trap_list.md)):

| File | SOP | Revision | Steps | Machines |
|---|---|---|---|---|
| `SOP-LOTO-01.txt` | Lockout/tagout procedure | rev 4, Jan 2026 | 8 (numbered, plus a closing line) | CNC-01, CNC-02, CNC-03 ("CNC machines", step 6) |
| `SOP-CRANE-02.txt` | Overhead crane lifting | rev 6, Mar 2026 | 6 bullets | CRN-01, CRN-02 |
| `SOP-CONFINED-03.txt` | Confined space entry | rev 2, Sep 2025 | 4 bullets + an "Applies to" line | BLAST-01, PMP-T01 ("test pump sump") |
| `0000_gm_brief_email.txt` | not an SOP: rejected `not_an_sop` | | | |

Every SOP starts with one header line: `JME SOP-LOTO-01 — LOCKOUT/TAGOUT PROCEDURE (rev 4, Jan 2026)`.

## How a file is read

| Step | What it does |
|---|---|
| `read_folder` | Every `.txt` / `.md` directly in the folder, by name, as bytes |
| `decode` | **UTF-8, always** (a BOM is allowed), CRLF -> LF. Never the OS default: on Windows that is cp1252, which turns the em dash in every title into `â€”` (D2). A file that isn't UTF-8 is rejected `not_utf8`, not guessed |
| `parse_document` | The header gives `doc_no`, `title`, `revision`, `rev_month` (`YYYY-MM`; the SOPs give no day). An `Applies to:` line right after it becomes `applies_to`. Every other non-blank line is a step: `1.` keeps its number as the marker, `-` is a bullet, a line with neither (LOTO's closing `VIOLATION = ...`) is a step with no marker. Line numbers count the header as line 1 |
| `split_rejects` | `not_an_sop` (first line isn't `JME SOP-...`: the GM's email, D1), `bad_header`, `not_utf8`, `duplicate_file` (same bytes as an earlier file), `duplicate_revision` / `duplicate_revision_conflict` (same doc no and revision as an earlier file; conflict = the steps differ). **Different revisions of one SOP are all kept**; `is_current` marks the highest (D9) |
| `machine_links` | `id`: a machine id in the text (`CRN-01/CRN-02` gives both; `CNC-01/02/03` gives three). `name`: a CMMS machine name in the text (`blasting chamber`), when the CMMS is loaded. `alias`: a phrase in `ALIASES` (`test pump sump` -> PMP-T01, `CNC machines` -> CNC-01/02/03). Each link records how it was made and the words it came from, because an alias is a judgement |
| `create_tables` | Creates the tables, then `rebuild_fulltext`: fills `sop_ft_stopwords`, sets `innodb_ft_user_stopword_table` for the session, and drops and re-adds both `FULLTEXT` indexes, so MySQL ignores the same words as `search_sops`. On **every** load, not only `--recreate`: MySQL reads the stopword list only when an index is built |
| `load` | Replaces every SOP row, in one transaction. The folder is the complete set of SOPs |

On the client's data a machine is linked by name only where no id or alias already links
it: "blasting chamber" sits next to `(BLAST-01)`, so that link is `id`. A real load with
the CMMS (12 machines) gives the same 7 links as the dry run: `alias=4, id=3`.

## Tables

| Table / view | What it holds |
|---|---|
| `sop_documents_raw` | One row per file: `source_folder`, `source_file`, `file_sha256`, `header_line`, `doc_no`, `revision`, `raw_text` (the whole file as read), `status`, `reject_reason`, `duplicate_of_raw_id`, `warnings` |
| `sops` | One row per SOP revision, key `(doc_no, revision)`: `title`, `rev_month`, `rev_label`, `applies_to`, `is_current`, `step_count`, `full_text` (`FULLTEXT` index with the title) |
| `sop_steps` | One row per step, key `(doc_no, revision, step_no)`: `line_no`, `marker`, `step_text` (as written), `refers_to` (documents it points to that aren't in the data). `FULLTEXT` on `step_text` |
| `sop_machines` | `(doc_no, revision, machine_id)` with `how` (`id` / `name` / `alias`) and `evidence` |
| `sop_ft_stopwords` | One column, `value` (primary key: hosted MySQL with `sql_require_primary_key` refuses a table without one, error 3750): the words both `FULLTEXT` indexes ignore. MySQL's 35 default stop words plus `search_sops`' `_STOP` (`work`, `machine`, `before`, ...), 60 in all. Rewritten on every load |
| `sops_audit`, `sop_steps_audit` (views) | Clean + file + its SHA-256 + run. **Cite these**: "SOP-CONFINED-03 rev 2, lines 3-6 (`SOP-CONFINED-03.txt`)" |

`raw_id` = `run_id * 1000 + file number` (files by name, from 1). `ingest_runs` records the
folder and one SHA-256 over all its files (`lineage.folder_sha256`), as for the images.

## Answering from the SOPs

1. **Find the SOP.** `search_sops(question, sops, links)` first widens the question with
   `expand_question` (`SYNONYMS`: "sandblasting" adds "blasting chamber", "isolating" adds
   "lockout"), then scores the current SOPs: a question word in the title counts 2, in the
   text 1, a machine the SOP is linked to 3. Words every SOP uses ("work", "machine",
   "before") don't count, and a score below 2 is no match. In the database,
   `MATCH (title, full_text) AGAINST (:q)` on `sops`, with `q = expand_question(question)`,
   finds the same candidates, because it ignores the same words (`sop_ft_stopwords`).
2. **Nothing found means no SOP.** 58 of the 85 incidents are of a kind no SOP covers
   (welding, grinding, chucks, portable electrics, housekeeping, D3). For those the app
   must say "no SOP in the data covers this", never stretch another SOP. **`search_sops`
   is the gate for that answer**: it needs a score of 2, while `MATCH` returns any row
   sharing one word.
3. **Quote it whole.** `quote_sop` gives every step of the SOP with its citation:
   `[Source: SOP-CONFINED-03, lines 3-6]`. A single step out of its procedure (step 5 of
   lockout without steps 1-4) is how someone gets hurt.
4. **Say what isn't there.** A step with `refers_to` (the machine manual, the HR operator
   list, a lifting plan, the entry permit: D8) gets "this refers to <document>, which isn't
   in the data". Answers give the revision and month: "current revision, rev 4, Jan 2026"
   (D9).

Checked in the tests: "before entering the blasting chamber" finds SOP-CONFINED-03, "max
load of CRN-01" SOP-CRANE-02, "lock out CNC-02" SOP-LOTO-01, "going inside the
sandblasting booth" SOP-CONFINED-03, "isolating power on the lathe" SOP-LOTO-01, and
"welding hot work PPE for grinding" finds nothing.

## Search: keyword, not a vector database (decision, 4 Nov 2026)

**Decision.** SOPs are found by keyword: `search_sops` in Python, with MySQL `FULLTEXT`
kept in step through the same stop words and synonyms. No embeddings and no vector
database for now. Asked during the first real load: "would it be better to store the SOPs
in a semantic database?"

**Why.**

| | Keyword (chosen) | Semantic / vector search |
|---|---|---|
| A question no SOP covers (D3) | Scores below 2: "no SOP in the data covers this" | Always returns a nearest SOP. Saying "none" needs a similarity cut-off, and nothing explains it |
| A paraphrase ("sandblasting booth") | Missed unless `SYNONYMS` has it | Found |
| Why an SOP matched | The words it shares with the question | A number |
| Storage and lineage | Same MySQL, same one-transaction load, same `_audit` views | A second store, or an embedding column to keep in step with every load |

- **The failure that matters most is a confident wrong SOP.** 58 of 85 incidents are of a
  kind no SOP covers. A vector search would put "welding hot work PPE" near confined
  space (hot-work and confined-space permits share wording) or near lockout, and hand a
  welder the wrong procedure. A missed SOP is bad; a wrong one is worse.
- **Size.** 3 SOPs, 18 steps, each under 1 KB. That is choosing among three documents,
  not retrieval over a corpus.
- **Keyword's weakness is real but small and testable.** Paraphrases that share no word
  with the SOP are missed. The two found so far are in `SYNONYMS` and in the tests, along
  with a test that they're missed without it.

**What was done instead.**

1. **MySQL ignores the same words as `search_sops`.** On the first real load,
   `MATCH ... AGAINST ('welding hot work PPE grinding')` returned SOP-LOTO-01 (score
   0.46) on "After work:" in step 7, because MySQL's default stopword list has no "work".
   `search_sops` returned nothing, which is right. `sop_ft_stopwords` now holds MySQL's
   defaults plus `_STOP`. A user stopword table *replaces* MySQL's list, so the defaults
   are included, written out in the script because reading them from
   `INFORMATION_SCHEMA.INNODB_FT_DEFAULT_STOPWORD` needs the `PROCESS` privilege. It is a
   session setting: no admin rights or Cloud SQL flag needed.
2. **`SYNONYMS` for paraphrases**, applied to the question only, before both
   `search_sops` and `MATCH`. Like `ALIASES` it is a judgement: keep it short, and add an
   entry, with its question as a test, when a real question misses.
3. **`search_sops` stays the gate** for "no SOP covers this" (score of at least 2). A
   `MATCH` score isn't used as a cut-off: it depends on how common each word is across all
   rows, so it shifts whenever the client adds an SOP.

**Revisit when** the client sends dozens of SOPs, or eval questions keep missing and
`SYNONYMS` keeps growing. Then add embeddings as a column in the same database (on
`sops` or `sop_steps`; brute-force cosine is fine at that size). Use them to rank or to
suggest candidates, never as the gate for "no SOP covers this", and keep quoting the
chosen SOP whole from `sop_steps`.

## Warnings

| Warning | Meaning |
|---|---|
| `refers_to_document_not_in_data` | A step points to a document that isn't in the package (D8); `sop_steps.refers_to` says which |
| `no_revision_in_header`, `revision_month_unparsed` | The header has no `(rev N, Mon YYYY)`, or its month can't be read |
| `no_steps` | A header and nothing else |
| `doc_no_differs_from_file_name` | `crane.txt` holding SOP-CRANE-02: fine, but check it's the right file |
| `unknown_machine` | A machine id the CMMS doesn't have (only checked when the CMMS is loaded) |
| `personal_data_in_text` | An email address or Singapore phone number in a step (PDPA): it would be quoted to anyone who asks |

## Run summary

```
Rows read: 4   loaded: 3   rejected: 1
One row is one file.
  SOP-CONFINED-03  rev 2, Sep 2025     4 steps  current  machines: BLAST-01, PMP-T01
  SOP-CRANE-02     rev 6, Mar 2026     6 steps  current  machines: CRN-01, CRN-02
  SOP-LOTO-01      rev 4, Jan 2026     8 steps  current  machines: CNC-01, CNC-02, CNC-03
Rejected files: not_an_sop=1 (total 1)
Loaded with warnings: refers_to_document_not_in_data=3 (total 3)
Machine links by kind: alias=4, id=3 (total 7)
Steps referring to documents not in the data: certified operator list (HR)=1, entry permit=1, lifting plan (signed by PE)=1, machine manual=1 (total 4)
Machine names: not matched (CMMS not loaded); ids and aliases only
```

That is the dry run on the client's folder. A real load after `ingest_cmms.py` says
`Machine names: matched against cmms_machines`, gives the same 7 links, and ends with
`FULLTEXT indexes rebuilt with <n> stop words (sop_ft_stopwords)`. The dry run prints the
steps, and for rejected files only the file name and reason, so the email's addresses
never reach the screen or a pipeline report.

## Personal data (PDPA)

- The GM's email (names, email addresses) is rejected and kept only in
  `sop_documents_raw.raw_text`, which the app user can't read (`*_raw` is never granted).
- Steps are checked for email addresses and phone numbers (`personal_data_in_text`).
- The SOPs name roles ("WSH officer", "shift supervisor"), not people.

## Audit queries

```sql
-- Every SOP, its revision and source file
SELECT doc_no, revision, rev_label, is_current, step_count, source_file, file_sha256
FROM sops_audit ORDER BY doc_no, revision;

-- The SOP for a question, as the app would find it in the database
SELECT doc_no, revision, title, MATCH (title, full_text) AGAINST ('blasting chamber entry') AS score
FROM sops WHERE is_current = 1 AND MATCH (title, full_text) AGAINST ('blasting chamber entry')
ORDER BY score DESC;

-- A topic no SOP covers (D3): must return no rows. Returned SOP-LOTO-01 (0.46, on 'work')
-- before sop_ft_stopwords; re-run after any load that changes the stop words
SELECT doc_no, MATCH (title, full_text) AGAINST ('welding hot work PPE grinding') AS score
FROM sops WHERE is_current = 1 AND MATCH (title, full_text) AGAINST ('welding hot work PPE grinding');

-- The stop words the indexes were built with
SELECT COUNT(*) FROM sop_ft_stopwords;

-- One SOP, whole, with line numbers for the citation
SELECT step_no, line_no, marker, step_text, refers_to FROM sop_steps_audit
WHERE doc_no = 'SOP-CONFINED-03' AND is_current = 1 ORDER BY step_no;

-- Which SOPs cover a machine, and how we know
SELECT doc_no, how, evidence FROM sop_machines WHERE machine_id = 'BLAST-01';

-- Steps that point to documents we don't have (D8)
SELECT doc_no, line_no, refers_to, step_text FROM sop_steps WHERE refers_to IS NOT NULL;

-- Rejected files (file name and reason only)
SELECT source_file, reject_reason, duplicate_of_raw_id FROM sop_documents_raw
WHERE status = 'rejected';
```

## Tests

`tests/test_sops.py` (38 tests, no database): the client's three SOPs exactly as delivered
(em dash, no final newline) and the GM's email, built as bytes. Covers the header (number,
title, revision, month; a hyphen instead of the em dash; no revision), UTF-8 where cp1252
would garble the title, a BOM and CRLF read like plain UTF-8, a non-UTF-8 file rejected,
the email rejected and its text kept out of the SOP rows and off the screen, duplicate
files and revisions (and a conflict), earlier revisions kept with only the highest current,
numbered steps, bullets and an unmarked closing line with their line numbers, `Applies to`
kept apart, gas-test limits kept exactly as written, references to missing documents (and
an incident report not counted as one), personal data flagged, machine links by id, CMMS
name and alias (`CRN-01/CRN-02`, `CNC-01/02/03`, `test pump sump`), an unknown machine
flagged, search for four real questions and two paraphrases (and the paraphrases missed
without `SYNONYMS`), synonyms matching whole words only, nothing for welding, superseded
revisions never searched, the whole SOP quoted with its citation, the `FULLTEXT` stop words
covering `_STOP` and MySQL's defaults, the stopword setting made before both indexes are
re-added (on a recording connection), a primary key on the stopword table, the client folder end to end, and a dry run the
pipeline can parse. `test_the_real_sop_folder` reads `data/sops/` if it's there.

**Checked on a real server (4 Nov 2026):** the DDL, the load (3 SOPs, 7 machine links,
CMMS loaded with 12 machines), `MATCH` for "blasting chamber entry" (SOP-CONFINED-03
only, 1.59), and the stopword rebuild: after it, the welding audit query returns no rows
(60 stop words in `sop_ft_stopwords`).

**The app user** (`agent_user.py`) can read `sops` (for `MATCH`: the `FULLTEXT` index is on
the table), `sops_audit`, `sop_steps_audit` and `sop_machines`. It can't read
`sop_documents_raw`, which keeps the GM's email; `--check` proves that. Re-run
`agent_user.py` after the first SOP load.

**Not wired in yet:** `pipeline.py` doesn't run this step, and `relationships.py` doesn't
check `sop_machines`.