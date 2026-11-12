"""Markdown → HTML for outgoing mail. Standard library only.

Why it exists. Models write their answers in Markdown — every chat UI renders
it — and a mail client shows it as source. Observed 2026-11-05 on the catasto
mailbox: a whole cave sheet arrived as ``**Numero catastale:** LI191``, dashes
and ``###``. The mail channel now sends such a body as multipart/alternative:
the Markdown source as text/plain (Markdown is designed to read as plain text)
and this module's rendering as text/html.

Why not a library. The mail channel promises "standard library only" (nothing
to install, works offline), and the obvious candidate gets wrong the one thing
that matters most for model output: Python-Markdown nests a list only under a
4-space indent, while models indent sub-items by 2 — the coordinates under
"**Coordinate:**" in that very mail would have come out flat. So this renders
the Markdown models actually write — headings, emphasis, code, links, GFM
tables, block quotes, lists nested at any indent — and is NOT a CommonMark
implementation. Deliberate differences: a single newline is a line break (as
in chat, and as a mail signature expects), indented code blocks do not exist
(models fence their code; an indented paragraph is just a paragraph), and a
line that is not indented ends the list above it.

Safety: everything that is not markup is escaped, raw HTML included (a body
that IS HTML is recognised by the channel and sent as is, it never comes
through here), and a link only becomes a link for http(s), mailto and tel.
Styles are inline because several mail clients drop ``<style>`` blocks.
"""
from __future__ import annotations

import html
import re

__all__ = ["looks_like_markdown", "render", "plain", "document"]

# ---------------------------------------------------------------- styles
_FONT_MONO = "Consolas,Menlo,'Courier New',monospace"
_S_BODY = "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5"
_S_TABLE = "border-collapse:collapse;margin:0.5em 0 1em"
_S_CELL = "border:1px solid #d0d0d0;padding:4px 8px;vertical-align:top"
_S_TH = _S_CELL + ";background:#f2f2f2"
_S_PRE = ("background:#f5f5f5;border:1px solid #e0e0e0;border-radius:4px;"
          f"padding:8px 10px;font-family:{_FONT_MONO};font-size:13px;"
          "line-height:1.4;white-space:pre-wrap")
_S_CODE = (f"background:#f2f2f2;border-radius:3px;padding:0 3px;"
           f"font-family:{_FONT_MONO};font-size:90%")
_S_QUOTE = "margin:0.5em 0;padding:0 0 0 12px;border-left:3px solid #d0d0d0;color:#555"
_S_HR = "border:0;border-top:1px solid #d0d0d0;margin:1em 0"
_S_LIST = "margin:0.3em 0 0.8em;padding-left:1.6em"
_S_IMG = "max-width:100%;height:auto"

# ------------------------------------------------------------- block syntax
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE = re.compile(r"^ {0,3}> ?")
_ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])[ \t]+(\S.*)$")
_TASK = re.compile(r"^\[([ xX])\][ \t]+")
_SEP_CELL = re.compile(r"^\s*(:?)-+(:?)\s*$")

# What makes a body Markdown rather than prose: see looks_like_markdown().
_SIGNALS = [
    re.compile(r"^ {0,3}#{1,6}[ \t]+\S", re.M),                      # heading
    re.compile(r"^ {0,3}(?:`{3,}|~{3,})", re.M),                      # fence
    re.compile(r"^ *(?:[-*+]|\d{1,3}[.)])[ \t]+\S", re.M),            # list item
    re.compile(r"^ {0,3}>[ \t]?\S", re.M),                            # quote
    re.compile(r"\*\*[^\s*](?:[^*\n]*[^\s*])?\*\*"),                  # **strong**
    re.compile(r"(?<![\w_])__[^\s_](?:[^_\n]*[^\s_])?__(?![\w_])"),   # __strong__
    re.compile(r"`[^`\n]+`"),                                          # `code`
    re.compile(r"\[[^\]\n]+\]\([^)\s]+\)"),                            # [link](url)
    re.compile(r"~~[^\s~][^~\n]*~~"),                                  # ~~strike~~
]

