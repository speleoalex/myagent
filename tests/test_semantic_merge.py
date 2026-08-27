#!/usr/bin/env python3
"""How semantic hits join the keyword results — search.py's merge half.

Run: server/.venv/bin/python tests/test_semantic_merge.py

The design decision under test: the semantic side is ONE MORE BUCKET in the
existing round_robin, not a reciprocal-rank fusion. The keyword scorer is
heavily tuned (IDF weights, rare-term anchors, proximity, leave-one-out
relaxation) and RRF would flatten all of it into a position. The price of the
cheaper design is that the two sides can offer the SAME passage twice, which
dedup_key cannot catch on its own — hence drop_overlaps, which is most of what
this file checks.

Cases:
  1. no embedder -> no semantic bucket, no note, and nothing raises. This is
     the invariant that keeps `local_search` exactly as it was;
  2. a PDF page found BOTH ways appears ONCE (measured: it took the first two
     of five slots before this);
  3. a semantic chunk that CONTAINS a keyword hit's line is dropped — the
     chunk aggregates paragraphs, so its id differs while the passage does not;
  4. a semantic hit from an untouched region survives;
  5. the semantic bucket sits AFTER the user's own documents, before the ZIM
     archives (ownership first — the rule text_bucket already encodes);
  6. a never-indexed root leaves a request file instead of indexing inline: the
     tool has 30s and a folder of manuals takes minutes.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "server" / "tools" / "library" / "local_search"
sys.path.insert(0, str(TOOL))

import search                                             # noqa: E402
import semindex                                           # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


# 2 + 3 + 4. drop_overlaps
kw = [
    {"id": "p:manual.pdf:4", "title": "manual.pdf p.4", "dedup": "hash-of-page"},
    {"id": "f:note.md:5", "title": "Frizione"},
]
sem = [
    # same PDF page, reached the other way
    {"id": "p:manual.pdf:4", "rel": "manual.pdf", "line_from": 4, "line_to": 4},
    # an aggregate chunk spanning lines 3-7, which contains the keyword hit at 5
    {"id": "f:note.md:3", "rel": "note.md", "line_from": 3, "line_to": 7},
    # untouched region of the same file
    {"id": "f:note.md:20", "rel": "note.md", "line_from": 20, "line_to": 24},
    # a duplicate inside the semantic bucket itself
    {"id": "f:note.md:20", "rel": "note.md", "line_from": 20, "line_to": 24},
]
kept = [h["id"] for h in search.drop_overlaps(sem, kw)]
check("a PDF page found both ways is offered once", "p:manual.pdf:4" not in kept)
check("a semantic chunk containing a keyword hit is dropped",
      "f:note.md:3" not in kept)
check("a semantic hit from an untouched region survives",
      "f:note.md:20" in kept)
check("the semantic bucket does not repeat itself", kept.count("f:note.md:20") == 1)
check("nothing else is invented", set(kept) == {"f:note.md:20"})

# dedup_key alone must NOT be expected to do this job — the comment in
# drop_overlaps says why, and this pins the reason so it stays true.
check("dedup_key really does give the two sides different keys",
      search.dedup_key(kw[0]) != search.dedup_key(
          {"id": "p:manual.pdf:4", "title": "manual.pdf p.4"}))

with tempfile.TemporaryDirectory() as tmp:
    cache = Path(tmp) / "cache"
    os.environ["MYAGENT_CACHE"] = str(cache)
    root = Path(tmp) / "docs"
    root.mkdir()
    (root / "n.md").write_text("# Frizione\n\nLa coppia e' 15-22 Nm.\n")

    # 1. no embedder configured -> the semantic side is simply absent.
    for var in ("MYAGENT_EMBED_URL", "MYAGENT_EMBED_MODEL"):
        os.environ.pop(var, None)
    terms, phrase = search.parse_query("frizione")
    hits, note = search.semantic_bucket(str(root), "frizione", terms, phrase, 5)
    check("no embedder -> no semantic hits", hits == [])
    check("no embedder -> no note (nothing to tell the user about)", note == "")
    check("no embedder -> no index request is queued",
          not list((cache / "index").glob("*.request")) if (cache / "index").exists() else True)

    # 6. embedder configured but nothing indexed yet: answer now, queue the work.
    os.environ["MYAGENT_EMBED_URL"] = "http://127.0.0.1:9/v1/embeddings"
    os.environ["MYAGENT_EMBED_MODEL"] = "nobody-home"
    hits, note = search.semantic_bucket(str(root), "frizione", terms, phrase, 5)
    check("an unindexed root still returns no semantic hits (it answers now)",
          hits == [])
    reqs = list((cache / "index").glob("*.request"))
    check("an unindexed root queues ONE index request", len(reqs) == 1)
    body = json.loads(reqs[0].read_text())
    check("the request names the root", body["root"] == os.path.realpath(str(root)))
    check("the request carries the OCR flag", body["ocr"] is False)

    # asking again must not churn the queue
    search.semantic_bucket(str(root), "frizione", terms, phrase, 5)
    check("a second search does not queue a second request",
          len(list((cache / "index").glob("*.request"))) == 1)

    # a paused root is NOT restarted behind the user's back
    reqs[0].unlink()
    Path(semindex.paused_path_for(str(root))).write_text("")
    search.semantic_bucket(str(root), "frizione", terms, phrase, 5)
    check("a paused root is not re-queued by a search",
          not list((cache / "index").glob("*.request")))

# 5. bucket order: the source is the contract here — inserting the semantic
#    bucket at 0 would put it ahead of the user's own documents.
src = (TOOL / "search.py").read_text(encoding="utf-8")
check("the semantic bucket goes after the user's documents",
      "buckets.insert(1 if text_bucket else 0, sem_bucket)" in src)
check("the user's own documents still go first",
      "buckets.insert(0, text_bucket)" in src)

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — semantic hits join as one more bucket, never duplicate a keyword "
      "hit, and an unindexed folder is queued instead of indexed inline")
