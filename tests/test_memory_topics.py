#!/usr/bin/env python3
"""The memory is one entry per SUBJECT, and a new fact replaces the old one.

What memory.md used to give the model was a log: `## Notes` and `## Recent`
queued one line per event, so a fact that changed three times appeared three
times and the model had to guess which line still held. The agent working on
the Orin, 2026-11-27, put it in one sentence: "è il punto in cui un modello
piccolo si convince di una cosa superata".

Pinned here:

  * a topic line carries its subject and a `verified:` date — "how much I trust
    it", which is what a reader needs, not "when it happened";
  * the same subject again REPLACES the line in place: one entry, the current
    truth, never two;
  * a different subject adds one;
  * the injected view is Profile + Topics, and `## Recent` is NEVER injected;
  * an agent with no Topics yet keeps its `## Notes` in the prompt, so the
    upgrade drops nothing — the first topic takes over from then on;
  * `memory_note` lands in the store, with the subject it is given or a derived
    one, never in the old log;
  * the section is capped: the oldest subject lines go, the newest stay.

Run:  server/.venv/bin/python tests/test_memory_topics.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-topics-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.storage import memory as mem_mod  # noqa: E402
from app.storage.memory import MemoryStore, topic_slug  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """label first, unlike the other scripts here: every call in this file
    reads as an assertion in English, and the detail only shows on failure."""
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"   [{detail}]" if not cond and detail else ""))
    if not cond:
        failures.append(label)


ROOT = Path(_TMP) / "memory"
store = MemoryStore(ROOT)
AGENT = "a"
md_path = ROOT / AGENT / "memory.md"


def md() -> str:
    return md_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- the store
print("1. a subject gets one line, with the date somebody verified it")
store.add_topic(AGENT, "salute/spermiogramma", "prenotare Andrologia 010 555 5846",
                verified="2026-11-27")
text = md()
check("## Topics exists", "## Topics" in text, text[:120])
check("the entry names its subject",
      "- salute/spermiogramma — prenotare Andrologia" in text, text[-200:])
check("and carries the verified date", "(verified: 2026-11-27)" in text)

print("2. the same subject REPLACES the previous fact")
store.add_topic(AGENT, "salute/spermiogramma",
                "prenotazione fatta per il 9 dicembre", verified="2026-11-28")
text = md()
check("one entry only", text.count("salute/spermiogramma") == 1,
      f"{text.count('salute/spermiogramma')} entries")
check("with the CURRENT truth", "prenotazione fatta per il 9 dicembre" in text)
check("the old sentence is gone from the prompt view",
      "010 555 5846" not in store.get_memory_md(AGENT))

print("3. a different subject is a second entry")
store.add_topic(AGENT, "lavoro/curriculum", "CV aggiornato al 2026-10")
entries = [l for l in md().splitlines()
           if l.startswith("- ") and "(verified: " in l]
check("two entries", len(entries) == 2, entries)

print("4. the injected view never contains ## Recent")
md_path.write_text(md().replace("## Profile",
                                "## Profile\nAlessandro, documenti sul NAS.", 1),
                   encoding="utf-8")
store.add_to_index(AGENT, "20260101000000", "una riga vecchia nel log")
view = store.get_memory_md(AGENT)
check("Topics are injected", "salute/spermiogramma" in view, view[:200])
check("Recent is NOT", "una riga vecchia" not in view and "## Recent" not in view, view)
check("Profile is injected", "## Profile" in view, view[:80])

print("5. an agent with no Topics keeps its Notes (the upgrade drops nothing)")
other = "b"
mdb = ROOT / other / "memory.md"
mdb.parent.mkdir(parents=True, exist_ok=True)
mdb.write_text("# Memory — b\n\n## Profile\nprofilo\n\n## Notes\n- 20260101000000 — nota storica\n",
               encoding="utf-8")
view_b = store.get_memory_md(other)
check("the legacy note stands in", "nota storica" in view_b, view_b)
store.add_topic(other, "primo/argomento", "fatto nuovo")
view_b = store.get_memory_md(other)
check("and steps aside once a topic exists", "nota storica" not in view_b, view_b)
check("which is injected instead", "primo/argomento" in view_b, view_b)

print("5b. an agent whose memory is Recent-only keeps it (last fallback)")
c = "c"
mdc = ROOT / c / "memory.md"
mdc.parent.mkdir(parents=True, exist_ok=True)
mdc.write_text("# Memory — c\n\n## Profile\np\n\n## Recent\n- 20260101000000 — solo recente\n",
               encoding="utf-8")
check("Recent stands in when nothing else is there",
      "solo recente" in store.get_memory_md(c), store.get_memory_md(c))

print("6. memory_note lands in the store, not in the log")
cid = store.add_note(AGENT, "il NAS si monta in ~/myagent/workspace/DISCONAS",
                     topic="nas/montaggio")
text = md()
check("the subject given by the model is used", "- nas/montaggio —" in text, text[-400:])
check("no ## Notes line was written for it",
      "- " + cid not in text, cid)
derived = store.add_note(AGENT, "la password del router scade a marzo")
check("without a topic one is derived", "password/router" in md().replace("/", "/") or
      "password" in md(), md()[-300:])

print("7. the cap keeps the newest subjects")
for i in range(mem_mod.TOPICS_CAP + 5):
    store.add_topic(AGENT, f"argomento{i}", f"fatto numero {i}")
text = md()
kept = [l for l in text.splitlines() if l.startswith("- ") and "(verified:" in l]
check(f"at most {mem_mod.TOPICS_CAP} entries", len(kept) <= mem_mod.TOPICS_CAP,
      f"{len(kept)} entries")
check("the newest is kept", any("fatto numero" in l and
                                str(mem_mod.TOPICS_CAP + 4) in l for l in kept),
      kept[:2])

print("8. the derived subject is a label, not the sentence")
check("short", len(topic_slug("x" * 400 + " " + "y" * 400)) <= 40,
      repr(topic_slug("x" * 400)))

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall ok")
