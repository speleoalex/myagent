#!/usr/bin/env python3
"""local_read tool — phase 2 of the two-phase library workflow.

Given a result id printed by ``local_search``, prints the document text at
length, paged. Ids are self-locating (both tools are stateless subprocesses):

  * ``z<N>:<entry_path>`` — article ``entry_path`` in the N-th ZIM archive of
    the sorted recursive listing under the root (same ordering as
    local_search; if archive N doesn't have the entry, every other archive is
    tried before giving up, so an added/removed ZIM between calls is mostly
    harmless).
  * ``f:<relpath>:<line>`` — text/Markdown file relative to the root; the
    line is where the matched chunk started, and becomes the default start
    of the first page when the file doesn't fit in one.
  * ``p:<relpath>:<page>`` — page of a PDF relative to the root. The text
    layer is extracted with ``pdftotext`` (same cache local_search fills, so
    a read right after a search costs nothing) and rendered with ``[p. N]``
    markers, so the model can cite the page it quotes. Reading starts at the
    matched page unless an explicit offset says otherwise.

Reads ``{"id": str, "offset"?: int, "path"?: str}`` as JSON on stdin.
``offset`` is a character offset into the extracted plain text (extraction is
deterministic, so offsets stay valid across calls). ``path`` must be the same
root passed to local_search, if any.

NOTE: the helpers marked "duplicated" are copied from
../local_search/search.py — keep them in sync (CoW overrides copy one
folder, so the two tools cannot share a module). DELIBERATE exception:
this copy of ``_TextExtractor`` also collects ``<img>`` tags (src + alt) so
article images can be delivered to the chat — local_search's copy must NOT
gain that (snippets don't need images); don't "fix" the divergence.
"""
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser

PAGE = 8000                    # chars per page, cut back to a word boundary
MAX_TEXT_BYTES = 3_000_000     # refuse text files larger than this
PDF_CACHE_MAX_BYTES = 20_000_000

# Article images delivered to the chat (resource channel, docs/TOOLS.md):
# extracted from the ZIM into <workspace>/_resources/ and flagged with a
# [[resource:...]] marker on stdout. Capped hard — an illustrated article can
# reference dozens.
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 2_000_000    # per image
MAX_IMAGES_TOTAL_BYTES = 5_000_000
# Extension FROM THE MIMETYPE, never from the entry name: Kiwix recompresses
# to WebP but keeps the original filename, so "_assets_/x.JPG" is image/webp.
IMG_EXT = {"image/webp": ".webp", "image/png": ".png", "image/jpeg": ".jpg",
           "image/gif": ".gif", "image/svg+xml": ".svg"}

BLOCK_TAGS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "ul", "ol", "table", "blockquote"}
SKIP_TAGS = {"script", "style", "head", "sup", "table"}  # sup = ref markers


