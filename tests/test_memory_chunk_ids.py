#!/usr/bin/env python3
"""Memory chunk ids: new ones carry a full year, old ones keep working.

Ids were YYMMDDHHMMSS. In memory.md's index — a file a human is meant to skim,
and which is injected into the agent's prompt whole — "260810190008" is
indistinguishable from a random number, so new ids are YYYYMMDDHHMMSS.

The old ids cannot be migrated: they are chunk FILENAMES on disk and they are
already written into every existing memory.md index line, which is user-editable
prose nothing rewrites. So both widths are legal forever, and that is exactly
what rots quietly. Two things this guards:

  1. A legacy 12-digit id still validates, still reads back, and still parses out
     of an index line — otherwise `memory_read` on an existing note 404s.
  2. Recency ordering survives the mix. Plain filename sort does NOT work once
     the widths coexist ("20260810..." < "260810..." lexicographically), so a
     brand-new chunk would look like the oldest one to _iter_chunks — which
     feeds find_chunk_by_hash and the compactor. This test asserts both the fix
     and the premise, so it fails loudly if either changes.

Run: python3 tests/test_memory_chunk_ids.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

FAILURES: list[str] = []
LEGACY = "260810190008"          # what the store wrote before 2026-08-10
LEGACY_CHUNK = (
    "---\nid: %s\nkind: note\ndate: 2026-08-10T19:00:08\nsession: \n"
    "channel: \nsource: \nhash: \nkeywords: spesa\n---\nvecchia nota\n" % LEGACY
)


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MYAGENT_MEMORY"] = os.path.join(tmp, "memory")
        from app.storage.memory import (MemoryStore, _INDEX_LINE,
                                        _VALID_CHUNK_ID, _chunk_sort_key)

        root = Path(os.environ["MYAGENT_MEMORY"])
        store = MemoryStore(root)
        agent = "tester"

        print("1. a new id is a full-year timestamp")
        new = store.write_chunk(agent, body="nota nuova", kind="note")
        check("14 digits", len(new) == 14, new)
        check("starts with the century", new.startswith("20"), new)
        check("validates", bool(_VALID_CHUNK_ID.match(new)), new)

        print("2. a legacy 12-digit id keeps working")
        check("validates", bool(_VALID_CHUNK_ID.match(LEGACY)))
        chunks = root / agent / "chunks"
        chunks.mkdir(parents=True, exist_ok=True)
        (chunks / f"{LEGACY}.md").write_text(LEGACY_CHUNK, encoding="utf-8")
        got = store.read_chunk(agent, LEGACY)
        check("reads back", bool(got) and got["body"] == "vecchia nota", str(got))
        check("index line parses",
              (_INDEX_LINE.match(f"- {LEGACY} — vecchia") or [None, None])[1] == LEGACY)

        print("3. recency ordering survives the mixed widths")
        ids = [c for c, _m, _b in store._iter_chunks(agent)]
        check("newest chunk comes first", ids and ids[0] == new, str(ids))
        check("oldest last", ids and ids[-1] == LEGACY, str(ids))
        # The premise: if this ever passes, the normalization is dead weight and
        # this whole test should be revisited rather than silently kept.
        check("plain filename sort really is wrong (premise still holds)",
              sorted(ids, reverse=True)[0] == LEGACY, str(sorted(ids, reverse=True)))

        print("4. a collision suffix sorts as a number, not as text")
        check("-2 before -10",
              _chunk_sort_key(f"{new}-2") < _chunk_sort_key(f"{new}-10"))
        check("suffixed id validates", bool(_VALID_CHUNK_ID.match(f"{new}-2")))
        check("suffixed index line parses",
              (_INDEX_LINE.match(f"- {new}-2 — x") or [None, None])[1] == f"{new}-2")

        print("5. both widths coexist in the index and in search")
        store.add_to_index(agent, LEGACY, "vecchia nota", kind="note")
        store.add_to_index(agent, new, "nota nuova", kind="note")
        md = (root / agent / "memory.md").read_text(encoding="utf-8")
        check("legacy line kept", LEGACY in md)
        check("new line added", new in md)
        hits = [r["id"] for r in store.search(agent, "nota")]
        check("search returns both", new in hits and LEGACY in hits, str(hits))

        print("6. a malformed id is still refused")
        for bad in ("2608", "abcdefghijkl", "../etc/passwd", "2026081019000812345"):
            check(f"rejects {bad!r}", not _VALID_CHUNK_ID.match(bad))

    if FAILURES:
        print("\nFAILED:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("\nOK — new ids carry a full year, legacy ids still resolve, and "
          "recency ordering holds across both widths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
