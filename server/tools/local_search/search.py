#!/usr/bin/env python3
"""local_search tool.

Searches a LOCAL folder of knowledge files and returns the most relevant text
(plain text) on stdout. Two kinds of files are supported side by side:

  * Wikipedia / ZIM archives (``*.zim``) — queried through libzim's built-in
    full-text index (title suggestions + relevance-ranked full text).
  * Plain-text / Markdown files (``.md``, ``.markdown``, ``.txt``, ``.rst`` …)
    — read from disk and searched with a simple keyword scorer, returning the
    best-matching sections as snippets.

Reads ``{"query": str, "path"?: str, "lang"?: str, "limit"?: int}`` as JSON on
stdin. ``path`` may be a directory (scanned recursively) or a single file; it
defaults to ``<MYAGENT_LIBRARY or ~/myagent/library>``. Relative paths
resolve against the tool's working directory (the agent workspace), so each
agent can be pointed at its own personal knowledge folder.
"""
import glob
import json
import os
import re
import sys
from html.parser import HTMLParser

MAX_TOTAL = 13000              # keep under tool.json max_output (14000)
TEXT_EXTS = {".md", ".markdown", ".mdown", ".mkd", ".txt", ".text", ".rst"}
MAX_TEXT_BYTES = 3_000_000     # skip individual text files larger than this
MAX_TEXT_FILES = 3000          # cap how many text files we open in one scan
MAX_ZIM = 6                    # cap how many ZIM archives we search
SNIPPET_CHARS = 900            # max chars of a single text snippet before scoring trim

BLOCK_TAGS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "ul", "ol", "table", "blockquote"}
SKIP_TAGS = {"script", "style", "head", "sup", "table"}  # sup = ref markers


# --------------------------------------------------------------------------- #
# HTML -> plain text (used for ZIM article bodies)
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


# --------------------------------------------------------------------------- #
# Query scoring shared by text search
# --------------------------------------------------------------------------- #
def parse_query(query):
    """Return (terms, phrase): distinct lowercase tokens (len>=2) + full phrase."""
    phrase = query.strip().lower()
    seen, terms = set(), []
    for tok in re.split(r"[^0-9a-zA-Zà-öø-ÿ]+", phrase):
        if len(tok) >= 2 and tok not in seen:
            seen.add(tok)
            terms.append(tok)
    if not terms and phrase:          # single short token (e.g. a language code)
        terms = [phrase]
    return terms, phrase


def score_text(hay, terms, phrase):
    """Keyword relevance of a piece of text against the parsed query."""
    low = hay.lower()
    score = 0
    present = 0
    for t in terms:
        c = low.count(t)
        if c:
            present += 1
            score += c
    if terms and present == len(terms):
        score += 3                    # bonus: every query term appears
    if phrase and len(phrase) > 2 and phrase in low:
        score += 10                   # bonus: exact phrase match
    return score


