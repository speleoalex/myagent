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

Reads ``{"id": str, "offset"?: int, "path"?: str, "images"?: bool,
"export"?: bool}`` as JSON on stdin. ``offset`` is a character offset into the
extracted plain text (extraction is deterministic, so offsets stay valid
across calls). ``path`` must be the same root passed to local_search, if any.
``export`` delivers the FULL document to the user as a file through the
resource channel instead of printing a page (see the export section below).

NOTE: the helpers marked "duplicated" are copied from
../local_search/search.py — keep them in sync (CoW overrides copy one
folder, so the two tools cannot share a module). DELIBERATE exception:
this copy of ``_TextExtractor`` also collects ``<img>`` tags (src + alt) so
article images can be delivered to the chat — local_search's copy must NOT
gain that (snippets don't need images); don't "fix" the divergence.
"""
import base64
import datetime
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import zipfile
import unicodedata
from html.parser import HTMLParser
from xml.etree import ElementTree

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

# Full-document export (export=true): the whole document goes to the USER as a
# file through the resource channel; the tool result stays tiny, because tool
# results are re-sent on every iteration — the reason the search is two-phase
# applies doubly to full documents.
MAX_EXPORT_BYTES = 25_000_000   # f:/p: copy cap — _resources/ is never pruned
HTML_EXPORT_BUDGET = 400_000    # exported article incl. inlined images: keeps
                                # an illustrated article under the web UI's
                                # 512 KB inline-preview cap (ui/js/chat.js)
# mimetypes.guess_type misses .md on some systems, and the OOXML types are
# absent from Python's built-in table (they come from /etc/mime.types when that
# exists at all) — an export typed octet-stream is one the browser refuses to
# hand to Word.
EXPORT_MIME = {
    ".md": "text/markdown", ".txt": "text/plain", ".rst": "text/x-rst",
    ".docx": "application/vnd.openxmlformats-officedocument"
             ".wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument"
             ".presentationml.presentation",
}
# Injected at the top of exported articles. The <meta charset> is deliberate:
# the content is re-encoded UTF-8 here, and a stale declaration deeper in the
# original <head> must lose (the first one in the document wins in browsers).
EXPORT_STYLE = ('<meta charset="utf-8"><style>body{max-width:46em;'
                'margin:1em auto;padding:0 1em;font-family:sans-serif;'
                'line-height:1.5}img{max-width:100%;height:auto}'
                'table{border-collapse:collapse}td,th{border:1px solid #ccc;'
                'padding:.2em .5em}</style>')

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


def myagent_home():
    """Root of the runtime layout — duplicated from local_search.

    The server exports the RESOLVED paths (config.tool_env), so this chain only
    decides what a tool run by hand from a terminal reads."""
    return os.environ.get("MYAGENT_HOME") or os.path.join(
        os.path.expanduser("~"), "myagent")


def default_library_dir():
    """Where an id resolves when the call passes no ``path``.

    Must stay byte-identical in behaviour to local_search's copy: that tool
    prints the ids this one is asked to open, and the two processes share
    nothing but the environment. MYAGENT_AGENT_DIR is the calling agent's
    folder (Agent.folder.path); the shared library is the fallback.
    """
    return (os.environ.get("MYAGENT_AGENT_DIR")
            or os.environ.get("MYAGENT_LIBRARY")
            or os.path.join(myagent_home(), "library"))


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
    base = os.environ.get("MYAGENT_CACHE") or os.path.join(myagent_home(), "cache")
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
        # The export clause rides the footer because models follow it
        # LITERALLY (observed: given only the offset hint, a 4B model paged
        # twice and then claimed the full document had been delivered). The
        # offset clause stays FIRST: with export first, the same model
        # stopped paging and claimed delivery after a single read.
        footer = (f"…more — call local_read with offset={end}; if the user "
                  f"wants the WHOLE document as a file, call again with "
                  f"export=true instead")
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
def read_zim(root, rid, n, entry_path, offset, images, path_given=True,
             export=False):
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
        if export:
            # Full document to the user, nothing to the conversation; the
            # exported HTML embeds its own images, so emit_images is skipped.
            export_zim_entry(archive, entry, entry_path, rid, zim_path)
            return
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
    workdir = os.environ.get("MYAGENT_WORKSPACE") or os.getcwd()
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
        # Never label a bare image with the ARTICLE title: the model then
        # reads "file delivered: <article>" and claims the full document was
        # delivered without ever exporting it (observed on a 4B model).
        label = " ".join((alt or f"image from {title}" or "").split())
        label = label.replace("|", "/").replace("]]", ")")
        print(f"[[resource:_resources/{fname}|{mime}|{label}]]")
        done += 1


# --------------------------------------------------------------------------- #
# Full-document export (export=true) — this section exists ONLY here, never in
# local_search: exporting is a read-side concern, and the shared duplicated
# helpers above stay byte-identical.
# --------------------------------------------------------------------------- #
def fail_export(msg):
    # Same contract as fail(): a self-explanatory refusal plus the next step,
    # or a small model reports "delivered" for a file that never landed.
    print(f"NOTHING WAS EXPORTED — {msg}", file=sys.stderr)
    sys.exit(1)


def safe_stem(name, fallback="document"):
    """Filename stem from a title: ASCII-folded ('Città' -> 'Citta') then
    sanitized. The full Unicode title still travels in the marker — the
    filename is just an address."""
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_.")
    return name[:60] or fallback


def write_resource(data, stem, ext):
    """Write content-addressed under <workspace>/_resources/ (same naming
    scheme as emit_images; existing file = re-export is a no-op) and return
    the workspace-relative path for the marker."""
    workdir = os.environ.get("MYAGENT_WORKSPACE") or os.getcwd()
    out_dir = os.path.join(workdir, "_resources")
    fname = f"{stem}-{hashlib.md5(data).hexdigest()[:8]}{ext}"
    try:
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, fname)
        if not os.path.exists(dest):
            with open(dest, "wb") as f:
                f.write(data)
    except OSError as e:
        fail_export(f"could not write into the workspace: {e}")
    return f"_resources/{fname}"


def print_export(rid, title, label, relpath, mime, size):
    """The ENTIRE tool result for an export: header + marker + one coaching
    line, NO document body. The executor swaps the marker for its own "file
    delivered" note; the one thing that note does not say is the one thing a
    small model needs told — don't paste."""
    title = " ".join((title or "").split()) or "document"
    header = title
    if label:
        header += f"  [{label}]"
    header += f"  — id {rid} — full document exported ({size:,} bytes)"
    safe_title = title.replace("|", "/").replace("]]", ")")
    print(header)
    print(f"[[resource:{relpath}|{mime}|{safe_title}]]")
    print("The document was delivered to the user as a file. Do not paste its "
          "contents — answer by referring to it.")


def sanitize_html(html):
    """Make a ZIM article safe to serve standalone: scripts and stylesheet
    <link>s point at archive paths that resolve nowhere outside the ZIM (and
    /api/files serves the export raw, not only inside the sandboxed preview);
    srcset would shadow the inlined src; inline handlers go as belt and
    braces. <style> blocks are inline and kept."""
    html = re.sub(r"<script\b.*?</script\s*>", "", html, flags=re.S | re.I)
    html = re.sub(r"<script\b[^>]*>", "", html, flags=re.I)   # unclosed tail
    html = re.sub(r"<link\b[^>]*>", "", html, flags=re.I)
    html = re.sub(r"\ssrcset\s*=\s*(\"[^\"]*\"|'[^']*')", "", html, flags=re.I)
    html = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html,
                  flags=re.I)
    return html


