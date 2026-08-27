#!/usr/bin/env python3
"""Semantic (vector) index over a folder of documents — standalone.

Deliberately independent of MyAgent: stdlib sqlite3 + urllib only, numpy as an
optional accelerator, and NOTHING imported from the server. It runs from a bare
terminal and takes its vectors from either of two backends:

    # an OpenAI-compatible /v1/embeddings endpoint (llama.cpp, Ollama, ...)
    python semindex.py --root DIR --index --embed-url URL --embed-model NAME
    # or in this very process, via fastembed — no server, nothing to configure
    python semindex.py --root DIR --index --embed-local
    python semindex.py --root DIR --query "clutch bolt torque"
    python semindex.py --root DIR --stats

The two are interchangeable behind ``embedder_from_env()`` and differ in one
way that matters: the local one cannot leak. Indexing sends the CONTENT of
every document to the embedder — the corpus, not the query — which is why the
HTTP side has to be *policed* (only local providers, see
``app.engine.embedding``) while the in-process side is safe by construction.

THE CALLBACK IS THE POINT. ``sync()`` takes ``read_chunks(path, rel)`` and knows
nothing about PDFs, Markdown or ZIM archives — only about sqlite, vectors and
staleness. Whoever calls it brings their own reader, and with it their own idea
of what a locator means.

For THIS tool that reader is ``search.chunks_for``, and using it is not a
convenience: the locator stored next to every vector is the id ``local_search``
would print, which is what the model then hands to ``local_read``. A reader
that split text differently would mint line numbers no keyword hit ever has —
the id would open a different passage, or nothing, and nothing would raise. So
the CLI imports search.py (its sister in the same leaf folder, copied together
by a copy-on-write override) rather than defining a splitter of its own.

Invariants worth keeping:

- Semantic search is an UPGRADE, never a requirement. No embedder, no numpy, an
  unreadable database — ``open_index()`` returns None and the caller does
  exactly what it did before. Nothing here may ever make a search fail.
- One database per ROOT (keyed by its realpath), under the cache: derived data,
  safe to delete, never inside the user's own folder — which may be read-only,
  or on a disk that gets unplugged.
- Changing the embedding model invalidates everything. Vectors from two models
  are not comparable, and half a mixed index is worse than none.
- One transaction per file. A SIGTERM, a reboot, a pulled plug: the index stays
  consistent and the next run resumes at the following file.
- A file that disappeared is forgotten (``forget_missing``), or the search
  would keep offering ids that no longer open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request

# Bumped to 2 when SEM_MIN_WORDLIKE arrived: an index built without it holds
# vectors for pages of OCR noise, and those took top slots. The stored set no
# longer means the same thing, so _create() wipes and the next pass rebuilds —
# the same mechanism a changed embedding model uses, for the same reason.
SCHEMA_VERSION = 2
DEFAULT_BATCH = 32
# Skip a TEXT file this big — mirrors local_search's own MAX_TEXT_BYTES: past
# this it is a log or a database dump, not something anyone wants matched.
#
# It is NOT applied to PDFs, and that distinction is load-bearing. For a text
# file the size on disk IS the text; for a PDF it is mostly images, and the
# service manuals this exists for run 96-205 MB with a small text layer.
# Capping them by file size excluded 56 of 109 PDFs in the folder that
# motivated the feature — every real manual among them — while the report
# still said "complete". What bounds a PDF is the extraction deadline and the
# pdftext cache, both of which already exist.
MAX_TEXT_BYTES = 3_000_000
PDF_EXTS = {".pdf"}
# Exempt for the SAME reason as PDFs, one step removed: a .docx/.xlsx/.pptx is a
# compressed archive, so its size on disk is not the size of its text either
# (either direction — a 500 KB spreadsheet expands to megabytes, a 5 MB report is
# mostly embedded images). What bounds them is MAX_OFFICE_CHARS, applied by
# search.office_text to the text that comes OUT.
OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
# Refuse to index a root with more files than this, and SAY so. An unbounded
# root would embed forever without ever reporting that it is not finished.
MAX_INDEX_FILES = 20_000
HTTP_TIMEOUT = 120
# The in-process embedder's default model. Multilingual on purpose: this index
# exists for a library that mixes Italian notes with English service manuals,
# and a monolingual model turns "coppia di serraggio" over an English manual
# into no answer at all — the exact failure local_search already fights with
# query relaxation. Measured (fastembed 0.8, this model): an Italian query
# scores 0.60 against the equivalent ENGLISH sentence and 0.11 against an
# unrelated one, so the cross-language hit is real and not noise.
#
# 384 dimensions and 241 MB on disk, against 1.0 GB for the mpnet sibling
# doc_indexer uses: at this corpus size the recall difference does not pay for
# a download four times bigger on a machine that may be a laptop.
DEFAULT_LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Bigger than the HTTP batch: there is no request per batch to amortise, and
# onnxruntime is faster with more rows. Measured ~540 chunks/s on this CPU.
DEFAULT_LOCAL_BATCH = 64
# A chunk whose text is mostly NOT words is not indexed, and a hit below
# SEM_MIN_SCORE is not offered. Both guard the same failure, which the keyword
# scorer is immune to by accident: junk matches no query term, so it never
# ranked — but a vector has a direction whatever it was built from, and
# "nearest" is not "relevant". Measured on a folder of vehicle manuals, the
# scanned wiring diagrams extract as `z'o]I I lrr: I o(J T -t- Bt a,` and one of
# them scored 0.399 for "tubi EGR posizione e collegamento", taking a top slot
# ahead of a real page at 0.385.
#
# 0.30 comes from the measurement, not from taste: on that corpus the junk pages
# sit at 0.08-0.13 wordlike and real manual pages have a median of 0.69-0.81.
# The threshold costs 5.6% of pages with a text layer, and they are exactly the
# ones it should cost — wiring and body DIAGRAMS, whose text layer is OCR noise
# around a drawing. A drawing cannot be matched by meaning.
#
# The filter lives HERE and not in `search.chunks_for`, which is shared with the
# keyword scorer and defines the locators: skipping a chunk must change what is
# EMBEDDED, never what an id means. Keyword search still reaches those pages,
# where an OCR-surviving part number is a legitimate match.
SEM_MIN_WORDLIKE = 0.30
SEM_MIN_SCORE = 0.30
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_VOWEL_RE = re.compile(r"[aeiouyàèéìòóùáíúäöüåøæœ]", re.IGNORECASE)

try:                                            # optional, and checked for
    import numpy as _np
except Exception:                               # pragma: no cover
    _np = None


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def myagent_home():
    return os.environ.get("MYAGENT_HOME") or os.path.join(
        os.path.expanduser("~"), "myagent")


def index_cache_dir():
    base = os.environ.get("MYAGENT_CACHE") or os.path.join(myagent_home(), "cache")
    return os.path.join(base, "index")


def db_path_for(root, cache_dir=None):
    """One database per root, named by its realpath so two agents pointed at the
    same folder share an index instead of each building their own."""
    key = hashlib.sha1(os.path.realpath(root).encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(cache_dir or index_cache_dir(), f"{key}.db")


def request_path_for(root, cache_dir=None):
    return db_path_for(root, cache_dir)[:-3] + ".request"


def paused_path_for(root, cache_dir=None):
    return db_path_for(root, cache_dir)[:-3] + ".paused"


def request_index(root, ocr=None, cache_dir=None):
    """Ask the server to (re)build the index for *root*, and return.

    A tool runs as a subprocess: it cannot reach app.state, and it must not do
    the work itself — indexing a folder of manuals takes minutes and the tool
    has a 30s timeout. So it drops a request file and the server's IndexService
    picks it up, runs `semindex.py --index` under supervision and deletes it.

    Best-effort by construction: no embedder means no point, a `.paused` marker
    means the user stopped this root and a search must not restart it behind
    their back, and any failure here is silent — a search must never fail
    because of bookkeeping.
    """
    if embedder_from_env() is None:
        return False
    req = request_path_for(root, cache_dir)
    try:
        if os.path.exists(paused_path_for(root, cache_dir)):
            return False
        os.makedirs(os.path.dirname(req), exist_ok=True)
        if os.path.exists(req):
            return True                     # already queued; don't churn mtime
        if ocr is None:
            ocr = (os.environ.get("MYAGENT_INDEX_OCR") or "") not in ("", "0", "false")
        tmp = f"{req}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"root": os.path.realpath(root), "ocr": bool(ocr),
                       "requested_at": time.time()}, f)
        os.replace(tmp, req)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
class Embedder:
    """An OpenAI-compatible ``/v1/embeddings`` endpoint, and nothing else.

    urllib rather than httpx so the module keeps working outside the app venv,
    which is the whole point of it being standalone.
    """

    def __init__(self, url, model, key=None, batch=DEFAULT_BATCH, throttle_ms=0):
        self.url = url
        self.model = model
        self.key = key or ""
        self.batch = max(1, int(batch or DEFAULT_BATCH))
        self.throttle_ms = max(0, int(throttle_ms or 0))

    @classmethod
    def from_env(cls, **kw):
        """The embedder the server configured, or None.

        The server only ever exports these for a LOCAL provider (see
        AgentExecutor.tool_env_overrides): indexing sends the CONTENT of every
        document to the endpoint, not just the query, so a remote one would ship
        the user's whole corpus off the machine.
        """
        url = (os.environ.get("MYAGENT_EMBED_URL") or "").strip()
        model = (os.environ.get("MYAGENT_EMBED_MODEL") or "").strip()
        if not url or not model:
            return None
        return cls(url, model, os.environ.get("MYAGENT_EMBED_KEY"), **kw)

    def encode(self, texts):
        """[[float, ...], ...] for *texts*, in order. Raises on failure — the
        caller decides whether that aborts a run or just skips a file."""
        out = []
        for i in range(0, len(texts), self.batch):
            out.extend(self._one(texts[i:i + self.batch]))
            # Embedding competes with the chat model for the same backend: on a
            # single-slot server every batch is a request the user's turn waits
            # behind. Pausing between batches is what keeps a background index
            # from making the assistant feel broken.
            if self.throttle_ms and i + self.batch < len(texts):
                time.sleep(self.throttle_ms / 1000.0)
        return out

    def _one(self, batch):
        body = json.dumps({"model": self.model, "input": batch}).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
        rows = payload.get("data") or []
        if len(rows) != len(batch):
            raise ValueError(
                f"embeddings endpoint returned {len(rows)} vectors for "
                f"{len(batch)} inputs")
        # index is advisory in the spec but every server sends it; sorting on it
        # costs nothing and protects against one that reorders.
        rows.sort(key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in rows]


def embed_cache_dir():
    """Where the in-process embedder keeps its model files.

    Under the cache and not the config dir: it is derived data, it is large,
    and deleting it must be safe — the next index run downloads it again.
    """
    return os.environ.get("MYAGENT_EMBED_CACHE") or os.path.join(
        os.environ.get("MYAGENT_CACHE") or os.path.join(myagent_home(), "cache"),
        "embed-models")


class LocalEmbedder:
    """An embedding model running IN THIS PROCESS, via fastembed (onnxruntime).

    The point is not speed, it is that there is nothing to configure and
    nothing to leak: no endpoint, no registered model, no second server to keep
    running, and the corpus never leaves the machine — so the local-only rule
    that ``app.engine.embedding`` has to *enforce* for the HTTP backend holds
    here by construction.

    Two properties make it safe to reach for from a search path:

    - **Nothing happens until ``encode()``.** Constructing this object does not
      import fastembed, create an ONNX session or touch the network. Measured:
      0.00s with a warm cache, because ``open_index()`` builds an embedder just
      to answer "is semantic search on?" and must not pay for a model to do it.
    - **A search may never download.** With ``allow_download`` false we set
      ``HF_HUB_OFFLINE``, so a missing model raises in ~1s instead of pulling
      241 MB inside a tool with a 30s timeout. Only an index run — background,
      niced, throttled, stoppable, and retried on failure — is allowed to
      fetch it. The gate is the library's own offline switch rather than a
      guess at its cache layout, which would rot the moment fastembed
      reorganised it.
    """

    def __init__(self, name=None, cache_dir=None, batch=DEFAULT_LOCAL_BATCH,
                 throttle_ms=0, allow_download=False):
        self.name = name or DEFAULT_LOCAL_MODEL
        # The invalidation key, read by SemanticIndex._check_model_name. The
        # prefix is load-bearing: `nomic-embed-text` served over HTTP and the
        # same weights run locally are not guaranteed to agree (pooling,
        # quantisation), and a silently mixed index answers worse than none.
        self.model = f"fastembed:{self.name}"
        self.cache_dir = cache_dir or embed_cache_dir()
        self.batch = max(1, int(batch or DEFAULT_LOCAL_BATCH))
        self.throttle_ms = max(0, int(throttle_ms or 0))
        self.allow_download = bool(allow_download)
        self._impl = None

    @staticmethod
    def available():
        """True if fastembed can be imported — WITHOUT importing it.

        importlib.find_spec costs a stat; importing fastembed pulls onnxruntime
        and tokenizers, which is far too much for a question the server answers
        on every settings page load.
        """
        try:
            import importlib.util
            return importlib.util.find_spec("fastembed") is not None
        except Exception:                              # pragma: no cover
            return False

    @classmethod
    def from_env(cls, allow_download=False, **kw):
        """The local embedder the server asked for, or None.

        ``MYAGENT_EMBED_LOCAL`` carries the model name, or a bare truthy value
        to mean "the default one". Absent = off: an installed package is not a
        decision to spend CPU and 241 MB on somebody's folders, so switching
        this on stays explicit (Settings -> Embedding model -> Local).
        """
        name = (os.environ.get("MYAGENT_EMBED_LOCAL") or "").strip()
        if not name:
            return None
        if name.lower() in ("1", "true", "yes", "on", "local", "default"):
            name = DEFAULT_LOCAL_MODEL
        kw.pop("batch", None)                  # the HTTP batch size is not ours
        return cls(name, allow_download=allow_download, **kw)

    def _load(self):
        if self._impl is not None:
            return self._impl
        if not self.allow_download:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import warnings
        # fastembed warns that this model switched from CLS to mean pooling and
        # suggests pinning 0.5.1. We do not: the index carries the model name
        # and re-embeds itself if it ever changes. Silenced because this text
        # would otherwise be the first 200 characters the IndexService reports
        # when a run really fails.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastembed import TextEmbedding
            os.makedirs(self.cache_dir, exist_ok=True)
            # lazy_load defers the ONNX session, not the download: with a warm
            # cache this is free, which is what makes open_index() cheap.
            self._impl = TextEmbedding(self.name, cache_dir=self.cache_dir,
                                       lazy_load=True)
        return self._impl

    def encode(self, texts):
        """[[float, ...], ...] for *texts*, in order — same contract as the
        HTTP Embedder, so nothing downstream knows which one it has."""
        impl = self._load()
        out = []
        for i in range(0, len(texts), self.batch):
            chunk = texts[i:i + self.batch]
            vecs = list(impl.embed(chunk, batch_size=len(chunk)))
            if len(vecs) != len(chunk):
                raise ValueError(f"local embedder returned {len(vecs)} vectors "
                                 f"for {len(chunk)} inputs")
            # float64 comes back from onnxruntime; pack() writes float32 and
            # the stored vectors are float32, so cast here rather than let
            # struct silently narrow.
            out.extend([float(x) for x in v] for v in vecs)
            # CPU, not a shared model server — os.nice(10) in the IndexService
            # covers most of it — but a throttle still keeps a single-core box
            # answering the chat while it indexes.
            if self.throttle_ms and i + self.batch < len(texts):
                time.sleep(self.throttle_ms / 1000.0)
        return out


def embedder_from_env(allow_download=False, **kw):
    """THE embedder, whichever kind — or None when semantic search is off.

    An HTTP endpoint wins, because naming a registered model is the more
    specific statement of intent; the in-process one is what you get when the
    answer is just "yes, locally". Every call site goes through here so the
    two backends can never both be half-used.
    """
    return (Embedder.from_env(**kw)
            or LocalEmbedder.from_env(allow_download=allow_download, **kw))


def wordlike_ratio(text):
    """Fraction of alphabetic tokens that look like words (3+ letters, a vowel).

    Language-agnostic on purpose — it asks "is this prose at all", not "is this
    Italian". A page of OCR noise scores under 0.15 because its tokens are one
    and two character fragments of symbols; prose in any Latin-script language
    scores well above 0.5. Empty text is 0.0, which is the right answer here.
    """
    toks = _WORD_RE.findall(text)
    if not toks:
        return 0.0
    good = sum(1 for w in toks if len(w) >= 3 and _VOWEL_RE.search(w))
    return good / len(toks)


def pack(vec):
    """float32 little-endian, the on-disk form."""
    return struct.pack(f"<{len(vec)}f", *vec)


# --------------------------------------------------------------------------- #
# The index
# --------------------------------------------------------------------------- #
class SyncReport:
    def __init__(self):
        self.indexed = 0        # files (re)indexed this run
        self.skipped = 0        # unchanged since last run
        self.forgotten = 0      # rows dropped for files that disappeared
        self.pending = 0        # known to need work, not reached this run
        self.failed = 0
        self.oversized = 0      # permanently excluded (text file over the cap)
        self.skipped_chunks = 0  # not prose (see SEM_MIN_WORDLIKE)
        self.total = 0
        self.capped = False     # the root has more files than MAX_INDEX_FILES

    @property
    def complete(self):
        # `oversized` does NOT count against completeness: it is a permanent,
        # declared exclusion, and treating it as outstanding work would make
        # the "still indexing" note nag forever over files that will never be
        # indexed. `pending` is work that a later run WILL do.
        return self.pending == 0 and self.failed == 0

    def __repr__(self):
        return (f"<SyncReport indexed={self.indexed} skipped={self.skipped} "
                f"forgotten={self.forgotten} pending={self.pending} "
                f"failed={self.failed} oversized={self.oversized} "
                f"skipped_chunks={self.skipped_chunks} total={self.total}>")


class SemanticIndex:
    def __init__(self, root, db_path, embed=None):
        self.root = os.path.realpath(root)
        self.db_path = db_path
        self.embed = embed
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path, timeout=30)
        # WAL: a search READS while the indexer WRITES. Without it a
        # "database is locked" would turn a healthy search into an error.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._create()

    # -- schema ----------------------------------------------------------
    def _create(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                rel TEXT UNIQUE, mtime INTEGER, size INTEGER,
                status TEXT, n_chunks INTEGER);
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
                locator TEXT, heading TEXT,
                line_from INTEGER, line_to INTEGER,
                text TEXT, vec BLOB);
            CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
        """)
        self.db.commit()
        if self._meta("schema") != str(SCHEMA_VERSION):
            self._wipe()
            self._set_meta(schema=str(SCHEMA_VERSION), root=self.root)

    def _meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, **kv):
        self.db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            [(k, str(v)) for k, v in kv.items()])
        self.db.commit()

    def _wipe(self):
        """Everything goes. Called when the schema or the embedding model
        changes: vectors from two models are not comparable, and half a mixed
        index answers worse than no index at all."""
        self.db.executescript("DELETE FROM chunks; DELETE FROM files;")
        self.db.commit()

    def _check_model_name(self):
        """Invalidate when the embedding MODEL changed. Called at the top of
        every sync, not while indexing a file: with nothing stale, no file is
        ever indexed, so a check buried in that path would never run and a
        switched model would leave the old vectors in place forever (caught by
        tests/test_semindex.py)."""
        name = self.embed.model if self.embed else ""
        if self._meta("embed_model") == name:
            return False
        if self._meta("embed_model") is not None:
            self._wipe()
            self.db.execute("DELETE FROM meta WHERE key='dim'")
        self._set_meta(embed_model=name, root=self.root)
        return True

    def _check_dim(self, dim):
        """Same rule for the DIMENSION, checked on the first vector of a run.

        The name alone is the case that happens; this catches the one that
        should not — a server re-pointing the same model id at a different
        model. Cheap, and the alternative is a silently mixed index.
        """
        seen = self._meta("dim")
        if seen == str(dim):
            return False
        if seen is not None:
            self._wipe()
        self._set_meta(dim=dim)
        return True

    # -- maintenance ------------------------------------------------------
    def forget_missing(self):
        """Drop every file that is no longer on disk. Without this the search
        keeps offering ids that open nothing."""
        gone = [(fid, rel) for fid, rel in
                self.db.execute("SELECT id, rel FROM files")
                if not os.path.exists(os.path.join(self.root, rel))]
        for fid, _ in gone:
            self.db.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
            self.db.execute("DELETE FROM files WHERE id=?", (fid,))
        if gone:
            self.db.commit()
        return len(gone)

    def progress(self):
        indexed = self.db.execute("SELECT COUNT(*) FROM files WHERE status='ok'").fetchone()[0]
        total = int(self._meta("total", indexed) or indexed)
        return {
            "indexed": indexed,
            "total": max(total, indexed),
            "chunks": self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "complete": indexed >= max(total, indexed) and total > 0,
            "model": self._meta("embed_model", ""),
            "dim": self._meta("dim", ""),
            "updated_at": float(self._meta("updated_at", 0) or 0),
        }

    def _stale(self, rel, st):
        row = self.db.execute(
            "SELECT id, mtime, size FROM files WHERE rel=?", (rel,)).fetchone()
        if row is None:
            return True, None
        # mtime+size, the same rule the PDF text cache already uses: cheap, and
        # wrong only for an edit that preserves both.
        return (int(st.st_mtime) != row[1] or st.st_size != row[2]), row[0]

    # -- indexing ---------------------------------------------------------
    def sync(self, files, read_chunks, deadline=None, on_file=None):
        """Bring the index up to date with *files* (absolute paths).

        ``read_chunks(path, rel)`` returns ``[(locator, heading, text,
        line_from, line_to), ...]``, ``[]`` for nothing usable, or ``None`` for
        "could not read it in time" — reported as pending, never as done.

        Stops at *deadline* (time.monotonic) leaving the rest pending: an
        interrupted run is normal, not an error, and the next one resumes.
        """
        rep = SyncReport()
        if self.embed is None:
            return rep
        if len(files) > MAX_INDEX_FILES:
            rep.capped = True
            files = files[:MAX_INDEX_FILES]
        rep.total = len(files)
        self._check_model_name()
        rep.forgotten = self.forget_missing()

        todo = []
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                rep.failed += 1
                continue
            if (os.path.splitext(path)[1].lower() not in PDF_EXTS | OFFICE_EXTS
                    and st.st_size > MAX_TEXT_BYTES):
                rep.oversized += 1
                continue
            rel = os.path.relpath(path, self.root).replace(os.sep, "/")
            stale, fid = self._stale(rel, st)
            if stale:
                todo.append((path, rel, st, fid))
            else:
                rep.skipped += 1

        # `total` is what we INTEND to index, so progress() can ever reach
        # complete: counting permanently excluded files in would pin it below
        # 100% forever.
        self._set_meta(total=len(files) - rep.oversized)

        for path, rel, st, fid in todo:
            if deadline is not None and time.monotonic() >= deadline:
                rep.pending = len(todo) - rep.indexed - rep.failed
                break
            try:
                ok = self._index_one(path, rel, st, fid, read_chunks, rep)
            except Exception as e:                      # never fatal
                print(f"WARNING: could not index {rel}: {e}", file=sys.stderr)
                rep.failed += 1
                continue
            if ok is None:
                rep.pending += 1
            elif ok:
                rep.indexed += 1
            else:
                rep.skipped += 1
            if on_file:
                on_file(rel, rep)
        self._set_meta(updated_at=time.time())
        return rep

    def _index_one(self, path, rel, st, fid, read_chunks, rep=None):
        """One file, one transaction. True = indexed, False = nothing to index,
        None = not readable yet (stays pending)."""
        chunks = read_chunks(path, rel)
        if chunks is None:
            return None
        # Drop what is not prose BEFORE embedding. Kept as a count so the caller
        # can say so: a page silently missing from the index is a page the user
        # will assume was searched.
        if chunks:
            usable = [c for c in chunks
                      if wordlike_ratio(c[2]) >= SEM_MIN_WORDLIKE]
            if rep is not None:
                rep.skipped_chunks += len(chunks) - len(usable)
            chunks = usable
        if chunks:
            # The heading is prepended to what gets EMBEDDED but not to what is
            # stored: "Frizione\n\nLa coppia e' 15-22 Nm." places a one-line
            # paragraph correctly in the vector space, while the text shown
            # later must stay the passage as written.
            vecs = self.embed.encode(
                [f"{h}\n\n{t}".strip() if h else t for _, h, t, _, _ in chunks])
            if self._check_dim(len(vecs[0])):
                fid = None                      # the wipe took the old row
        else:
            vecs = []

        cur = self.db.cursor()
        try:
            cur.execute("BEGIN")
            if fid is not None:
                cur.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                cur.execute("UPDATE files SET mtime=?, size=?, status=?, "
                            "n_chunks=? WHERE id=?",
                            (int(st.st_mtime), st.st_size, "ok", len(chunks), fid))
            else:
                cur.execute("INSERT OR REPLACE INTO files "
                            "(rel, mtime, size, status, n_chunks) VALUES (?,?,?,?,?)",
                            (rel, int(st.st_mtime), st.st_size, "ok", len(chunks)))
                fid = cur.lastrowid
            cur.executemany(
                "INSERT INTO chunks (file_id, locator, heading, line_from, "
                "line_to, text, vec) VALUES (?,?,?,?,?,?,?)",
                [(fid, loc, head, a, b, txt, pack(v))
                 for (loc, head, txt, a, b), v in zip(chunks, vecs)])
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return bool(chunks)

    # -- querying ---------------------------------------------------------
    def search(self, query, limit=5):
        """Best *limit* chunks for *query*, most similar first.

        Brute-force cosine: under ~100k chunks an approximate index buys
        milliseconds and costs a dependency plus a rebuild step.
        """
        if self.embed is None or _np is None:
            return []
        if self._meta("embed_model") not in (None, self.embed.model):
            # Queried with a model the stored vectors do not belong to. Answer
            # nothing rather than nonsense; the next sync wipes and rebuilds.
            return []
        rows = self.db.execute(
            "SELECT c.locator, c.heading, c.line_from, c.line_to, c.text, "
            "c.vec, f.rel FROM chunks c JOIN files f ON f.id=c.file_id").fetchall()
        if not rows:
            return []
        try:
            qv = _np.asarray(self.embed.encode([query])[0], dtype=_np.float32)
        except Exception as e:
            print(f"WARNING: could not embed the query: {e}", file=sys.stderr)
            return []

        dim = qv.size
        keep = [r for r in rows if len(r[5]) == dim * 4]
        if not keep:
            return []
        mat = _np.frombuffer(b"".join(r[5] for r in keep),
                             dtype="<f4").reshape(len(keep), dim)
        # Normalise both sides: cosine, and no division by zero on a null vector.
        norms = _np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        qn = _np.linalg.norm(qv) or 1.0
        scores = (mat @ qv) / (norms * qn)

        # A floor, not just a ranking. argsort always yields `limit` rows, so
        # without this a folder with nothing relevant still fills every slot —
        # and the merge gives this bucket a fixed position, so those rows evict
        # real keyword hits. "Nearest" is not "relevant".
        best = [i for i in _np.argsort(-scores)[:max(1, limit)]
                if scores[i] >= SEM_MIN_SCORE]
        return [{
            "locator": keep[i][0], "heading": keep[i][1],
            "line_from": keep[i][2], "line_to": keep[i][3],
            "text": keep[i][4], "rel": keep[i][6],
            "score": float(scores[i]),
        } for i in best]

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


