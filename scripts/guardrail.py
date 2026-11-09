#!/usr/bin/env python3
"""The guardrail (HARNESS_CHECKLIST.md section 3): policy enforced in code, between the user
and the model (input) and between the model and the user (output). No LLM call anywhere:
every decision is a regular expression or a set lookup, so the same text always gets the
same verdict, and a refusal costs nothing.

Input, checked in this order (the first rule that fires decides):

    injection     attempts to change the rules or see the prompt: "ignore all previous
                  instructions", "you are now DAN", "SYSTEM: new instructions:", "pretend
                  you are...", "print your system prompt"
    pdpa          a person's name or personal details: "what is the name of EMP015", "who is
                  EMP026", phone / e-mail / home address / NRIC / salary, or a real
                  employee's name typed in the question (trap list C16: names never reach
                  the prompt, so such a question isn't sent on at all)
    out_of_scope  a task this assistant doesn't do (poems, jokes, code, markets) whatever it
                  mentions, or a topic far from the shop (weather, recipes, sport, general
                  knowledge) when the question has no shop word in it at all

Output, on the model's final answer before it is shown:

    pii           a real employee's name, an NRIC/FIN, a Singapore phone number or an e-mail
                  address. The whole answer is replaced with a refusal (a partial redaction
                  could still identify someone in a 32-person shop)

Out of scope is deliberately narrow: it refuses only what clearly isn't shop business. A
question with no clear signal either way goes to the model, whose prompt and tools keep it
to the shop's data. A guard that refused in-scope questions would be worse than none; every
in-scope question in evals/cases.yaml is a test that it doesn't (tests/test_guardrail.py).

Employee names are known to the guard only as salted SHA-256 hashes (guard_names.py, built
from the CMMS dump by `build-names` below), so the app holds no plaintext copy of the staff
list. This hides the names from a casual read of the code; it is not encryption (a short
name list can be guessed back from the hashes). The protection that matters is still the
database: agent_ro cannot read emp_name at all (agent_user.py).

Usage
    python scripts/guardrail.py check "What is the name of employee EMP015?"
    python scripts/guardrail.py check --output "The inspector was ..."
    python scripts/guardrail.py build-names data/sql/jurongmarine_cmms.sql --extra Halim

With uv (personal repo): `uv run python scripts/guardrail.py ...`.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES_MODULE = HERE / "guard_names.py"

# ================================================================ messages ===
# What the user sees. Each rule has its own wording, so a refusal says WHY (8 Nov 2026: a
# PDPA question got "I could not check that", which reads like an outage). No numbers or
# ids in these: the eval suite's grounding check would (rightly) flag any.

SCOPE = ("JME's machines, maintenance and work orders, critical spares, sensor readings, "
         "safety incidents and SOPs")
MESSAGES = {
    "injection": ("I can't follow instructions that try to change how I work or what I can "
                  f"see. I can help with {SCOPE}, from the shop's own data."),
    "pdpa": ("I can't look up or give out personal details about people, such as names, "
             "contact details or ID numbers: staff data is protected under PDPA, and I only "
             "work with employee codes. Please ask again without naming anyone. I can help "
             f"with {SCOPE}."),
    "out_of_scope": (f"That's outside what I can help with. I answer questions about {SCOPE}, "
                     "from the shop's own data."),
    "pii": ("I can't show that answer: it would have included personal data about a member of "
            "staff, which is protected under PDPA. Please ask about the record by its incident "
            "or work order number, or by employee code."),
}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: str | None = None      # injection | pdpa | out_of_scope | pii
    reason: str | None = None    # which family of pattern fired; never the user's text
    message: str | None = None   # what the user sees instead

    @classmethod
    def ok(cls) -> "Verdict":
        return cls(True)

    @classmethod
    def refuse(cls, rule: str, reason: str) -> "Verdict":
        return cls(False, rule, reason, MESSAGES[rule])


def _rx(*patterns: str, flags=re.I) -> list[re.Pattern]:
    return [re.compile(p, flags) for p in patterns]


def _norm(text: str) -> str:
    """NFKC (full-width letters, odd spaces), curly quotes straightened, spaces collapsed."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[ \t ]+", " ", t).strip()


# =============================================================== injection ===

_RULE_WORDS = (r"(instructions?|rules?|prompts?|guidelines?|guardrails?|polic(y|ies)|"
               r"restrictions?|safeguards?|programming|filters?)")