def inline_images(archive, entry_path, html):
    """Rewrite <img> tags to self-contained data: URIs, resolving srcs exactly
    like emit_images, until HTML_EXPORT_BUDGET. Unresolvable or over-budget
    tags are DROPPED — cleaner than a broken-image icon in the exported page.
    data-src is checked first: mwoffliner lazy-loads images through it, and
    the scripts that would populate src are stripped by sanitize_html."""
    base = posixpath.dirname(entry_path)
    cache = {}
    spent = [len(html)]

    def attr(tag, name):
        m = re.search(rf"(?<![-\w]){name}\s*=\s*(\"([^\"]*)\"|'([^']*)')",
                      tag, flags=re.I)
        if not m:
            return ""
        return (m.group(2) if m.group(2) is not None else m.group(3)).strip()

    def repl(m):
        tag = m.group(0)
        src = attr(tag, "data-src") or attr(tag, "src")
        if not src or src.startswith(("http:", "https:", "//")):
            return tag                     # offline: the alt text shows
        if src.startswith("data:"):
            return tag                     # already inline
        if src not in cache:
            cache[src] = None
            tries = [posixpath.normpath(posixpath.join(base, src)), src]
            if src.startswith("./"):
                tries.append(src[2:])
            for path in dict.fromkeys(tries):
                try:
                    item = archive.get_entry_by_path(path).get_item()
                except Exception:
                    continue
                mime = (item.mimetype or "").lower().split(";")[0].strip()
                if not mime.startswith("image/"):
                    break
                try:
                    raw = bytes(item.content)
                except Exception:
                    break
                cache[src] = (f"data:{mime};base64,"
                              + base64.b64encode(raw).decode("ascii"))
                break
        uri = cache[src]
        if uri is None or spent[0] + len(uri) > HTML_EXPORT_BUDGET:
            return ""
        spent[0] += len(uri)
        alt = attr(tag, "alt").replace('"', "&quot;")
        return f'<img src="{uri}" alt="{alt}">'

    return re.sub(r"<img\b[^>]*>", repl, html, flags=re.I)