# ------------------------------------------------------------ inline syntax
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_BACKSLASH = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~<>\"'])")
_LABEL = r"((?:[^\[\]\n]|\[[^\[\]\n]*\])*)"
_TARGET = (r"\(\s*<?((?:[^\s()<>]|\([^\s()<>]*\))+)>?"
           r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
_IMAGE = re.compile(r"!\[" + _LABEL + r"\]" + _TARGET)
_LINK = re.compile(r"\[" + _LABEL + r"\]" + _TARGET)
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^\s<>]+)>", re.I)
_AUTOMAIL = re.compile(r"<([\w.+-]+@[\w-]+(?:\.[\w-]+)+)>")
_BARE_URL = re.compile(r"(?<![\w@/.:\-])((?:https?://|www\.)[^\s<>\"\x00]+)", re.I)
_SAFE_SCHEME = re.compile(r"^(?:https?://|mailto:|tel:)", re.I)
_EMPHASIS = [
    (re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"), r"<strong><em>\1</em></strong>"),
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])"), r"<strong>\1</strong>"),
    # Single * and _ never inside a word: 5*3*2 and file_name_v2 stay text.
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"), r"<em>\1</em>"),
    (re.compile(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])"), r"<em>\1</em>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"<del>\1</del>"),
]
_TOKEN = re.compile(r"\x00(\d+)\x00")


# ------------------------------------------------------------------ public
def looks_like_markdown(text: str) -> bool:
    """Whether an outgoing body uses Markdown syntax.

    Any one structural signal is enough — a heading, a list, a fence, a quote,
    a table, ``**strong**``, ``code``, a ``[link](url)``. A lone ``*word*`` is
    not: too common in prose to be worth turning a mail into HTML. Prose with
    none of them stays a single text/plain part, as it always was."""
    text = text or ""
    if any(p.search(text) for p in _SIGNALS):
        return True
    lines = text.split("\n")
    return any(_is_table(lines, i) for i in range(len(lines) - 1))


def render(text: str) -> str:
    """Markdown → an HTML fragment (no ``<html>``/``<body>``)."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return _blocks([ln.expandtabs(4) for ln in lines.split("\n")])


def plain(text: str) -> str:
    """Prose → an HTML fragment: escaped, paragraphs on blank lines, line
    breaks kept, bare URLs made links. For the parts of a mail that are not
    Markdown, when another part of the same mail is."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    out = []
    for para in re.split(r"\n[ \t]*\n", text.strip()):
        if para.strip():
            out.append("<p>" + _inline(para.strip(), markup=False).replace("\n", "<br>\n")
                       + "</p>")
    return "\n".join(out)


def document(fragment: str) -> str:
    """A fragment wrapped as the complete HTML document a text/html part is."""
    return ('<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'</head>\n<body style="{_S_BODY}">\n{fragment}\n</body></html>\n')


# ------------------------------------------------------------------ blocks
def _starts_block(lines: list[str], i: int) -> bool:
    """Whether line ``i`` opens a block, so it ends the paragraph above."""
    line = lines[i]
    if _FENCE.match(line) or _HEADING.match(line) or _HR.match(line) or _QUOTE.match(line):
        return True
    m = _ITEM.match(line)
    # An ordered item interrupts a paragraph only when it starts at 1, as in
    # CommonMark: "nel\n2015. fu..." is a wrapped sentence, not a list.
    if m and (not m.group(2)[0].isdigit() or int(m.group(2)[:-1]) == 1):
        return True
    return _is_table(lines, i)


def _blocks(lines: list[str], tight: bool = False) -> str:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            mark = m.group(1)
            body: list[str] = []
            i += 1
            while i < n:
                close = lines[i].strip()
                if close and set(close) == {mark[0]} and len(close) >= len(mark):
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            out.append(f'<pre style="{_S_PRE}"><code>'
                       f'{html.escape(chr(10).join(body))}</code></pre>')
            continue
        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2) or '')}</h{level}>")
            i += 1
            continue
        if _HR.match(line):
            out.append(f'<hr style="{_S_HR}">')
            i += 1
            continue
        if _is_table(lines, i):
            i = _table(lines, i, out)
            continue
        if _QUOTE.match(line):
            inner = []
            while i < n and _QUOTE.match(lines[i]):
                inner.append(_QUOTE.sub("", lines[i], count=1))
                i += 1
            out.append(f'<blockquote style="{_S_QUOTE}">{_blocks(inner)}</blockquote>')
            continue
        if _ITEM.match(line):
            i = _list(lines, i, out)
            continue
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines, i):
            para.append(lines[i])
            i += 1
        # Trailing double space / backslash are Markdown's hard breaks; every
        # newline already is one here, so they are only noise to drop.
        text = "\n".join(re.sub(r"(?:[ \t]+|\\)$", "", p.strip()) for p in para)
        body_html = _inline(text).replace("\n", "<br>\n")
        out.append(body_html if tight else f"<p>{body_html}</p>")
    return "\n".join(out)