INJECTION = {
    # "ignore all previous instructions", "disregard your guardrails". The object must be
    # the assistant's rules: "ignore the previous work order" is a maintenance question.
    "override_rules": _rx(
        r"\b(ignore|disregard|forget|override|bypass|circumvent)\b[^.?!\n]{0,30}?"
        r"\b(your|previous|prior|above|earlier|all|any|system|these|those|the\s+(above|previous|prior))"
        r"\s+(\w+\s+){0,2}" + _RULE_WORDS + r"\b",
        # "forget the rules above": the rules the assistant was given, not a shop rule
        # ("can I ignore the rules on tandem lifts?" is a safety question, and allowed)
        r"\b(ignore|disregard|forget|override|bypass)\s+(the\s+|all\s+(the\s+)?)?"
        + _RULE_WORDS + r"\s+(above|before|so\s+far|you\s+(were|have\s+been)\s+given|i\s+gave\s+you)\b"),
    "prompt_extraction": _rx(
        r"\b(system|developer|hidden|initial|secret)\s+(prompt|instructions?|message)\b",
        r"\b(reveal|print|show|repeat|output|display|dump|leak|tell me)\b[^.?!\n]{0,30}"
        r"\b(your|the)\s+(\w+\s+)?(prompt|instructions|configuration|guardrails?)\b"),
    "persona": _rx(
        r"\byou\s+are\s+now\b", r"\bfrom\s+now\s+on,?\s+(you|answer|respond|act)\b",
        r"\bpretend\b", r"\brole[\s-]?play\b", r"\bjailbreak", r"\bdeveloper\s+mode\b",
        r"\bdo\s+anything\s+now\b", r"\b(admin|god|sudo|root|unrestricted)\s+mode\b",
        r"\b(you|yourself)\b[^.?!\n]{0,20}\b(act|behave|respond)\s+as\b") +
        _rx(r"\bDAN\b", flags=0),                     # the jailbreak persona, in capitals only
    "fake_system_message": _rx(
        r"\b(new|updated|real|actual|override)\s+(instructions?|rules|task|system\s+prompt)\s*:",
        r"(^|\n)\s*(system|assistant|developer)\s*:",
        r"</?\s*(system|assistant|instructions?)\s*>", r"\[/?(system|inst)\]", r"<\|im_(start|end)\|>"),
    "no_rules": _rx(r"\b(no|without(\s+any)?)\s+(rules|restrictions|limits|filters|guardrails)\b"),
}


# ==================================================================== PDPA ===

_ROLE = (r"(employees?|staff|workers?|technicians?|techs?|operators?|inspectors?|fitters?|"
         r"electricians?|machinists?|storem[ae]n|store\s?keepers?|officers?|managers?|"
         r"supervisors?|engineers?|person|people|persons|colleagues?|crew|team\s+members?|"
         r"\bgm\b|who|whom|whose|EMP\d{3})")
# "name" of a THING is fine: the machine's name, an SOP's name, a part's name.
_THING = r"(machines?|parts?|files?|sops?|procedures?|models?|products?|brands?|oils?|tools?|tables?|columns?|fields?|documents?|forms?|spares?)"
_NAME_WORD = re.compile(r"\b(names?|named|surnames?|full\s+name|first\s+name|last\s+name)\b", re.I)
PDPA = {
    "names_of_people": None,                                   # _asks_for_a_persons_name()
    "who_is_code": _rx(r"\bwho('s|\s+is|\s+was)\s+(employee\s+)?EMP\d{3}\b",
                       r"\bEMP\d{3}('s)?\s+(name|identity|details)\b",
                       r"\bidentify\b[^.?!\n]{0,20}\b(the\s+)?(person|employee|worker|staff|EMP\d{3})\b"),
    "contact_or_id": _rx(
        r"\b(phone|mobile|handphone|cell|telephone|contact)\s*(number|no\.?|#|details?|info)\b",
        r"\be-?mail\s+(address|of|for)\b", r"\b(home|residential|mailing|house)\s+address\b",
        r"\bnric\b", r"\bfin\s+(number|no)\b", r"\bpassport\b", r"\bdate\s+of\s+birth\b",
        r"\bbirthday\b", r"\bsalar(y|ies)\b", r"\bpay\s?slips?\b", r"\bnext\s+of\s+kin\b",
        r"\bpersonal\s+(details?|data|info(rmation)?|particulars)\b",
        r"\bmedical\s+(history|records?)\b"),
}