def export_zim_entry(archive, entry, entry_path, rid, zim_path):
    """Write the full article into _resources/ and print the marker. HTML is
    exported self-contained (sanitized, images inlined, reading style
    injected); other text/* entries (zimgit-style plain text) go as-is."""
    item = entry.get_item()
    mime = (item.mimetype or "").lower().split(";")[0].strip()
    raw = bytes(item.content)
    title = entry.title or posixpath.basename(entry_path)
    if mime == "text/html":
        html = sanitize_html(raw.decode("utf-8", "replace"))
        html = inline_images(archive, entry_path, html)
        head = re.search(r"<head\b[^>]*>", html, flags=re.I)
        if head:
            html = html[:head.end()] + EXPORT_STYLE + html[head.end():]
        else:
            html = EXPORT_STYLE + html
        data, ext, out_mime = html.encode("utf-8"), ".html", "text/html"
    else:
        data, ext, out_mime = raw, ".txt", "text/plain"
    relpath = write_resource(data, safe_stem(title), ext)
    print_export(rid, title, os.path.basename(zim_path), relpath, out_mime,
                 len(data))


def export_copy(target, rid, rel, mime):
    """Copy an f:/p: source file into _resources/ and print the marker."""
    try:
        size = os.path.getsize(target)
    except OSError as e:
        fail_export(f"cannot read '{rel}': {e}")
    if size > MAX_EXPORT_BYTES:
        fail_export(
            f"'{rel}' is {size / 1_000_000:.0f} MB, over the "
            f"{MAX_EXPORT_BYTES // 1_000_000} MB export limit — read it in "
            f"pages with local_read instead, or tell the user the file is at "
            f"{os.path.abspath(target)}")
    with open(target, "rb") as f:
        data = f.read()
    name = os.path.basename(rel)
    stem, ext = os.path.splitext(name)
    relpath = write_resource(data, safe_stem(stem), ext.lower())
    print_export(rid, name, "", relpath, mime, size)


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