def _list(lines: list[str], i: int, out: list[str]) -> int:
    """One list starting at line ``i``; returns the first line after it.

    Indentation decides membership the way models write it, not by CommonMark's
    content-column rule: an item at the list's own indent (±1) is a sibling,
    anything indented 2 or more past it belongs to the item above — a nested
    list at 2 spaces nests, as it does in every chat UI."""
    n = len(lines)
    first = _ITEM.match(lines[i])
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    items: list[list[str]] = []
    loose = False

    def sibling(line: str) -> bool:
        m = _ITEM.match(line)
        return bool(m and len(m.group(1)) <= base + 1
                    and m.group(2)[0].isdigit() == ordered)

    while i < n and sibling(lines[i]):
        m = _ITEM.match(lines[i])
        offset = len(m.group(1)) + len(m.group(2)) + 1
        content = [m.group(3)]
        i += 1
        while i < n:
            line = lines[i]
            if line.strip():
                indent = len(line) - len(line.lstrip(" "))
                if indent < base + 2:
                    break
                content.append(line[min(indent, offset):])
                i += 1
                continue
            # A blank line: what follows decides whether the list goes on.
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and sibling(lines[j]):
                loose = True
                i = j
                break
            if j < n and len(lines[j]) - len(lines[j].lstrip(" ")) >= base + 2:
                # Two blocks in one item: rendered tight they would run
                # into each other, so the whole list gets its paragraphs.
                content.extend([""] * (j - i))
                loose = True
                i = j
                continue
            break
        items.append(content)
        if i < n and not sibling(lines[i]):
            break

    tag = "ol" if ordered else "ul"
    attrs = f' start="{start}"' if ordered and start != 1 else ""
    parts = []
    for content in items:
        box = ""
        t = _TASK.match(content[0])
        if t:
            box = "☑ " if t.group(1).lower() == "x" else "☐ "
            content = [content[0][t.end():]] + content[1:]
        parts.append(f"<li>{box}{_blocks(content, tight=not loose)}</li>")
    out.append(f'<{tag}{attrs} style="{_S_LIST}">' + "\n".join(parts) + f"</{tag}>")
    return i


# ------------------------------------------------------------------ tables
def _split_row(line: str) -> list[str]:
    """Cells of a table row: split on ``|`` outside code spans and not
    escaped; the outer pipes are optional."""
    cells, cur, in_code, k = [], [], False, 0
    s = line.strip()
    while k < len(s):
        c = s[k]
        if c == "\\" and k + 1 < len(s) and s[k + 1] == "|":
            cur.append("|")
            k += 2
            continue
        if c == "`":
            in_code = not in_code
        if c == "|" and not in_code:
            cells.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        k += 1
    cells.append("".join(cur))
    if s.startswith("|"):
        cells = cells[1:]
    if s.endswith("|") and not s.endswith("\\|") and cells:
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _is_table(lines: list[str], i: int) -> bool:
    """A row with pipes, then a separator row with as many cells."""
    if i + 1 >= len(lines) or "|" not in lines[i] or "|" not in lines[i + 1]:
        return False
    sep = _split_row(lines[i + 1])
    return (bool(sep) and all(_SEP_CELL.match(c) for c in sep)
            and len(sep) == len(_split_row(lines[i])))


