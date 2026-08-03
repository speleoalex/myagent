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

Reads ``{"id": str, "offset"?: int, "path"?: str}`` as JSON on stdin.
``offset`` is a character offset into the extracted plain text (extraction is
deterministic, so offsets stay valid across calls). ``path`` must be the same
root passed to local_search, if any.

NOTE: the helpers marked "duplicated" are copied from
../local_search/search.py — keep them in sync (CoW overrides copy one
folder, so the two tools cannot share a module).
"""
import json
import os
import re
import sys
from html.parser import HTMLParser

PAGE = 8000                    # chars per page, cut back to a word boundary
MAX_TEXT_BYTES = 3_000_000     # refuse text files larger than this

BLOCK_TAGS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "ul", "ol", "table", "blockquote"}
SKIP_TAGS = {"script", "style", "head", "sup", "table"}  # sup = ref markers


# --------------------------------------------------------------------------- #
# HTML -> plain text — duplicated from local_search
# --------------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    """Collect visible text from article HTML, dropping scripts/refs/tables."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
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


def html_to_text(html):
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.text()


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
    fail(f"'{rid}' is not a complete id.{hint} Copy an id from the local_search "
         f"output verbatim, colon included, and call local_read again now.")


# --------------------------------------------------------------------------- #
# The two id branches
# --------------------------------------------------------------------------- #
def read_zim(root, rid, n, entry_path, offset):
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
        html = bytes(entry.get_item().content).decode("utf-8", "replace")
        render(rid, entry.title, os.path.basename(zim_path),
               html_to_text(html), offset)
        return
    fail(f"entry '{entry_path}' not found in any ZIM archive under {root}")


def read_file(root, rid, rel, line, offset, offset_given):
    if os.path.isfile(root):
        target = root
    else:
        # The id comes from the MODEL, which can invent one, so a fabricated
        # "../../etc/passwd" must never resolve. The check is LEXICAL and not
        # realpath-based: a library is normally ASSEMBLED from symlinks (see
        # _walk), so a file local_search just offered legitimately resolves
        # outside the root — a realpath containment check rejected exactly the
        # ids the search had produced ("escapes the library root" on every note
        # under a symlinked folder). Traversal is what we block; a symlink the
        # library owner installed is topology, not model input.
        rel = os.path.normpath(rel).replace(os.sep, "/")
        if os.path.isabs(rel) or rel == ".." or rel.startswith("../"):
            fail(f"path in id escapes the library root: {rel}")
        target = os.path.normpath(os.path.join(root, rel))
    if not os.path.isfile(target):
        fail(f"file not found: {target}")
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

    root = os.path.expanduser((args.get("path") or "").strip() or default_library_dir())
    if not os.path.exists(root):
        fail(f"library path not found: {root}")

    m = re.match(r"z(\d+):(.+)$", rid)
    if m:
        read_zim(root, rid, int(m.group(1)), m.group(2), offset)
        return
    if rid.startswith("f:"):
        rel, sep, line_s = rid[2:].rpartition(":")
        if sep and line_s.isdigit():
            read_file(root, rid, rel, int(line_s), offset, offset_given)
            return
    fail_bad_id(rid)


if __name__ == "__main__":
    main()