def resolve_in_root(root, rel, path_given=True, quiet=False):
    """The file an ``f:``/``p:`` id points at, or fail.

    With ``quiet=True`` it PROBES instead: returns None where it would
    otherwise stop the tool. That is what lets main() ask "would this resolve?"
    before deciding how to read a malformed id, without turning the question
    into an answer.

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
        if quiet:
            return None
        fail(f"path in id escapes the library root: {rel}")
    target = os.path.normpath(os.path.join(root, rel))
    if not os.path.isfile(target):
        if quiet:
            return None
        fail(f"file not found: {target}.{wrong_root_hint(path_given)}")
    return target


def read_pdf(root, rid, rel, page, offset, offset_given, path_given=True,
             export=False):
    target = resolve_in_root(root, rel, path_given)
    if export:
        # The ORIGINAL file: for reading in full it beats the text layer, it
        # needs neither pdftotext nor OCR — a scan nobody OCR'd is exactly
        # the case where handing over the file is the only useful answer.
        export_copy(target, rid, rel, "application/pdf")
        return
    if not shutil.which("pdftotext"):
        fail("pdftotext (poppler-utils) is not installed, so PDFs cannot be "
             "read on this machine")
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


# --------------------------------------------------------------------------- #
# Office documents (OOXML) — duplicated in ../local_read/read.py
#
# .docx/.xlsx/.pptx are ZIP archives of XML, so the whole extractor is stdlib.
# That is a deliberate choice over python-docx/openpyxl/python-pptx (~7 packages
# including lxml, a C extension): these two tools are the OFFLINE core, their
# `run` launcher falls back to the system python3 when the venv is missing, and
# a dependency that is absent exactly in the disaster scenario the library
# exists for is worse than a simpler parser. Text extraction needs the `<w:t>`
# runs, not a document object model.
#
# The output of this code is where `f:<relpath>:<line>` ids POINT, so the two
# copies must stay char-for-char equal in BEHAVIOUR: local_search mints a line
# number from this text and local_read indexes its `offset` into it. Guarded by
# tests/test_library_helpers_sync.py.
#
# Legacy .doc/.xls/.ppt (OLE2, not ZIP) are NOT supported and cannot be with
# stdlib — neither do python-docx/openpyxl. Convert them once, with libreoffice.
OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}
# The text a single Office file may yield. Unlike a plain text file the size on
# disk is NOT the size of the text (it is a compressed archive), so the cap has
# to be applied to what comes OUT — a 500 KB spreadsheet expands to megabytes of
# cell values. Same number as MAX_TEXT_BYTES, applied one layer later.
MAX_OFFICE_CHARS = 3_000_000
# A zip that claims to expand to more than this is not a document anyone wants
# matched; refusing before extracting also makes a zip bomb a non-event.
MAX_OFFICE_UNCOMPRESSED = 80_000_000
# Cells and table cells are joined with this, never a newline: a table of
# torque values stops saying which figure belongs to which bolt when every
# cell lands on its own line. Same lesson as keeping pdftotext's -layout.
CELL_SEP = "  |  "
# Excel serial dates. 1899-12-30 is the epoch that makes serial 1 = 1900-01-01
# while absorbing the Lotus fake leap day (serial 60 = "1900-02-29", which does
# not exist); serials at or below it are off by one and rare enough to be left
# to the generic branch rather than given a wrong date.
_XLS_EPOCH_1900 = "1899-12-30"
_XLS_EPOCH_1904 = "1904-01-01"
# Builtin numFmt ids that mean "this number is a date/time" (ECMA-376 18.8.30).
_XLS_DATE_FMT_IDS = frozenset(list(range(14, 23)) + list(range(45, 48)))


def _xml_tag(elem):
    """An element's local name, namespace stripped.

    Namespace-agnostic on purpose: OOXML ships in a transitional and a strict
    flavour with DIFFERENT namespace URIs for the same elements, and a parser
    keyed on the URI silently returns nothing for the other one.
    """
    tag = elem.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_root(zf, name):
    """Parse one member of an open zip, or None if it is missing/unparseable."""
    try:
        with zf.open(name) as fh:
            return ElementTree.parse(fh).getroot()
    except Exception:
        return None


def _docx_para_text(para):
    """One <w:p> flattened: runs joined, tabs and breaks kept as whitespace."""
    out = []
    for node in para.iter():
        tag = _xml_tag(node)
        if tag == "t":
            out.append(node.text or "")
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _docx_heading_level(para):
    """1-6 if this paragraph uses a Heading style, else 0.

    Word headings become Markdown headings, which is not cosmetic: chunk_document
    turns them into the running context of every paragraph beneath, so a
    structured report gets real chunk titles in results AND in the semantic index
    instead of the file name repeated.
    """
    for node in para.iter():
        if _xml_tag(node) != "pStyle":
            continue
        val = ""
        for key, value in node.attrib.items():
            if key.rsplit("}", 1)[-1] == "val":
                val = str(value)
                break
        m = re.match(r"(?:heading|titolo|titre|berschrift|kop)\s*([1-6])$",
                     val.strip().lower())
        if m:
            return int(m.group(1))
    return 0


def _docx_text(zf):
    """Body text of a .docx, in document order."""
    root = _xml_root(zf, "word/document.xml")
    if root is None:
        return ""
    lines = []
    for body in root.iter():
        if _xml_tag(body) == "body":
            root = body
            break
    for node in root:
        tag = _xml_tag(node)
        if tag == "p":
            text = _docx_para_text(node).strip()
            if not text:
                lines.append("")
                continue
            level = _docx_heading_level(node)
            lines.append(f"{'#' * level} {text}" if level else text)
        elif tag == "tbl":
            # A table is emitted one row per line so a row stays readable as a
            # unit, with a blank line around it so chunk_document keeps the
            # whole table together instead of splitting it mid-row.
            lines.append("")
            for row in node:
                if _xml_tag(row) != "tr":
                    continue
                cells = [" ".join(_docx_para_text(p).split())
                         for cell in row if _xml_tag(cell) == "tc"
                         for p in cell.iter() if _xml_tag(p) == "p"]
                cells = [c for c in cells if c]
                if cells:
                    lines.append(CELL_SEP.join(cells))
            lines.append("")
    return "\n".join(lines)


def _pptx_slide_order(zf):
    """Slide part names in presentation order.

    Sorted NUMERICALLY: a lexical sort puts slide10 between slide1 and slide2,
    so every slide number printed in a result would be wrong from the tenth on.
    """
    names = [n for n in zf.namelist()
             if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]

    def key(name):
        m = re.search(r"(\d+)\.xml$", name)
        return int(m.group(1)) if m else 0

    return sorted(names, key=key)


def _pptx_text(zf):
    """Slide text of a .pptx, one "## Slide N" section per slide.

    The heading is what makes a hit citable — "slide 7" is how a presentation is
    referred to, the way a page is for a PDF — and chunk_document picks it up as
    the chunk title for free. Speaker notes are deliberately left out: mapping
    notesSlideN to slideN needs the relationship parts, and guessing it by number
    would attribute someone's notes to the wrong slide.
    """
    parts = []
    for n, name in enumerate(_pptx_slide_order(zf), 1):
        root = _xml_root(zf, name)
        if root is None:
            continue
        lines = []
        for para in root.iter():
            if _xml_tag(para) != "p":
                continue
            text = "".join(node.text or "" for node in para.iter()
                           if _xml_tag(node) == "t").strip()
            if text:
                lines.append(text)
        parts.append(f"## Slide {n}\n\n" + "\n\n".join(lines) if lines
                     else f"## Slide {n}")
    return "\n\n".join(parts)


def _xlsx_shared_strings(zf):
    root = _xml_root(zf, "xl/sharedStrings.xml")
    if root is None:
        return []
    out = []
    for si in root:
        out.append("".join(node.text or "" for node in si.iter()
                           if _xml_tag(node) == "t"))
    return out


def _xlsx_date_styles(zf):
    """Indices into cellXfs whose number format means "date".

    Without this a column of dates is searchable only as five-digit serials —
    and "when is the appointment" is exactly the question a folder of personal
    documents gets asked.
    """
    root = _xml_root(zf, "xl/styles.xml")
    if root is None:
        return set()
    custom = {}
    for node in root.iter():
        if _xml_tag(node) != "numFmt":
            continue
        fid = node.attrib.get("numFmtId")
        code = node.attrib.get("formatCode") or ""
        # Date tokens OUTSIDE literal quotes; "General" and currency codes
        # containing a stray 'd' inside a quoted string must not qualify.
        bare = re.sub(r'"[^"]*"', "", code).lower()
        if fid and re.search(r"[ymd]", bare) and "e+" not in bare:
            custom[fid] = True
    dated = set()
    for node in root.iter():
        if _xml_tag(node) != "cellXfs":
            continue
        for i, xf in enumerate(node):
            fid = xf.attrib.get("numFmtId")
            if fid is None:
                continue
            try:
                builtin = int(fid) in _XLS_DATE_FMT_IDS
            except ValueError:
                builtin = False
            if builtin or custom.get(fid):
                dated.add(i)
        break
    return dated


def _xlsx_serial_to_date(raw, epoch_1904):
    """An Excel serial rendered as ISO, or None if it is not one."""
    try:
        serial = float(raw)
    except (TypeError, ValueError):
        return None
    if serial <= 60 and not epoch_1904:
        return None
    base = _XLS_EPOCH_1904 if epoch_1904 else _XLS_EPOCH_1900
    try:
        day = datetime.date.fromisoformat(base) + datetime.timedelta(days=int(serial))
    except (ValueError, OverflowError):
        return None
    frac = serial - int(serial)
    if frac <= 0:
        return day.isoformat()
    seconds = int(round(frac * 86400))
    return f"{day.isoformat()} {seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def _xlsx_sheets(zf):
    """``[(display name, part name)]`` in workbook order.

    Read through the relationships rather than globbing sheetN.xml: the file
    order is not the tab order, and the NAME only exists in workbook.xml — a
    sheet called "Costi 2026" is what a result should be cited by.
    """
    book = _xml_root(zf, "xl/workbook.xml")
    rels = _xml_root(zf, "xl/_rels/workbook.xml.rels")
    targets = {}
    if rels is not None:
        for node in rels:
            rid = node.attrib.get("Id")
            target = node.attrib.get("Target") or ""
            if not rid or not target:
                continue
            target = target.lstrip("/")
            targets[rid] = target if target.startswith("xl/") else "xl/" + target
    out = []
    if book is not None:
        for node in book.iter():
            if _xml_tag(node) != "sheet":
                continue
            name = node.attrib.get("name") or ""
            rid = next((v for k, v in node.attrib.items()
                        if k.rsplit("}", 1)[-1] == "id"), None)
            part = targets.get(rid)
            if part and part in zf.namelist():
                out.append((name, part))
    if out:
        return out
    # A workbook we could not read the index of still has its sheets on disk.
    return [(os.path.basename(n), n) for n in sorted(zf.namelist())
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]


def _xlsx_epoch_1904(zf):
    book = _xml_root(zf, "xl/workbook.xml")
    if book is None:
        return False
    for node in book.iter():
        if _xml_tag(node) == "workbookPr":
            return str(node.attrib.get("date1904", "")).lower() in ("1", "true")
    return False


def _xlsx_text(zf):
    """Cell values of a .xlsx, one "## Sheet: name" section per sheet.

    One line per row, cells joined by CELL_SEP; empty rows and empty trailing
    cells are dropped, or a spreadsheet whose used range is wider than its data
    (very common) pads every line with separators.
    """
    strings = _xlsx_shared_strings(zf)
    dated = _xlsx_date_styles(zf)
    epoch_1904 = _xlsx_epoch_1904(zf)
    parts, budget = [], MAX_OFFICE_CHARS
    for name, part in _xlsx_sheets(zf):
        root = _xml_root(zf, part)
        if root is None:
            continue
        lines = [f"## Sheet: {name}" if name else "## Sheet"]
        for row in root.iter():
            if _xml_tag(row) != "row":
                continue
            cells = []
            for cell in row:
                if _xml_tag(cell) != "c":
                    continue
                cells.append(_xlsx_cell_text(cell, strings, dated, epoch_1904))
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                line = CELL_SEP.join(cells)
                budget -= len(line) + 1
                if budget <= 0:
                    lines.append("[... spreadsheet truncated]")
                    break
                lines.append(line)
        parts.append("\n".join(lines))
        if budget <= 0:
            break
    return "\n\n".join(parts)


def _xlsx_cell_text(cell, strings, dated, epoch_1904):
    """One <c> rendered as text: shared/inline string, date, or raw number."""
    ctype = cell.attrib.get("t") or "n"
    if ctype == "inlineStr":
        return " ".join("".join(n.text or "" for n in cell.iter()
                                if _xml_tag(n) == "t").split())
    value = ""
    for node in cell:
        if _xml_tag(node) == "v":
            value = node.text or ""
            break
    if ctype == "s":
        try:
            return " ".join(strings[int(value)].split())
        except (ValueError, IndexError):
            return ""
    if ctype == "b":
        return "TRUE" if value == "1" else "FALSE"
    if ctype in ("str", "e"):
        return " ".join(value.split())
    try:
        style = int(cell.attrib.get("s", "-1"))
    except ValueError:
        style = -1
    if style in dated:
        iso = _xlsx_serial_to_date(value, epoch_1904)
        if iso:
            return iso
    return value.strip()


def office_text(path):
    """Plain text of an OOXML document, or None if it cannot be read.

    None (rather than "") for an unreadable file so the caller reports it the
    same way it reports an unreadable text file; "" is a legitimately empty
    document.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in OFFICE_EXTS:
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            if sum(max(0, i.file_size) for i in zf.infolist()) > MAX_OFFICE_UNCOMPRESSED:
                return None
            if ext == ".docx":
                text = _docx_text(zf)
            elif ext == ".pptx":
                text = _pptx_text(zf)
            else:
                text = _xlsx_text(zf)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError):
        return None
    except Exception:
        return None
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    if len(text) > MAX_OFFICE_CHARS:
        # Declared, like every other cap here: a document cut in silence reads
        # as a document that ends there.
        text = text[:MAX_OFFICE_CHARS] + "\n\n[... document truncated]"
    return text