# --------------------------------------------------------------------------- #
# HTML -> plain text — duplicated from local_search
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    """Collect visible text from article HTML, dropping scripts/refs/tables.

    Unlike local_search's copy, also records every ``<img>`` (src, alt) — see
    the module docstring. Recorded UNCONDITIONALLY, skip depth included: the
    article's main image usually sits in the infobox, i.e. inside a ``<table>``
    that SKIP_TAGS drops. The inline ``[image: …]`` placeholder instead
    respects the skip (it must not resurrect text inside a dropped table); it
    is what tells the model, from the TEXT alone, that images exist."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0
        self.images = []           # (src, alt), document order

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            d = dict(attrs)
            src = (d.get("src") or "").strip()
            if src:
                self.images.append((src, (d.get("alt") or "").strip()))
                if self._skip_depth == 0:
                    name = d.get("alt") or posixpath.basename(src).split("?")[0]
                    self._parts.append(f"\n[image: {name}]\n")
            return
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self):
        raw = "".join(self._parts)
        lines = []
        for line in raw.splitlines():
            line = " ".join(line.split())
            if line:
                lines.append(line)
        return "\n".join(lines)


def default_library_dir():
    # duplicated from local_search
    return os.environ.get("MYAGENT_LIBRARY") or os.path.join(
        os.path.expanduser("~"), "myagent", "library")


def _walk(root):
    """os.walk that FOLLOWS directory symlinks — duplicated from local_search.

    A library is normally ASSEMBLED, not copied: the bulky archives live on an
    external disk and get symlinked under ~/myagent/library. os.walk defaults
    to followlinks=False, so those trees were skipped in silence.

    followlinks=True can loop forever on a symlink cycle, hence the realpath
    set. It also DEDUPES: two links to the same directory are walked once, so
    the same archive cannot show up under two z<N> indices.
    """
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []          # already walked under another name
            continue
        seen.add(real)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        yield dirpath, dirnames, filenames


def list_zims(root):
    """Sorted recursive listing of *.zim under *root* — duplicated from
    local_search. The sort order DEFINES the ``z<N>`` id indices, so both
    tools must list identically."""
    found = []
    for dirpath, dirnames, filenames in _walk(root):
        for name in filenames:
            if name.lower().endswith(".zim"):
                path = os.path.join(dirpath, name)
                found.append((os.path.relpath(path, root).replace(os.sep, "/"),
                              path))
    found.sort()
    return [path for _, path in found]


# --------------------------------------------------------------------------- #
# PDF text layer — duplicated from local_search (keep byte-for-byte in sync:
# the two tools must agree on the page count and on the extracted text, or a
# ``p:`` id stops meaning the same page in both)
# --------------------------------------------------------------------------- #
def pdf_cache_dir():
    # duplicated from local_search
    base = os.environ.get("MYAGENT_CACHE") or os.path.join(
        os.path.expanduser("~"), "myagent", "cache")
    return os.path.join(base, "pdftext")


def normalize_layout(text):
    """Squeeze ``pdftotext -layout`` output: whitespace runs to two spaces, at
    most one blank line — duplicated from local_search.

    ``-layout`` is kept because it holds a table ROW together ("Clutch cover
    bolt  15-22  1.5-2.2  11-15"), while plain pdftotext puts every cell on a
    line of its own and a torque table stops saying which value belongs to
    which bolt. What ``-layout`` also does is pad with the page geometry:
    measured on a scanned service manual, 45% of its output was indentation.
    """
    out, blank = [], 0
    for line in text.splitlines():
        line = re.sub(r"[ \t]{2,}", "  ", line).strip()
        if not line:
            blank += 1
            if blank == 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _pdf_trim(pages):
    """Drop trailing blank pages — duplicated from local_search. Applied on
    BOTH the extraction and the cache-read path, or the same PDF would report
    a different page count depending on who had warmed the cache."""
    while pages and not pages[-1].strip():
        pages.pop()
    return pages


def _pdf_cache_key(path):
    # duplicated from local_search: realpath + mtime + size, so re-OCRing a
    # file invalidates its entry by itself.
    try:
        st = os.stat(path)
    except OSError:
        return None
    h = hashlib.sha1(os.path.realpath(path).encode("utf-8", "replace")).hexdigest()[:16]
    return f"{h}-{int(st.st_mtime)}-{st.st_size}.txt"


def _pdf_cache_write(dest, key, body):
    """Best-effort — duplicated from local_search. A cache that cannot be
    written must never fail a read, so every error here is swallowed."""
    if len(body) > PDF_CACHE_MAX_BYTES:
        return
    folder = os.path.dirname(dest)
    try:
        os.makedirs(folder, exist_ok=True)
        tmp = f"{dest}.{os.getpid()}"          # a search may race us
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, dest)
        stale = key.split("-")[0]              # same file, older mtime/size
        for name in os.listdir(folder):
            if name.startswith(stale) and name != os.path.basename(dest):
                os.unlink(os.path.join(folder, name))
    except OSError:
        pass


def pdf_pages(path, deadline=None):
    """``[page_text, …]`` for a PDF, 1-based by position — duplicated from
    local_search.

    Blank pages are KEPT in place: the index IS the printed page number, which
    is what a ``p:`` id and document_extract's ``from_page`` both mean.
    """
    key = _pdf_cache_key(path)
    cached = os.path.join(pdf_cache_dir(), key) if key else None
    if cached:
        try:
            with open(cached, "r", encoding="utf-8", errors="replace") as f:
                return _pdf_trim(f.read().split("\f"))
        except OSError:
            pass
    left = 90 if deadline is None else int(deadline - time.monotonic())
    if left <= 0:
        return None
    try:
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                              capture_output=True, timeout=min(90, max(1, left)))
    except subprocess.TimeoutExpired:
        return None
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 and not proc.stdout:
        return []
    pages = _pdf_trim([normalize_layout(p) for p in
                       proc.stdout.decode("utf-8", "replace").split("\f")])
    if cached:
        _pdf_cache_write(cached, key, "\f".join(pages))
    return pages


def pdf_document(pages):
    """``(text, offsets)``: the whole PDF as ONE citable string, every page
    prefixed with ``[p. N]``, plus the char offset of each page inside it.

    The marker is what lets the model quote a page number it can be held to;
    the offsets are what make "start at the page that matched" a plain
    character offset, i.e. the same paging contract the other two id kinds
    already use.
    """
    parts, offsets, pos = [], [], 0
    for n, body in enumerate(pages, 1):
        chunk = f"[p. {n}]\n{body}".rstrip()
        offsets.append(pos)
        parts.append(chunk)
        pos += len(chunk) + 2          # the "\n\n" the join inserts
    return "\n\n".join(parts), offsets


# --------------------------------------------------------------------------- #
# Paged rendering
# --------------------------------------------------------------------------- #
def render(rid, title, label, text, offset, note=""):
    total = len(text)
    if offset >= total and total:
        fail(f"offset {offset} is past the end of the document ({total} chars)")
    offset = max(0, min(offset, total))
    if 0 < offset < total and not text[offset - 1].isspace():
        nxt = text.find(" ", offset)    # never start mid-word
        offset = nxt + 1 if 0 <= nxt < offset + 100 else offset
    end = min(offset + PAGE, total)
    body = text[offset:end]
    if end < total and " " in body:
        body = body.rsplit(" ", 1)[0]   # don't split a word across pages
        end = offset + len(body)
    # Title first, id last: the model quotes the head of this line as the
    # source, and "Fonte: z2:Robert_Millikan" is not a citation a reader can
    # use.
    header = title
    if label:
        header += f"  [{label}]"
    header += f"  — id {rid}, chars {offset}-{end} of {total}"
    if note:
        header += f"  ({note})"
    if end < total:
        footer = f"…more — call local_read with offset={end}"
    else:
        footer = "— end of document —"
    print(f"{header}\n\n{body.strip()}\n\n{footer}")


def fail(msg):
    # Every refusal opens with "nothing was read" and asks for a retry in the
    # SAME turn: told only "ERROR", small models give up on the read and
    # answer from the snippet — or re-run the search they already ran.
    print(f"NOTHING WAS READ — {msg}", file=sys.stderr)
    sys.exit(1)


def fail_bad_id(rid):
    hint = ""
    if re.fullmatch(r"z\d+", rid):
        hint = (" You passed only the archive number: an id also carries the "
                "article path after the colon, e.g. 'z1:Leonardo_Da_Vinci'.")
    elif rid.startswith("f:"):
        hint = (" A file id ends with the line number, e.g. "
                "'f:notes/cave.md:42'.")
    elif rid.startswith("p:"):
        hint = (" A PDF id ends with the page number, e.g. "
                "'p:manuals/clutch.pdf:12'.")
    fail(f"'{rid}' is not a complete id.{hint} Copy an id from the local_search "
         f"output verbatim, colon included, and call local_read again now.")


# --------------------------------------------------------------------------- #
# The two id branches
# --------------------------------------------------------------------------- #
def read_zim(root, rid, n, entry_path, offset, images, path_given=True):
    try:
        from libzim.reader import Archive
    except ImportError as e:
        fail(f"libzim not installed in this interpreter ({sys.executable}): {e}")

    if os.path.isfile(root):
        zims = [root] if root.lower().endswith(".zim") else []
    else:
        zims = list_zims(root)
    if not zims:
        fail(f"no ZIM archives under {root}")

    # Try the indexed archive first, then all others (index drift fallback).
    candidates = []
    if 1 <= n <= len(zims):
        candidates.append(zims[n - 1])
    candidates += [z for z in zims if z not in candidates]

    for zim_path in candidates:
        try:
            archive = Archive(zim_path)
            entry = archive.get_entry_by_path(entry_path)
        except Exception:
            continue
        item = entry.get_item()
        # A model can paste an image id here; decoding a binary through UTF-8
        # and the HTML stripper yields garbage, not an error — refuse instead.
        mime = (item.mimetype or "").lower()
        if mime and not mime.startswith("text/"):
            fail(f"entry '{entry_path}' is {mime}, not a readable document — "
                 f"read the article that references it instead")
        html = bytes(item.content).decode("utf-8", "replace")
        parser = _TextExtractor()
        try:
            parser.feed(html)
        except Exception:
            pass
        render(rid, entry.title, os.path.basename(zim_path),
               parser.text(), offset)
        # Images go to the user by default on the FIRST page only (each later
        # page would re-deliver the same files); images=true forces them on a
        # later page, images=false disables. Markers go on stdout AFTER the
        # footer — the registry drops stderr of a clean exit.
        if images is not False and (offset == 0 or images is True):
            emit_images(archive, entry_path, parser.images, entry.title)
        return
    fail(f"entry '{entry_path}' not found in any ZIM archive under "
         f"{root}.{wrong_root_hint(path_given, certain=False)}")


def emit_images(archive, entry_path, images, title):
    """Extract up to MAX_IMAGES article images into <workspace>/_resources/
    and print one resource marker each. Best-effort and silent per image:
    a mini/nopic archive simply has nothing resolvable here."""
    workdir = os.environ.get("MYAGENT_WORKDIR") or os.getcwd()
    out_dir = os.path.join(workdir, "_resources")
    base = posixpath.dirname(entry_path)
    seen, done, total = set(), 0, 0
    for src, alt in images:
        if done >= MAX_IMAGES or total >= MAX_IMAGES_TOTAL_BYTES:
            break
        if src in seen or src.startswith(("data:", "http:", "https:", "//")):
            continue
        seen.add(src)
        # The src is relative to the article ("./_assets_/<md5>.JPG");
        # normpath resolves it to an archive path get_entry_by_path accepts.
        tries = [posixpath.normpath(posixpath.join(base, src)), src]
        if src.startswith("./"):
            tries.append(src[2:])
        item = None
        for path in dict.fromkeys(tries):
            try:
                item = archive.get_entry_by_path(path).get_item()
                break
            except Exception:
                continue
        if item is None:
            continue
        mime = (item.mimetype or "").lower().split(";")[0].strip()
        if not mime.startswith("image/") or item.size > MAX_IMAGE_BYTES:
            continue
        try:
            raw = bytes(item.content)
        except Exception:
            continue
        total += len(raw)
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_",
                      (alt or posixpath.basename(src).rsplit(".", 1)[0]
                       or "image"))[:40] or "image"
        fname = f"{stem}-{hashlib.md5(raw).hexdigest()[:8]}" \
                f"{IMG_EXT.get(mime, '.img')}"
        try:
            os.makedirs(out_dir, exist_ok=True)
            dest = os.path.join(out_dir, fname)
            if not os.path.exists(dest):
                with open(dest, "wb") as f:
                    f.write(raw)
        except OSError:
            continue
        label = " ".join((alt or title or "").split())
        label = label.replace("|", "/").replace("]]", ")")
        print(f"[[resource:_resources/{fname}|{mime}|{label}]]")
        done += 1


def wrong_root_hint(path_given, certain=True):
    """An id only resolves against the root that produced it. When the caller
    passed no ``path``, "not found" almost always means the search that printed
    the id ran somewhere else — so say that instead of letting the model
    conclude the document does not exist (measured: three failed reads in a row,
    then "I can't find this information in the documents").

    *certain* is False for ZIM ids, where the same failure is usually just an
    invented article name: there the hint has to be a condition, or it sends
    the model off to add a 'path' it never had.
    """
    if path_given:
        return ""
    if certain:
        return (" This id was printed by a local_search over another folder: "
                "call local_read again NOW with the same 'path' you gave "
                "local_search.")
    return (" If that search passed a 'path', pass the same one here and call "
            "local_read again now.")


def resolve_in_root(root, rel, path_given=True):
    """The file an ``f:``/``p:`` id points at, or fail.

    The id comes from the MODEL, which can invent one, so a fabricated
    "../../etc/passwd" must never resolve. The check is LEXICAL and not
    realpath-based: a library is normally ASSEMBLED from symlinks (see
    _walk), so a file local_search just offered legitimately resolves
    outside the root — a realpath containment check rejected exactly the
    ids the search had produced ("escapes the library root" on every note
    under a symlinked folder). Traversal is what we block; a symlink the
    library owner installed is topology, not model input.
    """
    if os.path.isfile(root):
        return root
    rel = os.path.normpath(rel).replace(os.sep, "/")
    if os.path.isabs(rel) or rel == ".." or rel.startswith("../"):
        fail(f"path in id escapes the library root: {rel}")
    target = os.path.normpath(os.path.join(root, rel))
    if not os.path.isfile(target):
        fail(f"file not found: {target}.{wrong_root_hint(path_given)}")
    return target


def read_pdf(root, rid, rel, page, offset, offset_given, path_given=True):
    if not shutil.which("pdftotext"):
        fail("pdftotext (poppler-utils) is not installed, so PDFs cannot be "
             "read on this machine")
    target = resolve_in_root(root, rel, path_given)
    pages = pdf_pages(target) or []
    if not any(p.strip() for p in pages):
        fail(f"'{rel}' has no text layer — it is a scan nobody OCR'd, so there "
             f"is nothing to read here. document_extract on the same path can "
             f"OCR it instead")

    text, offsets = pdf_document(pages)
    if 1 <= page <= len(offsets):
        note = f"match on page {page}"
        if not offset_given:
            offset = offsets[page - 1]
    else:
        note = f"page {page} is past the end ({len(pages)} pages)"
    render(rid, rel, f"PDF, {len(pages)} pages", text, offset, note)


def read_file(root, rid, rel, line, offset, offset_given, path_given=True):
    target = resolve_in_root(root, rel, path_given)
    if os.path.getsize(target) > MAX_TEXT_BYTES:
        fail(f"file too large to read: {target}")
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # Small file: whole thing. Big file with no explicit offset: start the
    # first page shortly before the matched line, so the read lands on the
    # match instead of burning pages from the top — but never so late that
    # the page comes back nearly empty (a match in the last paragraph).
    note = ""
    if len(text) > PAGE and line > 1:
        note = f"match at line {line}"
        if not offset_given:
            at = sum(len(ln) + 1 for ln in text.splitlines()[:line - 1])
            offset = max(0, min(at - 200, len(text) - PAGE))
    render(rid, rel, "", text, offset, note)


def main():
    try:
        args = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        fail(f"invalid JSON input: {e}")

    rid = str(args.get("id") or "").strip()
    if not rid:
        fail("'id' is required")

    offset_given = args.get("offset") is not None
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset, offset_given = 0, False

    given = (args.get("path") or "").strip()
    root = os.path.expanduser(given or default_library_dir())
    if not os.path.exists(root):
        fail(f"library path not found: {root}")

    # None = default (first page only); tolerate the string forms small
    # models produce for booleans.
    images = args.get("images")
    if isinstance(images, str):
        images = images.strip().lower() in ("1", "true", "yes")

    m = re.match(r"z(\d+):(.+)$", rid)
    if m:
        read_zim(root, rid, int(m.group(1)), m.group(2), offset, images,
                 bool(given))
        return
    if rid.startswith(("f:", "p:")):
        rel, sep, tail = rid[2:].rpartition(":")
        if sep and tail.isdigit():
            if rid.startswith("p:"):
                read_pdf(root, rid, rel, int(tail), offset, offset_given,
                         bool(given))
            else:
                read_file(root, rid, rel, int(tail), offset, offset_given,
                          bool(given))
            return
    fail_bad_id(rid)


if __name__ == "__main__":
    main()