def _table(lines: list[str], i: int, out: list[str]) -> int:
    header = _split_row(lines[i])
    aligns = []
    for c in _split_row(lines[i + 1]):
        m = _SEP_CELL.match(c)
        left, right = bool(m.group(1)), bool(m.group(2))
        aligns.append("center" if left and right else "right" if right else "")
    i += 2
    rows = []
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        rows.append(_split_row(lines[i]))
        i += 1

    def cell(tag: str, style: str, text: str, k: int) -> str:
        align = f";text-align:{aligns[k]}" if k < len(aligns) and aligns[k] else ""
        return f'<{tag} style="{style}{align}">{_inline(text)}</{tag}>'

    head = "".join(cell("th", _S_TH, h, k) for k, h in enumerate(header))
    body = "".join("<tr>" + "".join(cell("td", _S_CELL, row[k] if k < len(row) else "", k)
                                    for k in range(len(header))) + "</tr>\n"
                   for row in rows)
    out.append(f'<table style="{_S_TABLE}"><thead><tr>{head}</tr></thead>\n'
               f"<tbody>\n{body}</tbody></table>")
    return i


# ------------------------------------------------------------------ inline
def _href(url: str) -> str:
    if url.lower().startswith("www."):
        url = "https://" + url
    return url if _SAFE_SCHEME.match(url) else ""


def _anchor(url: str, inner: str) -> str:
    href = _href(url)
    if not href:
        return inner
    return f'<a href="{html.escape(href, quote=True)}">{inner}</a>'


def _split_url(url: str) -> tuple[str, str]:
    """A bare URL minus the punctuation that ends the sentence around it:
    "vedi https://x.it/a." links https://x.it/a, and "**https://x.it**"
    keeps its asterisks for the bold around it."""
    tail = ""
    while url:
        last = url[-1]
        if last in ".,;:!?'\"*_~":
            pass
        elif last == ")" and url.count("(") < url.count(")"):
            pass
        else:
            break
        tail = last + tail
        url = url[:-1]
    return url, tail


def _inline(text: str, markup: bool = True) -> str:
    """One run of text → HTML. What must not be touched by escaping or
    emphasis (code, links, URLs) is set aside as ``\\x00n\\x00`` tokens first and
    put back last."""
    stash: list[str] = []

    def put(s: str) -> str:
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    s = (text or "").replace("\x00", "")
    if markup:
        def code(m: re.Match) -> str:
            inner = m.group(2)
            if len(inner) > 1 and inner.startswith(" ") and inner.endswith(" ") and inner.strip():
                inner = inner[1:-1]
            return put(f'<code style="{_S_CODE}">{html.escape(inner)}</code>')

        def image(m: re.Match) -> str:
            alt, href = m.group(1), _href(m.group(2))
            if href.lower().startswith(("http://", "https://")):
                return put(f'<img src="{html.escape(href, quote=True)}" '
                           f'alt="{html.escape(alt, quote=True)}" style="{_S_IMG}">')
            return put(html.escape(alt))

        def link(m: re.Match) -> str:
            return put(_anchor(m.group(2), _inline(m.group(1)) or html.escape(m.group(2))))

        s = _CODE_SPAN.sub(code, s)
        s = _BACKSLASH.sub(lambda m: put(html.escape(m.group(1))), s)
        s = _IMAGE.sub(image, s)
        s = _LINK.sub(link, s)
        s = _AUTOLINK.sub(lambda m: put(_anchor(m.group(1), html.escape(m.group(1)))), s)
        s = _AUTOMAIL.sub(
            lambda m: put(_anchor("mailto:" + m.group(1), html.escape(m.group(1)))), s)

    def bare(m: re.Match) -> str:
        url, tail = _split_url(m.group(1))
        if not url:
            return m.group(0)
        return put(_anchor(url, html.escape(url))) + tail

    s = _BARE_URL.sub(bare, s)
    s = html.escape(s, quote=False)
    if markup:
        for pattern, repl in _EMPHASIS:
            s = pattern.sub(repl, s)
    return _TOKEN.sub(lambda m: stash[int(m.group(1))], s)