def _asks_for_a_persons_name(t: str) -> bool:
    """'name' (or names, surname, full name) in the same message as a word for a person or
    an employee code, and not as the name of a thing ('the machine name', 'name of the SOP')."""
    if not re.search(_ROLE, t, re.I):
        return False
    for m in _NAME_WORD.finditer(t):
        before, after = t[max(0, m.start() - 20):m.start()], t[m.end():m.end() + 25]
        if re.search(_THING + r"\s*$", before, re.I):
            continue                                            # "machine name"
        if re.match(r"\s+of\s+(the\s+|that\s+|this\s+|each\s+)?" + _THING + r"\b", after, re.I):
            continue                                            # "name of the SOP"
        return True
    return False


# ============================================================= out of scope ===

# A task this assistant never does, whatever the topic: refused even with shop words in it.
OFF_TASK = {
    "creative_writing": _rx(
        r"\b(write|compose|make\s+up|tell|create|give|generate)\b[^.?!\n]{0,40}"
        r"\b(poems?|poetry|haikus?|sonnets?|songs?|lyrics|raps?|jokes?|story|stories|limericks?|"
        r"essays?|fan\s?fiction)\b"),
    "write_code": _rx(
        r"\b(write|generate|code|create|build|give)\b[^.?!\n]{0,40}"
        r"\b(python|javascript|typescript|java|c\+\+|c#|html|css|bash|powershell)\b",
        r"\b(scrape|scraper|scraping)\b"),
    "markets": _rx(r"\b(stock|share)\s+price\b", r"\bbitcoin\b", r"\bcrypto\w*\b", r"\bforex\b",
                   r"\b(buy|sell)\s+(shares|stocks)\b", r"\bdividends?\b", r"\blottery\b"),
}
# Far from the shop: refused only when the question has no shop word at all.
OFF_TOPIC = _rx(
    r"\bweather\b", r"\bforecast\b", r"\b(raining|rainy|umbrella)\b",
    r"\brecipes?\b", r"\b(cook|bake|baking)\b", r"\bchicken rice\b", r"\b(dinner|lunch|breakfast)\b",
    r"\brestaurants?\b", r"\bhawker\b",
    r"\b(football|soccer|premier league|world cup|nba|olympics|formula (one|1)|f1)\b",
    r"\bwho won\b", r"\b(elections?|prime minister|president|parliament|politics?|political)\b",
    r"\bcapital of\b", r"\bpopulation of\b", r"\bwho invented\b", r"\bmeaning of life\b",
    r"\b(movies?|films?|netflix|celebrit(y|ies)|tv shows?|k-?drama|music|album)\b",
    r"\b(flights?|hotels?|holiday|vacation|itinerary|visa)\b",
    r"\b(homework|equation|integral|derivative)\b",
    r"\b(girlfriend|boyfriend|dating|relationship advice)\b",
    r"\b(translate|translation)\b",
)
# Words that make a question shop business. Only used to let an OFF_TOPIC word through
# ("does rain get into the blasting chamber?"), never to refuse.
DOMAIN = _rx(
    r"\b[A-Z]{2,6}-?[A-Z]?\d{1,3}\b",                                  # CNC-02, PMP-T01, CRN 1
    r"\b(machines?|lathes?|mill(ing)?|cnc|weld\w*|cranes?|hoists?|slings?|lift(s|ing)?|"
    r"compressors?|pumps?|blast\w*|chambers?|sumps?|nameplates?|plates?)\b",
    r"\b(oils?|grease|lubric\w*|hydraulic|coolant|bearings?|belts?|seals?|spindles?|chucks?|"
    r"gearbox|vfd|motors?|guards?|interval)\b",
    r"\b(sops?|procedures?|lockout|loto|tagout|permits?|confined|gas\s+test|ppe|attendant|"
    r"safety|incidents?|accidents?|injur\w*|near[\s-]miss|severity|scale|lost\s+days|wsh|mom|audit\w*)\b",
    r"\b(work\s+orders?|wo|maintenance|pm|preventive|corrective|breakdowns?|repairs?|faults?|"
    r"spares?|parts?|stock|inventory|minimum|lead\s+time|storem[ae]n|sensors?|vibration|"
    r"temperature|readings?|trend|shift|cmms|technicians?|downtime|jme|jurong|workshop|shop)\b",
    r"\b(mazak|dmg|bridgeport|lincoln|miller|konecranes|demag|atlas|copco|ksb|colchester|tonna|"
    r"vg\s?\d+|capacity|load|weight|kg|serial)\b",
)


def _first(rules: dict, t: str) -> str | None:
    for reason, patterns in rules.items():
        if patterns and any(p.search(t) for p in patterns):
            return reason
    return None


