#!/usr/bin/env python3
"""Markdown → HTML for outgoing mail (connectors plugin, ``markdown_html``).

Born from a live mail (2026-11-05, catasto mailbox): a cave sheet written in
Markdown arrived as ``**Numero catastale:** LI191``. Pinned here, all pure:

  * what counts as Markdown: one structural signal is enough, prose with none
    (a signature, a URL, an underscore in a file name) stays prose;
  * the Markdown models write renders: emphasis, headings, code (fenced and
    inline), tables with alignment, quotes, rules, task lists, ordered lists
    keeping their start, and lists nested at a 2-space indent — the one thing
    Python-Markdown gets wrong and the reason this module exists;
  * a single newline is a line break (as in chat, as a signature expects);
  * nothing the model wrote becomes markup it did not mean: raw HTML is
    escaped, a link is a link only for http(s)/mailto/tel, ``5*3*2`` and
    ``file_name_v2`` stay text, a bare URL loses the punctuation after it;
  * ``document()`` is a complete page with the charset declared.

Run:  server/.venv/bin/python tests/test_markdown_html.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "connectors" / "plugin"))

from myagent_connectors import markdown_html as md  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def test_detection():
    print("What is Markdown:")
    yes = [
        "### Descrizione\nUn breve scivolo.",
        "La scheda della **Grotta della Redarca**.",
        "Dati:\n- uno\n- due",
        "1. primo\n2. secondo",
        "Vedi [la scheda](https://example.org/1).",
        "Il file `rilievo.pdf`.",
        "```\ncodice\n```",
        "> citato",
        "| a | b |\n|---|---|\n| 1 | 2 |",
    ]
    no = [
        "Ciao Alessandro,\n\nquesta è una mail di test.\n\nAssistente del Curatore",
        "Scheda: https://www.catastogrotte.net/liguria/it/caves/view/1/",
        "Il file Grotta_della_Redarca-ingresso.jpg è la foto.",
        "Un *solo* corsivo non basta.",
        "nel\n2015. fu svuotato",
        "Saluti\n-- \nFirma",
        "",
    ]
    for t in yes:
        check(md.looks_like_markdown(t), f"markdown: {t[:40]!r}")
    for t in no:
        check(not md.looks_like_markdown(t), f"prose:    {t[:40]!r}")


def test_render():
    print("Rendering:")
    out = md.render("La scheda della **Grotta** e il *corsivo*, ~~vecchio~~.")
    check("<strong>Grotta</strong>" in out and "<em>corsivo</em>" in out
          and "<del>vecchio</del>" in out, "emphasis, strong, strikethrough")
    check(md.render("### Descrizione").startswith("<h3>Descrizione</h3>"), "ATX heading")

    # The mail that started it: a 2-space nested list under a bold label.
    out = md.render("- **Quota:** 253 m  \n- **Coordinate:**  \n  - 44°04′35.0″ N\n"
                    "  - 9°55′53.4″ E\n- **Andamento:** orizzontale")
    check(out.count("<ul") == 2 and out.index("<ul", 1) < out.index("44°"),
          "a list nested at 2 spaces nests")
    check(out.rstrip().endswith("</ul>") and "orizzontale</li></ul>" in out,
          "…and the outer list resumes after it")
    check("253 m</li>" in out and "<strong>Coordinate:</strong>\n<ul" in out,
          "hard-break trailing spaces are dropped")

    out = md.render("1. uno\n   - sotto\n2. due")
    check("<ol" in out and "<ul" in out and out.index("<ul") < out.index("due"),
          "bullets nested in an ordered list")
    check('start="3"' in md.render("3. tre\n4. quattro"), "an ordered list keeps its start")
    out = md.render("- [ ] da fare\n- [x] fatto")
    check("☐ da fare" in out and "☑ fatto" in out, "task list boxes")
    out = md.render("- a\n\n- b")
    check(out.count("<p>") == 2, "blank lines between items make a loose list (paragraphs)")
    out = md.render("- a\n\n  secondo paragrafo\n- b")
    check(out.count("<p>") == 3, "two blocks in one item make the list loose too")

    out = md.render("| Grotta | Sviluppo | Quota |\n|:---|---:|:---:|\n"
                    "| **Redarca** | 180 m | 253 |\n| `a|b` | x \\| y |")
    check("<table" in out and out.count("<tr>") == 3, "GFM table: header + 2 rows")
    check("text-align:right" in out and "text-align:center" in out, "column alignment")
    check("<strong>Redarca</strong>" in out, "inline markup inside a cell")
    check(">a|b</code>" in out and "x | y" in out,
          "a pipe inside code or escaped does not split the cell")

    out = md.render("```python\nprint('<x>')\n  indented\n```\nDopo.")
    check("<pre" in out and "&lt;x&gt;" in out and "\n  indented" in out,
          "fenced code: escaped, indentation kept")
    check(out.rstrip().endswith("<p>Dopo.</p>"), "the paragraph after the fence")
    check("<code" in md.render("il file `a_b_c.txt`") and "a_b_c.txt" in md.render("`a_b_c.txt`"),
          "inline code")

    out = md.render("> citazione **forte**\n> seconda riga\n\n---")
    check("<blockquote" in out and "<strong>forte</strong>" in out and "<hr" in out,
          "block quote with markup inside, then a rule")

    out = md.render("Ciao Alessandro,\nriga due\n\nAssistente")
    check(out == "<p>Ciao Alessandro,<br>\nriga due</p>\n<p>Assistente</p>",
          f"a single newline is a line break, a blank line a paragraph: {out!r}")
    out = md.render("Dati:\n- uno\n- due")
    check(out.startswith("<p>Dati:</p>") and "<ul" in out,
          "a list interrupts the paragraph above it (no blank line needed)")


def test_links_and_safety():
    print("Links and safety:")
    out = md.render("Vedi [la scheda](https://www.catastogrotte.net/liguria/it/caves/view/193/).")
    check('<a href="https://www.catastogrotte.net/liguria/it/caves/view/193/">la scheda</a>' in out,
          "inline link")
    out = md.render("PDF: https://x.it/view/1/?export_pdf=1&format=pdf.")
    check('href="https://x.it/view/1/?export_pdf=1&amp;format=pdf"' in out
          and "</a>." in out, "bare URL linked, & escaped, final period left outside")
    out = md.render("(https://en.wikipedia.org/wiki/Foo_(bar)) e **https://y.it/z**")
    check('href="https://en.wikipedia.org/wiki/Foo_(bar)"' in out
          and '<strong><a href="https://y.it/z">' in out,
          "balanced parentheses kept, bold around a URL stays bold")
    check('href="mailto:info@example.org"' in md.render("Scrivi a <info@example.org>."),
          "autolinked address")

    out = md.render("<script>alert(1)</script> <b>x</b> & ok")
    check("<script>" not in out and "&lt;script&gt;" in out and "&amp; ok" in out,
          "raw HTML is escaped")
    out = md.render("[clic](javascript:alert(1)) ![i](javascript:x) [ok](ftp://x)")
    check("href" not in out and "src" not in out and "clic" in out,
          "a non-http(s)/mailto/tel link keeps only its text")
    out = md.render("![ingresso](https://x.it/a.jpg)")
    check('<img src="https://x.it/a.jpg" alt="ingresso"' in out, "remote image")
    out = md.render("file_name_v2.txt, 5*3*2, \\*letterale\\*")
    check("<em>" not in out and "*letterale*" in out,
          "no emphasis inside words, backslash escapes are literal")
    check('href="https://www.example.org"' in md.render("vai su www.example.org"),
          "www. gets a scheme")


def test_plain_and_document():
    print("Prose and document:")
    out = md.plain("Ciao <tu>,\nvedi https://x.it/a.\n\nFirma **non** markup")
    check(out == '<p>Ciao &lt;tu&gt;,<br>\nvedi <a href="https://x.it/a">https://x.it/a</a>.</p>\n'
                 "<p>Firma **non** markup</p>",
          f"plain(): escaped, line breaks, URLs, no Markdown: {out!r}")
    doc = md.document("<p>x</p>")
    check(doc.startswith("<!DOCTYPE html>") and 'charset="utf-8"' in doc
          and "<body" in doc and "<p>x</p>" in doc, "document() wraps a full page")
    check(md.render("") == "" and md.plain("") == "", "empty in, empty out")


test_detection()
test_render()
test_links_and_safety()
test_plain_and_document()

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
