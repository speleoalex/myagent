#!/usr/bin/env python3
"""local_search tool — phase 1 of the two-phase library workflow.

Searches a LOCAL folder of knowledge files and prints COMPACT results only:
one line per hit, ``id | title | snippet`` (~200 chars around the best match).
The full text is fetched on demand with the companion ``local_read`` tool,
passing the ``id`` verbatim. Two kinds of files are supported side by side:

  * Wikipedia / ZIM archives (``*.zim``) — queried through libzim's built-in
    full-text index (title suggestions + relevance-ranked full text).
    Result ids look like ``z1:A/Photosynthesis`` where ``1`` is the archive's
    1-based position in the sorted recursive listing of ``*.zim`` files under
    the search root (the same ordering ``local_read`` reconstructs).
  * Plain-text / Markdown files (``.md``, ``.txt``, ``.rst`` …) — read from
    disk and scored with a simple keyword scorer, one chunk per paragraph.
    Result ids look like ``f:<relpath>:<line>``.

Reads ``{"query": str, "path"?: str, "lang"?: str, "limit"?: int}`` as JSON on
stdin. ``path`` may be a directory (scanned recursively) or a single file; it
defaults to ``<MYAGENT_LIBRARY or ~/myagent/library>``. Relative paths
resolve against the tool's working directory (the agent workspace), so each
agent can be pointed at its own personal knowledge folder. Ids only resolve
against the same root they were produced from.

NOTE: the helpers below marked "duplicated" are copied in
../local_read/read.py — keep them in sync (CoW overrides copy one folder,
so the two tools cannot share a module).
"""
import json
import os
import re
import sys
from html.parser import HTMLParser

MAX_TOTAL = 2000               # phase-1 budget: compact lines only
TEXT_EXTS = {".md", ".markdown", ".mdown", ".mkd", ".txt", ".text", ".rst"}
MAX_TEXT_BYTES = 3_000_000     # skip individual text files larger than this
MAX_TEXT_FILES = 3000          # cap how many text files we open in one scan
MAX_ZIM = 6                    # cap how many ZIM archives we search
SNIPPET_WIDTH = 200            # chars of context around the best match
MAX_RELAXED_QUERIES = 4        # leave-one-out retries when a query is too strict


BLOCK_TAGS = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "ul", "ol", "table", "blockquote"}
SKIP_TAGS = {"script", "style", "head", "sup", "table"}  # sup = ref markers


# --------------------------------------------------------------------------- #
# HTML -> plain text (used for ZIM article bodies) — duplicated in local_read
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
    # duplicated in local_read
    return os.environ.get("MYAGENT_LIBRARY") or os.path.join(
        os.path.expanduser("~"), "myagent", "library")