# ================================================================== names ===

_NAMES_CACHE: tuple[str, frozenset] | None = None
MAX_NAME_WORDS = 5          # longest phrase hashed (the longest staff name has four words)
MIN_SINGLE_WORD = 6         # a lone word is a name only if this long (Vikram, Lakshmi):
                            # shorter ones (Ng, Ho, Low, Lim, Ang) are ordinary English words
                            # or too common to refuse on


def _words(text: str) -> list[str]:
    """Lower-case words, apostrophes dropped: "Marcus D'Silva" -> ['marcus', 'dsilva']."""
    return re.findall(r"[a-z]+", re.sub(r"['’]", "", _norm(text).lower()))


def _hash(pepper: str, phrase: str) -> str:
    return hashlib.sha256(f"{pepper}|{phrase}".encode()).hexdigest()[:16]


def name_phrases(name: str) -> set[str]:
    """What counts as 'this person's name': every run of two or more words of it, and any
    single word of MIN_SINGLE_WORD letters or more. 'Aminah binte Yusof' ->
    'aminah binte', 'binte yusof', 'aminah binte yusof', 'aminah'."""
    w = _words(name)
    out = {" ".join(w[i:j]) for i in range(len(w)) for j in range(i + 2, len(w) + 1)}
    out |= {x for x in w if len(x) >= MIN_SINGLE_WORD}
    return out


def _load_names() -> tuple[str, frozenset]:
    global _NAMES_CACHE
    if _NAMES_CACHE is None:
        try:
            import guard_names
            _NAMES_CACHE = (guard_names.PEPPER, frozenset(guard_names.HASHES))
        except Exception:                      # missing, broken or half-written: no names.
            _NAMES_CACHE = ("", frozenset())   # The app then refuses to run (require_names)
    return _NAMES_CACHE


def names_loaded() -> int:
    """How many name phrases the guard knows (0 = guard_names.py is missing or empty)."""
    return len(_load_names()[1])


class GuardNotReady(RuntimeError):
    """The staff name list isn't loaded, so names in questions and answers can't be caught."""


NOT_READY = ("The guardrail's staff name list (scripts/guard_names.py) is missing or empty, so "
             "names in questions and answers can't be caught. Build it with: python "
             "scripts/guardrail.py build-names data/sql/jurongmarine_cmms.sql --extra <name> "
             "(docs/guardrail.md), and ship it with the app.")
# What a user sees if a question arrives while the list is missing (ask() fails closed).
MESSAGES["not_ready"] = ("I can't answer questions right now: a safety check this assistant "
                         "depends on isn't set up. Please tell whoever runs it.")


def require_names() -> int:
    """Fail closed: the number of name phrases, or GuardNotReady. Called when the app, the
    page and the eval runner start, so a deploy without guard_names.py stops at once with a
    clear message instead of quietly running without its name checks."""
    n = names_loaded()
    if not n:
        raise GuardNotReady(NOT_READY)
    return n


def contains_name(text: str) -> bool:
    """True if any known staff name phrase is in the text (whole words, any case)."""
    pepper, hashes = _load_names()
    if not hashes:
        return False
    w = _words(text)
    # Every run of 1-5 words is looked up. Which phrases count was decided when the list
    # was built (name_phrases, plus --extra names whole), so a short word like "low" only
    # matches if it was put on the list on purpose.
    return any(_hash(pepper, " ".join(w[i:i + n])) in hashes
               for n in range(1, MAX_NAME_WORDS + 1) for i in range(len(w) - n + 1))


# =================================================================== checks ===

def check_input(text: str) -> Verdict:
    """The verdict on a user's message, before anything is sent to the model."""
    t = _norm(text)
    if reason := _first(INJECTION, t):
        return Verdict.refuse("injection", reason)
    if _asks_for_a_persons_name(t):
        return Verdict.refuse("pdpa", "names_of_people")
    if reason := _first(PDPA, t):
        return Verdict.refuse("pdpa", reason)
    if contains_name(t):
        return Verdict.refuse("pdpa", "staff_name_in_question")
    if reason := _first(OFF_TASK, t):
        return Verdict.refuse("out_of_scope", reason)
    if any(p.search(t) for p in OFF_TOPIC) and not any(p.search(t) for p in DOMAIN):
        return Verdict.refuse("out_of_scope", "off_topic")
    return Verdict.ok()


