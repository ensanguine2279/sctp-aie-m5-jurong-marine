#!/usr/bin/env python3
"""Keyword search over the SOPs, shared by the loader and the agent.

Cut out of ingest/ingest_sops.py on 17 Nov 2026 (the ingestion split), unchanged, so the
agent's find_sop tool (scripts/agent_tools.py) no longer imports a loader: the API's image
holds scripts/ and common/, never ingest/. ingest_sops.py imports these names back, builds
sops.search_text with search_text(), and gives the FULLTEXT index the same stop words
(_STOP), so MySQL's MATCH ... AGAINST and search_sops() compare the same words.

How the search works, and why it is keyword and not a vector database:
docs/ingest_sops.md ('Search: keyword, not a vector database').
"""
from __future__ import annotations

import re

import pandas as pd


_STOP = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "is", "are", "what",
         "whats", "what's", "how", "do", "i", "we", "before", "after", "sop", "procedure", "with",
         "my", "our", "can", "should", "when", "it", "this", "that", "be", "by",
         # words every SOP uses: on their own they say nothing about the topic
         "work", "working", "machine", "machines", "all", "only", "use", "required", "before",
         "after", "every", "per", "person", "personnel"}
MIN_SCORE = 2.0   # one stray common word (1 point) is not a match

# Words people use in questions for what the SOPs call something else. expand_question()
# adds the SOP's word to the question, before search_sops and before MySQL MATCH ... AGAINST,
# so the two search the same words. A judgement, like ALIASES: keep it short, add an entry
# when a real question misses, and add that question to the tests.
SYNONYMS = {
    "sandblasting": "blasting chamber",      # SOP-CONFINED-03 'blasting chamber (BLAST-01)'
    "sandblast": "blasting chamber",
    "blast room": "blasting chamber",
    "blast booth": "blasting chamber",
    "lock out": "lockout",                   # SOP-LOTO-01 'LOCKOUT/TAGOUT'
    "lock-out": "lockout",
    "tag out": "tagout",
    "isolate": "lockout",
    "isolating": "lockout",
    "isolation": "lockout",
    "de-energise": "lockout",
    "de-energize": "lockout",
    "de-energising": "lockout",
    "de-energizing": "lockout",
    "blast chamber": "blasting chamber",     # 'going into the blast chamber' (5 Nov 2026)
}

# Going into a space is what SOP-CONFINED-03 calls "entry" ("CONFINED SPACE ENTRY"). A verb
# alone isn't enough: "enter a work order" or "what's going into the report" must not find
# the confined-space SOP, so expand_question adds "entry" only when the question also names
# a kind of space. On the real data (5 Nov 2026) the client's own question, "before entering
# the blasting chamber", scored exactly MIN_SCORE (2.0: 'entering' never matched 'entry'), and
# "going into the blast chamber" found nothing. A judgement, like SYNONYMS: add a word with
# its question as a test in test_sops.py.
ENTRY_VERBS = ("enter", "enters", "entering", "go in", "go into", "going into", "go inside",
               "going inside", "get into", "getting into", "get inside", "climb into",
               "climbing into", "step into", "stepping into", "work inside", "working inside")
SPACE_WORDS = ("chamber", "booth", "tank", "sump", "pit", "vessel", "hold", "space", "room",
               "enclosure", "duct", "silo", "hatch", "manhole")


def _has(low: str, phrase: str, plural: bool = False):
    """`phrase` as whole words in `low` (a trailing 's' allowed when plural)."""
    return re.search(rf"(?<![\w-]){re.escape(phrase)}{'s?' if plural else ''}(?![\w-])", low)


def expand_question(question: str) -> str:
    """The question plus the SOP words for any SYNONYMS phrase in it (whole words only:
    'isolated' is not 'isolate'), and 'entry' when it has a verb of going into a space AND
    names a space (ENTRY_VERBS, SPACE_WORDS). Pass the result to MATCH ... AGAINST too."""
    low = question.lower()
    extra = [w for phrase, w in SYNONYMS.items() if _has(low, phrase)]
    if any(_has(low, v) for v in ENTRY_VERBS) and \
            any(_has(low, w, plural=True) for w in SPACE_WORDS):
        extra.append("entry")
    return " ".join([question] + list(dict.fromkeys(extra)))


def _words(s: str) -> list[str]:
    """Lower-case words, stop words dropped, a plural 's' removed ('tanks' -> 'tank')."""
    out = []
    for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", s.lower()):
        if w in _STOP:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def search_text(text: str) -> str:
    """Text as search_sops reads it, for the sops.search_text column: lower-case words,
    stop words dropped, a plural 's' removed. MySQL FULLTEXT doesn't stem ('tank' and
    'tanks' are different words to it), so MATCH searches this column with search_terms()
    and both searches compare the same words."""
    return " ".join(_words(text))


def search_terms(question: str) -> str:
    """The question as search_sops reads it (synonyms added, then search_text's rules):
    pass this to MATCH (search_text) AGAINST (...)."""
    return " ".join(dict.fromkeys(_words(expand_question(question))))


def search_sops(question: str, sops: pd.DataFrame, links: pd.DataFrame | None = None,
                limit: int = 3) -> list[tuple[str, int, float]]:
    """(doc_no, revision, score) of current SOPs, best first; [] if nothing scores at least
    MIN_SCORE, so the app can say 'no SOP in the data covers this' (trap list D3). This is
    the gate for that answer. The question is first widened by expand_question. Title
    words count double; a machine the SOP is linked to (id or evidence phrase in the
    question) counts 3. In the database, MATCH (search_text) AGAINST (search_terms(question))
    on sops compares the same words and finds the same candidates."""
    question = expand_question(question)
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