def read_text_file(path):
    """The text of one searchable non-PDF file, or None if it cannot be read.

    The single seam through which Office support reaches everything:
    search_text_file, chunks_for (so the semantic index too) and local_read's
    read_file all go through here, which is why `f:<relpath>:<line>` ids keep
    meaning the same thing for all of them.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in OFFICE_EXTS:
        return office_text(path)
    try:
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_file(root, rid, rel, line, offset, offset_given, path_given=True,
              export=False):
    target = resolve_in_root(root, rel, path_given)
    if export:
        # Before the MAX_TEXT_BYTES guard: that cap protects the model's
        # context, which an export never enters (its cap is MAX_EXPORT_BYTES).
        ext = os.path.splitext(rel)[1].lower()
        export_copy(target, rid, rel,
                    EXPORT_MIME.get(ext) or mimetypes.guess_type(rel)[0]
                    or "text/plain")
        return
    text = read_text_file(target)
    if text is None:
        # One message for both causes on purpose: for a text file the limit is
        # the size on disk, for an Office document it is the text that comes out
        # of it, and the caller cannot act differently on the two.
        fail(f"file too large or unreadable: {target}")

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

    export = args.get("export")
    if isinstance(export, str):
        export = export.strip().lower() in ("1", "true", "yes")
    export = bool(export)              # offset is simply ignored when exporting

    m = re.match(r"z(\d+):(.+)$", rid)
    if m:
        read_zim(root, rid, int(m.group(1)), m.group(2), offset, images,
                 bool(given), export)
        return
    if rid.startswith(("f:", "p:")):
        rel, sep, tail = rid[2:].rpartition(":")
        # A `path` the model made up is the OTHER half of the same problem: the
        # id is right and the root is wrong. If the file is not under the root
        # we were handed but IS under the default one, use that and say so —
        # the same forgiveness read_zim already extends to a drifted z<N>
        # index. Only ever a FALLBACK: an explicit path that works still wins.
        if given:
            probe = rel if (sep and tail.isdigit()) else rid[2:]
            fallback = os.path.expanduser(default_library_dir())
            if (probe and os.path.realpath(fallback) != os.path.realpath(root)
                    and not resolve_in_root(root, probe, quiet=True)
                    and resolve_in_root(fallback, probe, quiet=True)):
                print(f"NOTE: '{given}' does not hold this id; read from "
                      f"{fallback} instead. Do not pass 'path'.")
                root, given = fallback, ""
        if sep and tail.isdigit():
            if rid.startswith("p:"):
                read_pdf(root, rid, rel, int(tail), offset, offset_given,
                         bool(given), export)
            else:
                read_file(root, rid, rel, int(tail), offset, offset_given,
                          bool(given), export)
            return
        # No trailing ":<n>". Dropping the page number off a `p:` id is by far
        # the commonest way a model mangles one, and the file is still named in
        # full — the only thing missing is WHERE to start, which already has a
        # default. So open it at the top instead of refusing.
        #
        # This is not the tolerant-matching that file_edit deliberately refuses
        # to do: there, guessing could rewrite the wrong line, silently. Here
        # nothing is ambiguous (the path is exact), nothing is written, and the
        # header states what was opened — `id p:x.pdf:1, chars 0-2072` — so
        # both the model and the user can see the assumption.
        #
        # Only when the path RESOLVES, though: an invented id must still come
        # back as "nothing was read", or a hallucinated filename would quietly
        # open whatever happened to be nearby.
        whole = rid[2:]
        if whole and "/" not in whole[:1]:
            # Straight through: resolve_in_root then reports what is REALLY
            # wrong — "file not found: <root>/x.pdf" plus the wrong-root hint —
            # instead of "not a complete id", which sent the model chasing the
            # id when the path was the problem (observed).
            if rid.startswith("p:"):
                read_pdf(root, f"{rid}:1", whole, 1, offset, offset_given,
                         bool(given), export)
            else:
                read_file(root, f"{rid}:1", whole, 1, offset, offset_given,
                          bool(given), export)
            return
    fail_bad_id(rid)


if __name__ == "__main__":
    main()