# --------------------------------------------------------------------------- #
# Text / Markdown files
# --------------------------------------------------------------------------- #
def chunk_document(text):
    """Split a document into (heading, body, line_no) chunks.

    Markdown headings become the running context for the paragraphs beneath
    them; paragraphs are separated by blank lines. Works fine on plain text
    (which simply has no headings).
    """
    chunks = []
    heading = ""
    buf, buf_line = [], 0

    def flush():
        nonlocal buf, buf_line
        if buf:
            body = "\n".join(buf).strip()
            if body:
                chunks.append((heading, body, buf_line))
        buf = []

    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s{0,3}(#{1,6})\s+(.*)", line)
        if m:
            flush()
            heading = m.group(2).strip()
            continue
        if not line.strip():
            flush()
        else:
            if not buf:
                buf_line = i
            buf.append(line)
    flush()
    return chunks


def read_text_file(path):
    try:
        if os.path.getsize(path) > MAX_TEXT_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def search_text_file(path, rel, terms, phrase):
    """Return this file's matching chunks as result dicts (best first)."""
    text = read_text_file(path)
    if not text:
        return []
    results = []
    for heading, body, line_no in chunk_document(text):
        hay = f"{heading}\n{body}" if heading else body
        s = score_text(hay, terms, phrase)
        if s <= 0:
            continue
        title = rel + (f" › {heading}" if heading else f" (line {line_no})")
        results.append({"score": s, "title": title, "body": body})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def collect_text_files(root):
    """Recursively gather candidate text files under *root* (a directory)."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # skip hidden directories (.git, .cache, …)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in TEXT_EXTS:
                files.append(os.path.join(dirpath, name))
                if len(files) >= MAX_TEXT_FILES:
                    return files
    return files


# --------------------------------------------------------------------------- #
# ZIM archives
# --------------------------------------------------------------------------- #
def zim_matches_lang(fname, lang):
    tokens = re.split(r"[^a-z0-9]+", fname.lower())
    return lang in tokens


def search_zim(zim_path, query, limit):
    """Return up to *limit* article result dicts from one ZIM archive."""
    from libzim.reader import Archive
    from libzim.search import Query, Searcher

    archive = Archive(zim_path)

    # Title suggestions rank the canonical article first (e.g. "Leonardo da
    # Vinci" -> the biography), blended ahead of relevance-ranked full text.
    paths, seen = [], set()
    try:
        from libzim.suggestion import SuggestionSearcher
        for p in SuggestionSearcher(archive).suggest(query).getResults(0, limit):
            if p not in seen:
                seen.add(p)
                paths.append(p)
    except Exception:
        pass  # some archives lack a title index; full text below still works

    try:
        searcher = Searcher(archive)
        search = searcher.search(Query().set_query(query))
        for p in search.getResults(0, limit * 2):
            if len(paths) >= limit:
                break
            if p not in seen:
                seen.add(p)
                paths.append(p)
    except Exception:
        pass

    label = os.path.basename(zim_path)
    results = []
    for path in paths[:limit]:
        try:
            entry = archive.get_entry_by_path(path)
            title = entry.title
            html = bytes(entry.get_item().content).decode("utf-8", "replace")
            body = html_to_text(html)
        except Exception as e:
            title, body = path, f"(could not read entry: {e})"
        results.append({"title": f"{title}  [{label}]", "body": body})
    return results


# --------------------------------------------------------------------------- #
# Merge + render
# --------------------------------------------------------------------------- #
def round_robin(buckets, limit):
    """Interleave non-empty buckets one item at a time, up to *limit*."""
    active = [list(b) for b in buckets if b]
    out = []
    while active and len(out) < limit:
        nxt = []
        for b in active:
            if len(out) >= limit:
                break
            out.append(b.pop(0))
            if b:
                nxt.append(b)
        active = nxt
    return out


def default_library_dir():
    return os.environ.get("MYAGENT_LIBRARY") or os.path.join(
        os.path.expanduser("~"), "myagent", "library")


def main():
    try:
        args = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    query = (args.get("query") or "").strip()
    if not query:
        print("ERROR: 'query' is required", file=sys.stderr)
        sys.exit(1)

    root = os.path.expanduser((args.get("path") or "").strip() or default_library_dir())
    if not os.path.exists(root):
        print(f"ERROR: search path not found: {root}", file=sys.stderr)
        sys.exit(1)

    lang = (args.get("lang") or "").strip().lower()

    try:
        limit = int(args.get("limit") or 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(1, min(limit, 8))

    # Gather the files to search.
    if os.path.isfile(root):
        zim_files = [root] if root.lower().endswith(".zim") else []
        text_files = [root] if os.path.splitext(root)[1].lower() in TEXT_EXTS else []
        text_root = os.path.dirname(root) or "."
    else:
        zim_files = sorted(glob.glob(os.path.join(root, "*.zim")))
        text_files = collect_text_files(root)
        text_root = root

    # ZIM: optionally restrict to a language edition.
    if lang and zim_files:
        matched = [z for z in zim_files if zim_matches_lang(os.path.basename(z), lang)]
        if matched:
            zim_files = matched
    zim_files = zim_files[:MAX_ZIM]

    if not zim_files and not text_files:
        print(f"No searchable files in {root} "
              f"(supported: *.zim, {', '.join(sorted(TEXT_EXTS))}).")
        return

    terms, phrase = parse_query(query)

    # One bucket per ZIM archive.
    buckets = []
    for z in zim_files:
        try:
            hits = search_zim(z, query, limit)
        except ImportError as e:
            print(f"ERROR: libzim not installed in this interpreter "
                  f"({sys.executable}): {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"WARNING: could not search {os.path.basename(z)}: {e}",
                  file=sys.stderr)
            hits = []
        if hits:
            buckets.append(hits)

    # One combined text bucket, kept diverse by round-robining across files
    # (ordered by each file's best-matching chunk).
    per_file = []
    for fp in text_files:
        rel = os.path.relpath(fp, text_root) if os.path.isdir(root) else os.path.basename(fp)
        hits = search_text_file(fp, rel, terms, phrase)
        if hits:
            per_file.append(hits)
    per_file.sort(key=lambda hits: hits[0]["score"], reverse=True)
    text_bucket = round_robin(per_file, limit)
    if text_bucket:
        buckets.append(text_bucket)

    results = round_robin(buckets, limit)
    if not results:
        scanned = len(zim_files) + len(text_files)
        print(f"No matches for \"{query}\" in {root} ({scanned} file(s) searched).")
        return

    per_item = max(600, MAX_TOTAL // len(results))
    header = (f"Local search in {root} — {len(results)} result(s) for \"{query}\":\n")
    out = [header]
    used = len(header)
    for i, r in enumerate(results, 1):
        body = r["body"]
        if len(body) > per_item:
            body = body[:per_item].rsplit(" ", 1)[0] + " …[truncated]"
        block = f"\n=== [{i}] {r['title']} ===\n{body}\n"
        if used + len(block) > MAX_TOTAL and i > 1:
            out.append(f"\n…[{len(results) - i + 1} more result(s) omitted to "
                       f"stay within size limit]")
            break
        out.append(block)
        used += len(block)

    print("".join(out).rstrip())


if __name__ == "__main__":
    main()
