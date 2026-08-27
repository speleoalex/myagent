#!/usr/bin/env python3
"""Which query terms count, and how high the bar is — search_pdf_file.

Run: server/.venv/bin/python tests/test_query_terms.py

Two rules that used to be one, and the one they replaced caused a real failure.
Asked "come sono collegati i tubi dell'egr nell'l300?" over a folder of vehicle
manuals, the agent reported the folder did not contain the answer. It did: 686
occurrences of EGR as a word across 34 pages. Two independent reasons:

  1. `egr` is three characters, and the scorer dropped every term shorter than
     four. The comment justifying that said the dropped terms are function
     words — 'si', 'la', 'the', 'of' — which is true of a natural question and
     false of a technical corpus, where the SHORTEST token is usually the
     subject. Length was a proxy for the wrong property; STOPWORDS is the
     property itself.
  2. The bar was "half the terms asked". An Italian question over English
     manuals shares only the acronym, so two-of-four was unreachable no matter
     how well a page answered. The bar is now half of what THIS FILE can match.

Neither may be loosened into a weak fallback: a folder of manuals always has
some page mentioning some word, and offering it evicts a real answer from
another source. So the bar stays a bar — it is the denominator that changed.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server" / "tools" / "library" / "local_search"))

import search                                              # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# --- 1. what survives the term filter ------------------------------------
def kept(q):
    terms, _ = search.parse_query(q)
    return [t for t in terms if t not in search.STOPWORDS] or terms


k = kept("come sono collegati i tubi dell'egr nell'l300?")
check("an acronym survives the term filter", "egr" in k)
check("the model name survives too", "l300" in k)
check("Italian function words do not", not {"come", "sono", "dell", "nell"} & set(k))

k2 = kept("come si cura la rosolia?")
check("the query that motivated dropping terms still drops them",
      "si" not in k2 and "la" not in k2)
check("...while keeping what it is about", {"cura", "rosolia"} <= set(k2))

k3 = kept("how is the EGR valve connected to the intake")
check("English function words are dropped as well",
      not {"how", "is", "the", "to"} & set(k3))
check("...and the acronym is kept whatever its case", "egr" in k3)

# No acronym may ever end up in the list — that is the bug this file exists for.
for a in ("egr", "abs", "tdc", "ecu", "rpm", "pto", "dpf", "4wd", "l300", "obd"):
    check(f"'{a}' must never be a stopword", a not in search.STOPWORDS)

# The list is only useful if it is actually shorter than "every short word":
# `LONG_TERM` dropped 3-character tokens wholesale, which is what broke.
short_kept = [t for t in kept("egr abs tdc rpm") if len(t) < search.LONG_TERM]
check("short content words are no longer dropped for being short",
      len(short_kept) == 4)

# --- 2. the bar is relative to what the file can match -------------------
# A synthetic stand-in for the real case: a document in one language, a question
# in another, sharing exactly one term.
PAGES = [
    "EMISSION CONTROL - Service Adjustment Procedures. EXHAUST GAS "
    "RECIRCULATION (EGR) SYSTEM. Check the EGR valve and the EGR solenoid.",
    "CLUTCH - Disassembly. Release cylinder, push rod, conical spring.",
]
terms, phrase = search.parse_query("come sono collegati i tubi dell'egr nell'l300?")
long_terms = [t for t in terms if t not in search.STOPWORDS] or terms
pats = search.word_patterns(long_terms)
lowered = [p.lower() for p in PAGES]
matchable = sum(1 for _t, pat in pats if any(pat.search(low) for low in lowered))
check("only one of the four terms is matchable in this document", matchable == 1)

fixed_bar = max(1, (len(long_terms) + 1) // 2)
calib_bar = max(1, (matchable + 1) // 2)
check("the old bar was unreachable here", fixed_bar > matchable)
check("the calibrated bar is reachable", calib_bar <= matchable)

s, present = search.score_words(PAGES[0], pats, phrase)
check("the EGR page qualifies under the calibrated bar",
      s > 0 and present >= calib_bar)
s2, present2 = search.score_words(PAGES[1], pats, phrase)
check("a page about something else still does not",
      not (s2 > 0 and present2 >= calib_bar))

# And the bar must remain a BAR: with several terms matchable, one is not enough.
terms3, phrase3 = search.parse_query("clutch release cylinder push rod spring")
lt3 = [t for t in terms3 if t not in search.STOPWORDS] or terms3
pats3 = search.word_patterns(lt3)
m3 = sum(1 for _t, pat in pats3 if any(pat.search(low) for low in lowered))
bar3 = max(1, (m3 + 1) // 2)
check("with many matchable terms the bar is still more than one",
      m3 >= 4 and bar3 >= 2)
s4, present4 = search.score_words(PAGES[0], pats3, phrase3)
check("a page matching one term out of many matchable is still refused",
      not (s4 > 0 and present4 >= bar3))

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — function words are dropped and acronyms are not, and the bar is "
      "half of what the document can match rather than half of what was asked")