def list_zims(root):
    """Sorted recursive listing of *.zim under *root* — duplicated in
    local_read. The sort order DEFINES the ``z<N>`` id indices, so both
    tools must list identically."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(".zim"):
                path = os.path.join(dirpath, name)
                found.append((os.path.relpath(path, root).replace(os.sep, "/"),
                              path))
    found.sort()
    return [path for _, path in found]


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


def best_window(text, terms, phrase, width=SNIPPET_WIDTH):
    """(snippet, distinct terms in it) for the window densest in matches.

    The count is the proximity signal callers rank on: an article that
    mentions every query term in ONE sentence answers the question, while
    one that scatters the same terms across unrelated paragraphs usually
    doesn't.

    Falls back to the head of the text when no term occurs (e.g. a ZIM hit
    that came from a title suggestion in another language).
    """
    def clean(chunk, start):
        snippet = " ".join(chunk.split())
        trimmed = False
        if len(snippet) > width:
            snippet = snippet[:width].rsplit(" ", 1)[0]
            trimmed = True
        pre = "…" if start > 0 else ""
        post = "…" if trimmed or start + len(chunk) < len(text) else ""
        return f"{pre}{snippet}{post}"

    low = text.lower()
    anchors = []                      # (weight, position)
    if phrase and len(phrase) > 2:
        i = low.find(phrase)
        while i >= 0:
            anchors.append((10, i))
            i = low.find(phrase, i + 1)
    for t in terms:
        i = low.find(t)
        while i >= 0:
            anchors.append((1, i))
            i = low.find(t, i + len(t))
    if not anchors:
        return clean(text[:width + 30], 0), 0

    # Sliding window over the sorted anchor positions: for each anchor as the
    # window start, sum the weights of anchors within [pos, pos + width).
    anchors.sort(key=lambda a: a[1])
    best_pos, best_score = anchors[0][1], -1
    j, total = 0, 0
    for i, (_, pos) in enumerate(anchors):
        if i > 0:
            total -= anchors[i - 1][0]
        while j < len(anchors) and anchors[j][1] < pos + width:
            total += anchors[j][0]
            j += 1
        if total > best_score:
            best_score, best_pos = total, pos

    start = max(0, best_pos - 30)     # a little left context
    if start > 0:                     # snap forward to a word boundary
        nxt = text.find(" ", start)
        if 0 <= nxt < best_pos:
            start = nxt + 1
    end = min(len(text), start + width + 30)
    window = low[start:end]
    covered = sum(1 for t in terms if t in window)
    return clean(text[start:end], start), covered


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
    """Return this file's matching chunks as result dicts (best first).

    A chunk qualifies when it carries at least half of the query terms:
    matching on one short word ("di", "the") turns every paragraph of a long
    document into a result. Chunks below that bar are kept aside and only
    used if nothing qualifies, so a loose query still returns something.
    """
    text = read_text_file(path)
    if not text:
        return []
    needed = max(1, (len(terms) + 1) // 2)
    strong, weak = [], []
    for heading, body, line_no in chunk_document(text):
        hay = f"{heading}\n{body}" if heading else body
        s = score_text(hay, terms, phrase)
        if s <= 0:
            continue
        low = hay.lower()
        present = sum(1 for t in terms if t in low)
        hit = {
            "score": s,
            "id": f"f:{rel}:{line_no}",
            "title": heading or os.path.basename(rel),
            "snippet": best_window(hay, terms, phrase)[0],
        }
        (strong if present >= needed else weak).append(hit)
    results = strong or weak
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


def relevance(title, text, terms, phrase):
    """How well one article answers the query — COVERAGE, not occurrences.

    Counting occurrences the way score_text does would rank by article
    length: a long page mentioning one query term repeatedly would beat the
    short page that mentions every one of them. What matters here is how
    many distinct terms the article covers, and whether the whole phrase
    lands in the title.
    """
    low_title, low_text = title.lower(), text.lower()
    score = 0
    for t in terms:
        if t in low_title:
            score += 3
        elif t in low_text:
            score += 1
    if phrase and len(phrase) > 2:
        if phrase in low_title:
            score += 6
        elif phrase in low_text:
            score += 2
    return score


def zim_query(archive, query, want):
    """(paths, estimated_matches) for one query: title suggestions, then
    full-text hits, deduped.

    Suggestions rank the canonical article first (e.g. "Leonardo da Vinci" ->
    the biography), blended ahead of relevance-ranked full text.
    """
    from libzim.search import Query, Searcher

    found, seen = [], set()
    try:
        from libzim.suggestion import SuggestionSearcher
        for p in SuggestionSearcher(archive).suggest(query).getResults(0, want):
            if p not in seen:
                seen.add(p)
                found.append(p)
    except Exception:
        pass  # some archives lack a title index; full text below still works

    estimated = 0
    try:
        search = Searcher(archive).search(Query().set_query(query))
        estimated = search.getEstimatedMatches()
        for p in search.getResults(0, want * 2):
            if p not in seen:
                seen.add(p)
                found.append(p)
    except Exception:
        pass
    return found, estimated


def search_zim(zim_path, zim_idx, query, terms, phrase, limit):
    """Return up to *limit* article result dicts from one ZIM archive.

    *zim_idx* is the archive's 1-based position in list_zims(root) and is
    baked into each result id so local_read can reopen the same archive.
    Extracting the full article text per hit is accepted cost: the archive
    is mmap'd, and a future semantic index would cache extractions instead.
    """
    from libzim.reader import Archive

    archive = Archive(zim_path)
    paths, seen = [], set()
    for p in zim_query(archive, query, limit)[0]:
        seen.add(p)
        paths.append(p)

    # A thin result set usually means the query was too strict, not that the
    # library is empty: the full-text index ANDs every term, and one common
    # word can veto the right article outright. Measured on the Italian
    # Wikipedia ZIM: "effetto fotoelettrico Nobel" returns 2 articles and
    # NOT Albert Einstein, whose text reads "legge dell'effetto
    # fotoelettrico" — Xapian keeps the apostrophe inside the token, so
    # "effetto" never matches it. Dropping "effetto" surfaces him at rank 4.
    # So retry leave-one-out, keeping the full query's own hits first. Only
    # when results are thin, which is also when the extra searches are
    # cheapest.
    #
    # The retries are consumed FEWEST-MATCHES-FIRST, not round-robin: the
    # useful retry is the one that dropped the noisy term, and it stays
    # narrow ("fotoelettrico Nobel", 5 matches, Einstein among them), while
    # dropping the rare term leaves something generic ("effetto Nobel",
    # which offers the Nobel prize in literature). Interleaving gave the two
    # equal weight and spent the budget on the noise.
    if len(paths) < limit and len(terms) >= 2:
        retries = []
        for dropped in range(min(len(terms), MAX_RELAXED_QUERIES)):
            sub = " ".join(t for i, t in enumerate(terms) if i != dropped)
            found, estimated = zim_query(archive, sub, limit)
            found = [p for p in found if p not in seen]
            if found:
                retries.append((estimated or len(found), found))
        retries.sort(key=lambda r: r[0])
        for _, found in retries:
            for p in found:
                if len(paths) >= limit:
                    break
                if p not in seen:
                    seen.add(p)
                    paths.append(p)

    results = []
    for rank, path in enumerate(paths[:limit]):
        near = 0
        try:
            entry = archive.get_entry_by_path(path)
            title = entry.title
            html = bytes(entry.get_item().content).decode("utf-8", "replace")
            text = html_to_text(html)
            snippet, near = best_window(text, terms, phrase)
        except Exception as e:
            title, text, snippet = path, "", f"(could not read entry: {e})"
        score = relevance(title, text, terms, phrase) + 2 * near
        results.append({"id": f"z{zim_idx}:{path}", "title": title,
                        "snippet": snippet, "rank": (-score, rank)})
    # Re-rank on the text we already had to extract for the snippet. Xapian
    # orders by its own relevance and the relaxed retries append after it, so
    # the article that actually answers can land last: the user's
    # "effetto fotoelettrico Nobel" put Owen Richardson (one incidental
    # "effetto") first and Einstein — whose line names both the prize and the
    # law — fifth. Ties keep the original order, so a title suggestion still
    # wins when nothing distinguishes the candidates.
    results.sort(key=lambda r: r["rank"])
    for r in results:
        del r["rank"]
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
        limit = int(args.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 8))

    # Gather the files to search. ZIM ids must carry the archive's position
    # in the FULL sorted listing (before lang filtering), or local_read —
    # which never sees 'lang' — would reconstruct different indices.
    if os.path.isfile(root):
        zim_files = [(1, root)] if root.lower().endswith(".zim") else []
        text_files = [root] if os.path.splitext(root)[1].lower() in TEXT_EXTS else []
        text_root = os.path.dirname(root) or "."
    else:
        zim_files = list(enumerate(list_zims(root), 1))
        text_files = collect_text_files(root)
        text_root = root

    # ZIM: optionally restrict to a language edition.
    if lang and zim_files:
        matched = [(i, z) for i, z in zim_files
                   if zim_matches_lang(os.path.basename(z), lang)]
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
    for idx, z in zim_files:
        try:
            hits = search_zim(z, idx, query, terms, phrase, limit)
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
        rel = rel.replace(os.sep, "/")
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

    # Compact rendering: one "id | title | snippet" line per result, total
    # under MAX_TOTAL so a whole search costs the model a few hundred tokens.
    header = f"{len(results)} result(s) for \"{query}\":"
    footer = "Read one with local_read(id)."
    per_line = max(140, (MAX_TOTAL - len(header) - len(footer) - 2) // len(results))
    lines = [header]
    for r in results:
        line = f"{r['id']} | {r['title']} | {r['snippet']}"
        if len(line) > per_line:
            line = line[:per_line - 1].rstrip() + "…"
        lines.append(line)
    lines.append(footer)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