NRIC = re.compile(r"\b[STFGM]\d{7}[A-Z]\b")                      # NRIC / FIN
# 8 digits starting 6, 8 or 9, optionally +65, as 1234 5678 / 1234-5678 / 12345678. Not
# part of a longer number (an epoch, a cost like 91234567.50); a full stop after is fine.
SG_PHONE = re.compile(r"(?<!\d)(?<!\d[.,])(\+65[\s-]?)?[689]\d{3}[\s-]?\d{4}(?!\d|[.,]\d)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b")


def check_output(text: str) -> Verdict:
    """The verdict on the model's answer, before the user sees it."""
    t = _norm(text)
    if contains_name(t):
        return Verdict.refuse("pii", "staff_name")
    if NRIC.search(t):
        return Verdict.refuse("pii", "nric")
    if SG_PHONE.search(t):
        return Verdict.refuse("pii", "phone")
    if EMAIL.search(t):
        return Verdict.refuse("pii", "email")
    return Verdict.ok()


# ============================================================ build-names ===

_EMP_INSERT = re.compile(r"INSERT INTO `employees`[^;]*?VALUES(.*?\));", re.S)
_EMP_ROW = re.compile(r"\(\s*\d+\s*,\s*'(EMP\d+)'\s*,\s*'((?:[^']|'')*)'")


def employee_names(dump_text: str) -> list[str]:
    """The distinct names in the dump's employees INSERT ('' is an escaped quote)."""
    names = []
    for block in _EMP_INSERT.findall(dump_text):
        names += [n.replace("''", "'") for _, n in _EMP_ROW.findall(block)]
    return sorted(set(names))


def build_hashes(names: list[str], extra: list[str], pepper: str) -> list[str]:
    phrases = set()
    for n in names:
        phrases |= name_phrases(n)
    for x in extra:                     # added whole, whatever their length (Halim)
        phrases.add(" ".join(_words(x)))
        phrases |= name_phrases(x)
    return sorted({_hash(pepper, p) for p in phrases if p})


def write_names_module(dump: Path, extra: list[str], out: Path = NAMES_MODULE,
                       pepper: str = "jme-guard-v1") -> int:
    text = dump.read_text(encoding="utf-8", errors="replace")
    names = employee_names(text)
    if not names:
        raise SystemExit(f"No employees INSERT found in {dump}.")
    hashes = build_hashes(names, extra, pepper)
    block = "".join(_EMP_INSERT.findall(text))
    sha = hashlib.sha256(block.encode("utf-8", "replace")).hexdigest()
    body = ",\n".join(f'    "{h}"' for h in hashes)
    out.write_text(
        '"""Employee name phrases for guardrail.py, as salted SHA-256 hashes: GENERATED, do not\n'
        "edit. Rebuild after the staff list changes, with the same --extra names as before\n"
        "(docs/guardrail.md says which; they aren't repeated here):\n"
        f"    python scripts/guardrail.py build-names {dump.as_posix()} --extra ...\n\n"
        "No plaintext names here (PDPA, trap list C16). Not encryption: a short list of\n"
        'common names can be guessed back from these. agent_ro still cannot read emp_name."""\n\n'
        f'PEPPER = "{pepper}"\n'
        f"EMPLOYEES_SHA256 = \"{sha}\"   # the dump's employees INSERT it was built from\n"
        f"PEOPLE = {len(names)}               # distinct names in the dump\n"
        f"EXTRA = {len(extra)}                 # names added with --extra\n"
        f"HASHES = frozenset({{\n{body},\n}})\n", encoding="utf-8")
    return len(hashes)


# ===================================================================== CLI ===

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="the guard's verdict on one text (no model, no database)")
    c.add_argument("text")
    c.add_argument("--output", action="store_true", help="check it as an answer, not a question")
    b = sub.add_parser("build-names", help="write scripts/guard_names.py from the CMMS dump")
    b.add_argument("dump", type=Path)
    b.add_argument("--extra", action="append", default=[],
                   help="another name to block (e.g. the storeman in the spares sheet title)")
    a = ap.parse_args(argv)
    if a.cmd == "build-names":
        n = write_names_module(a.dump, a.extra)
        print(f"Wrote {NAMES_MODULE.name}: {n} name phrases (hashed, no plaintext).")
        return 0
    v = check_output(a.text) if a.output else check_input(a.text)
    print("allowed" if v.allowed else f"refused: {v.rule} ({v.reason})\n{v.message}")
    if not names_loaded():
        print("(no guard_names.py: names aren't checked)", file=sys.stderr)
    return 0 if v.allowed else 1


if __name__ == "__main__":
    sys.exit(main())