def open_index(root, cache_dir=None, embed=None, create=True):
    """A SemanticIndex for *root*, or None — and None is a normal answer.

    None means "no semantic search here": no embedder configured, no numpy, an
    unreadable cache. Every caller must then behave exactly as it did before
    this module existed. Nothing in here may ever make a search fail.
    """
    if _np is None:
        return None
    # No allow_download here on purpose: this is the SEARCH path. A local
    # embedder built here refuses to fetch its model, so a cold cache costs a
    # second and an empty bucket instead of a 241 MB download inside a tool
    # call. Only the CLI's --index run passes allow_download.
    embed = embed if embed is not None else embedder_from_env()
    if embed is None:
        return None
    try:
        path = db_path_for(root, cache_dir)
        if not create and not os.path.exists(path):
            return None
        return SemanticIndex(root, path, embed)
    except Exception as e:
        print(f"WARNING: semantic index unavailable: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# CLI — also how the IndexService runs a background pass
# --------------------------------------------------------------------------- #
def _sister_reader():
    """search.chunks_for, imported from the sister module in this folder.

    Not a convenience: the locators stored here must be the ids local_search
    prints, or local_read opens the wrong passage (see the module docstring).
    Falling back to a private splitter would hide that breakage, so there is
    no fallback — without search.py the CLI refuses to index.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import search                                        # noqa: E402
    return lambda path, rel: search.chunks_for(path, rel), search


def _cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--budget", type=float, default=0,
                    help="seconds to spend indexing (0 = until done)")
    ap.add_argument("--throttle-ms", type=int, default=0,
                    help="pause between embedding batches, so a background "
                         "pass does not starve the chat model")
    # Accepted and, for now, only passed on to the reader through the
    # environment: IndexService already sends --ocr for a request that asks
    # for it, and argparse would exit 2 on an unknown flag — a run that can
    # only ever fail, retried with backoff forever. The reader does not OCR
    # yet, so this is deliberately inert rather than a promise.
    ap.add_argument("--ocr", action="store_true",
                    help="allow OCR while extracting (reader support pending)")
    ap.add_argument("--embed-url"), ap.add_argument("--embed-model")
    ap.add_argument("--embed-local", nargs="?", const=DEFAULT_LOCAL_MODEL,
                    help="embed in this process with fastembed instead of an "
                         "HTTP endpoint (optionally naming the model)")
    ap.add_argument("--prefetch", action="store_true",
                    help="download the local embedding model and exit (--root "
                         "is ignored) — how install.sh gets the one-time fetch "
                         "out of the way")
    args = ap.parse_args(argv)

    if args.embed_url:
        os.environ["MYAGENT_EMBED_URL"] = args.embed_url
    if args.embed_model:
        os.environ["MYAGENT_EMBED_MODEL"] = args.embed_model
    if args.ocr:
        os.environ["MYAGENT_INDEX_OCR"] = "1"
    if args.embed_local:
        os.environ["MYAGENT_EMBED_LOCAL"] = args.embed_local
        # An explicit --embed-local means "use the local one", so it must not
        # lose to an MYAGENT_EMBED_URL inherited from the service environment.
        os.environ.pop("MYAGENT_EMBED_URL", None)

    if args.prefetch:
        emb = LocalEmbedder.from_env(allow_download=True)
        if emb is None:
            print("ERROR: --prefetch needs --embed-local or "
                  "MYAGENT_EMBED_LOCAL.", file=sys.stderr)
            return 2
        try:
            emb.encode(["warm the cache"])
        except Exception as e:
            print(f"ERROR: could not fetch {emb.name}: {e}", file=sys.stderr)
            return 1
        print(f"ready: {emb.name} in {emb.cache_dir}")
        return 0

    root = os.path.expanduser(args.root)
    if not os.path.isdir(root):
        print(f"ERROR: not a folder: {root}", file=sys.stderr)
        return 2
    # Indexing is the ONE path allowed to fetch the local model: it runs in the
    # background under the IndexService, which throttles, nices, times out,
    # retries and can be stopped from the UI.
    embed = embedder_from_env(throttle_ms=args.throttle_ms,
                              allow_download=bool(args.index))
    if embed is None:
        print("ERROR: no embedder — set MYAGENT_EMBED_URL and "
              "MYAGENT_EMBED_MODEL (or pass --embed-url/--embed-model), or "
              "use --embed-local to embed in this process with fastembed.",
              file=sys.stderr)
        return 2
    if _np is None:
        print("ERROR: numpy is required for semantic search.", file=sys.stderr)
        return 2

    idx = open_index(root, embed=embed)
    if idx is None:
        print("ERROR: could not open the index.", file=sys.stderr)
        return 2

    if args.index:
        _, search_mod = _sister_reader()
        text_files, pdf_files = search_mod.collect_files(root)
        files = sorted(text_files + pdf_files, key=lambda p: os.path.getsize(p)
                       if os.path.exists(p) else 0)
        deadline = time.monotonic() + args.budget if args.budget else None
        # The deadline is baked into the reader, not passed to sync alone: PDF
        # text extraction happens INSIDE it, and without a deadline one
        # pathological file gets its own 90s timeout.
        rep = idx.sync(files,
                       lambda path, rel: search_mod.chunks_for(path, rel, deadline),
                       deadline)
        print(json.dumps(rep.__dict__) if args.json else rep)

    if args.query:
        hits = idx.search(args.query, args.limit)
        if args.json:
            print(json.dumps(hits, ensure_ascii=False))
        else:
            for h in hits:
                snippet = re.sub(r"\s+", " ", h["text"])[:110]
                print(f"{h['score']:.3f}  {h['locator']:32} {snippet}")

    if args.stats or not (args.index or args.query):
        p = idx.progress()
        p["db"] = idx.db_path
        print(json.dumps(p, ensure_ascii=False, indent=2) if args.json
              else " ".join(f"{k}={v}" for k, v in p.items()))
    idx.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
