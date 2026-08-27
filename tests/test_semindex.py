#!/usr/bin/env python3
"""The semantic index — server/tools/library/local_search/semindex.py.

Run: server/.venv/bin/python tests/test_semindex.py
(stdlib + numpy only, no network: the embedder is a deterministic fake, so a
run is reproducible and costs nothing.)

The contract, one case each:
  1. a first pass indexes everything and answers queries;
  2. a second pass over unchanged files re-embeds NOTHING (mtime+size);
  3. touching one file re-indexes THAT file only;
  4. deleting a file forgets it — otherwise the search keeps offering ids that
     open nothing;
  5. changing the embedding model wipes the index: vectors from two models are
     not comparable and half a mixed index answers worse than none;
  6. a deadline leaves the rest PENDING, and the next pass finishes it;
  7. every degraded path returns None instead of raising — no embedder, no
     numpy, an unreadable database. Semantic search is an upgrade, never a
     requirement;
  8. THE ONE THAT MATTERS: the locators stored here are the ids local_search
     prints, so local_read opens the passage the search offered. A private
     splitter in semindex would break this silently;
  9. text that is not prose never gets a vector, and a hit below the score
     floor is not offered. "Nearest" is not "relevant", and argsort always
     returns `limit` rows.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "server" / "tools" / "library" / "local_search"
sys.path.insert(0, str(TOOL))

import semindex                                          # noqa: E402
import search                                            # noqa: E402

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


class FakeEmbedder:
    """Deterministic bag-of-words vectors: same text -> same vector, and two
    texts sharing words end up near each other. Counts calls, which is how the
    staleness cases below are actually proven."""

    def __init__(self, model="fake-a", dim=16):
        self.model, self.dim, self.calls, self.texts = model, dim, 0, 0
        self.batch, self.throttle_ms = 32, 0

    def encode(self, texts):
        self.calls += 1
        self.texts += len(texts)
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in t.lower().split():
                v[hash(w) % self.dim] += 1.0
            out.append(v or [0.0] * self.dim)
        return out


def build_root(tmp):
    root = Path(tmp) / "docs"
    root.mkdir()
    (root / "frizione.md").write_text(
        "# Frizione\n\nLa coppia di serraggio dei bulloni della frizione "
        "e' 15-22 Nm secondo il manuale.\n")
    (root / "freni.md").write_text(
        "# Freni\n\nLo spessore minimo delle pastiglie dei freni anteriori "
        "e' 2 mm, oltre il quale vanno sostituite.\n")
    (root / "olio.txt").write_text(
        "Il cambio olio motore va eseguito ogni 10000 chilometri "
        "oppure una volta all'anno.\n")
    return root


def files_in(root):
    text_files, pdf_files = search.collect_files(str(root))
    return sorted(text_files + pdf_files)


reader = lambda p, rel: search.chunks_for(p, rel)        # noqa: E731

with tempfile.TemporaryDirectory() as tmp:
    os.environ["MYAGENT_CACHE"] = str(Path(tmp) / "cache")
    root = build_root(tmp)
    files = files_in(root)
    db = semindex.db_path_for(str(root))

    # 1. first pass
    emb = FakeEmbedder()
    idx = semindex.SemanticIndex(str(root), db, emb)
    rep = idx.sync(files, reader)
    check("first pass indexes every file", rep.indexed == 3)
    check("first pass reports nothing pending", rep.complete)
    check("progress() sees them", idx.progress()["indexed"] == 3)

    hits = idx.search("bulloni frizione coppia serraggio", limit=3)
    # NOT a fixed count: search() applies SEM_MIN_SCORE, so how many of three
    # unrelated documents clear the floor is a property of the embedder, and
    # this one is a 16-dimension word hash whose absolute cosines mean nothing.
    # What must hold is that the relevant document comes back, and first.
    check("a relevant query comes back with at least one hit", len(hits) >= 1)
    check("the best hit is the right document",
          hits[0]["rel"] == "frizione.md")
    check("every hit clears the score floor",
          all(h["score"] >= semindex.SEM_MIN_SCORE for h in hits))

    # 8. the locators must be the ids local_search prints.
    terms, phrase = search.parse_query("frizione bulloni")
    kw = {h["id"] for h in search.search_text_file(
        str(root / "frizione.md"), "frizione.md", terms, phrase)}
    check("the stored locator is an id the keyword search also mints",
          hits[0]["locator"] in kw)
    # ...and it must actually resolve back to a real passage.
    rel, _, line = hits[0]["locator"][2:].rpartition(":")
    check("the locator points at a file that exists",
          (root / rel).is_file() and line.isdigit())

    # 2. nothing changed -> nothing re-embedded
    before = emb.calls
    rep = idx.sync(files, reader)
    check("an unchanged pass skips every file", rep.skipped == 3 and rep.indexed == 0)
    check("an unchanged pass calls the embedder zero times", emb.calls == before)

    # 3. touch one file -> only that one
    time.sleep(1.1)                      # mtime has 1s resolution
    (root / "olio.txt").write_text(
        "Il cambio olio motore va eseguito ogni 15000 chilometri.\n")
    before = emb.calls
    rep = idx.sync(files_in(root), reader)
    check("editing one file re-indexes exactly that one",
          rep.indexed == 1 and rep.skipped == 2)
    check("editing one file costs one embedder call", emb.calls == before + 1)

    # 4. delete -> forgotten
    (root / "freni.md").unlink()
    rep = idx.sync(files_in(root), reader)
    check("a deleted file is forgotten", rep.forgotten == 1)
    check("its chunks are gone too",
          all(h["rel"] != "freni.md" for h in idx.search("freni pastiglie", 5)))

    # 5. a different model invalidates everything
    idx.close()
    idx = semindex.SemanticIndex(str(root), db, FakeEmbedder(model="fake-b"))
    rep = idx.sync(files_in(root), reader)
    check("switching embedding model re-indexes from scratch", rep.indexed == 2)
    idx.close()

with tempfile.TemporaryDirectory() as tmp:
    # 7b. SEM_MIN_SCORE is a FLOOR, not just an ordering, and SEM_MIN_WORDLIKE
    # keeps non-prose out of the index entirely. Both guard one failure the
    # keyword scorer is immune to by accident: junk text matches no query term,
    # so it never ranked — but a vector has a direction whatever built it, and
    # argsort always yields `limit` rows. Measured on a folder of vehicle
    # manuals: a scanned wiring diagram extracting as `z'o]I I lrr: I o(J T`
    # scored 0.399 for "tubi EGR posizione e collegamento" and took a top slot
    # ahead of a real page at 0.385 — and the merge gives this bucket a fixed
    # position, so it evicted a real keyword hit from another source.
    #
    # A WIDE fake embedder here on purpose: the 16-dimension one above cannot
    # express "far away", because hashing four nonsense words into 16 buckets
    # collides with the corpus and yields a high cosine by accident.
    os.environ["MYAGENT_CACHE"] = str(Path(tmp) / "cache")
    root = build_root(tmp)
    (root / "diagram.txt").write_text(
        "z'o]I I lrr: I o(J T -t- Bt a, -t C) 6]> ;- 3r o F o EEL?EFS r:Is J m "
        "bl d 5\u20acT '< (t ; bl/ | f ' E ; Hi 5:E o ffi tftt r> >trt >'C' _,- > 3\n")
    db = semindex.db_path_for(str(root))
    idx = semindex.SemanticIndex(str(root), db, FakeEmbedder(model="wide", dim=512))
    rep = idx.sync(files_in(root), reader)
    check("a file that is not prose contributes no vectors",
          rep.skipped_chunks >= 1)
    check("...and the run says so rather than dropping it silently",
          "skipped_chunks" in repr(rep))
    check("its text is nowhere in the index",
          all("diagram" not in (h["rel"] or "")
              for h in idx.search("z'o]I lrr o(J EEL?EFS", limit=5)))
    check("a relevant query still works", 
          idx.search("bulloni frizione coppia", limit=3)[0]["rel"] == "frizione.md")
    far = idx.search("zzzz qqqq xxxx wwww yyyy vvvv", limit=3)
    check("a query with nothing near it returns NOTHING, not the least distant",
          far == [])
    idx.close()


with tempfile.TemporaryDirectory() as tmp:
    os.environ["MYAGENT_CACHE"] = str(Path(tmp) / "cache")
    root = build_root(tmp)
    db = semindex.db_path_for(str(root))

    # 6. a deadline leaves work pending, and the next pass finishes it.
    idx = semindex.SemanticIndex(str(root), db, FakeEmbedder())
    rep = idx.sync(files_in(root), reader, deadline=time.monotonic() - 1)
    check("an expired deadline indexes nothing", rep.indexed == 0)
    check("an expired deadline reports the work as PENDING, not done",
          rep.pending == 3 and not rep.complete)
    rep = idx.sync(files_in(root), reader)
    check("the next pass finishes what was pending", rep.indexed == 3)
    idx.close()

    # 7. degraded paths -> None, never an exception.
    for var in ("MYAGENT_EMBED_URL", "MYAGENT_EMBED_MODEL"):
        os.environ.pop(var, None)
    check("no embedder configured -> no index, no error",
          semindex.open_index(str(root)) is None)
    check("Embedder.from_env() is None when nothing is configured",
          semindex.Embedder.from_env() is None)

    real_np = semindex._np
    semindex._np = None
    check("no numpy -> no index, no error",
          semindex.open_index(str(root), embed=FakeEmbedder()) is None)
    semindex._np = real_np

    bad = Path(tmp) / "cache" / "index" / "broken.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"this is not a database, not even close")
    broken = semindex.open_index(str(root), cache_dir=str(bad.parent),
                                 embed=FakeEmbedder())
    if broken is not None:
        try:
            broken.sync(files_in(root), reader)
            broken.close()
        except Exception:
            failures.append("a corrupt database raised instead of degrading")

# --------------------------------------------------------------------------- #
# 9. The wire format. Embedder talks urllib to a real socket here, so batching,
#    the Authorization header, ordering by "index" and a server error are all
#    exercised without a model or a network.
# --------------------------------------------------------------------------- #
import json as _json                                      # noqa: E402
import threading                                          # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

seen = {"batches": [], "auth": None}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen["auth"] = self.headers.get("Authorization")
        texts = body["input"]
        seen["batches"].append(len(texts))
        if any("boom" in t for t in texts):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "nope"}')
            return
        # Deliberately OUT of order, with an explicit index: the client must
        # sort, or every vector would be attached to the wrong chunk.
        rows = [{"index": i, "embedding": [float(len(t)), 1.0, 0.0]}
                for i, t in enumerate(texts)]
        payload = _json.dumps({"data": list(reversed(rows))}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


srv = HTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_address[1]}/v1/embeddings"

emb = semindex.Embedder(url, "wire-model", key="s3cret", batch=2)
vecs = emb.encode(["aa", "bbbb", "cccccc"])
check("the wire client returns one vector per input", len(vecs) == 3)
check("vectors are re-ordered by the response 'index'",
      [v[0] for v in vecs] == [2.0, 4.0, 6.0])
check("inputs are split into batches of the configured size",
      seen["batches"] == [2, 1])
check("an api key travels as a Bearer header",
      seen["auth"] == "Bearer s3cret")

try:
    emb.encode(["boom"])
    failures.append("a server error was swallowed instead of raised")
except Exception:
    pass                      # the caller decides; sync() turns it into failed

os.environ["MYAGENT_EMBED_URL"] = url
os.environ["MYAGENT_EMBED_MODEL"] = "wire-model"
from_env = semindex.Embedder.from_env()
check("from_env() picks the configured endpoint up",
      from_env is not None and from_env.url == url
      and from_env.model == "wire-model")
srv.shutdown()

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — the index tracks the filesystem, invalidates on model change, and"
      " mints locators local_read can open; every degraded path returns None")
