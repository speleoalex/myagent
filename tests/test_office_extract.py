#!/usr/bin/env python3
"""Office (OOXML) extraction in the library tools.

Run: server/.venv/bin/python tests/test_office_extract.py
(No network, no third-party libs, no real user files: every fixture is a
minimal .docx/.xlsx/.pptx built here, so the assertions pin OUR parser rather
than whatever a particular Word version happened to write.)

The contract these tools depend on:
  1. .docx — paragraphs in document order, Heading styles become Markdown
     headings (chunk_document turns those into chunk titles, in results AND in
     the semantic index), table rows stay ONE line joined by CELL_SEP.
  2. .pptx — one "## Slide N" section per slide, slides in NUMERIC order:
     a lexical sort puts slide10 between slide1 and slide2, and every slide
     number printed from the tenth on would be wrong.
  3. .xlsx — "## Sheet: <name>" per sheet using the workbook's names and ORDER
     (not the sheetN.xml file order), shared + inline strings resolved, one line
     per row, date-formatted serials rendered ISO (custom format codes too, and
     a 'd' inside a quoted literal must NOT make a column a date).
  4. Namespace-agnostic: OOXML ships transitional AND strict, with different
     namespace URIs for the same elements. A parser keyed on the URI silently
     returns nothing for the other flavour.
  5. Caps are declared, a non-zip / corrupt file is None (never a crash), and a
     zip that claims a huge expansion is refused BEFORE extracting.
  6. THE LOCATOR INVARIANT: the line number local_search mints for an Office hit
     must be the line local_read opens. Both tools are exercised through their
     own module copy, so a divergence fails here as well as in
     test_library_helpers_sync.py.
"""

import io
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server" / "tools" / "library" / "local_search"))
sys.path.insert(0, str(ROOT / "server" / "tools" / "library" / "local_read"))

import search                                                  # noqa: E402
import read                                                     # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W_STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_R = "http://schemas.openxmlformats.org/package/2006/relationships"

TMP = Path(os.environ.get("TMPDIR", "/tmp")) / "myagent-office-test"
TMP.mkdir(parents=True, exist_ok=True)


def _zip(name: str, members: dict) -> str:
    path = TMP / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, body in members.items():
            zf.writestr(member, body)
    return str(path)


# --------------------------------------------------------------------------- #
# .docx
# --------------------------------------------------------------------------- #
def _para(text, style=None, ns=W):
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p xmlns:w="{ns}">{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>'


def docx_fixture(ns=W):
    body = (
        _para("Manuale VEVOR", style="Heading1", ns=ns)
        + _para("Il riscaldatore va installato fuori dall'abitacolo.", ns=ns)
        + _para("Coppie", style="Heading2", ns=ns)
        + f'<w:tbl xmlns:w="{ns}">'
          f'<w:tr><w:tc>{_para("Bullone", ns=ns)}</w:tc>'
          f'<w:tc>{_para("Coppia", ns=ns)}</w:tc></w:tr>'
          f'<w:tr><w:tc>{_para("Testata", ns=ns)}</w:tc>'
          f'<w:tc>{_para("15-22 Nm", ns=ns)}</w:tc></w:tr>'
          f'</w:tbl>'
        + f'<w:p xmlns:w="{ns}"><w:r><w:t>riga uno</w:t></w:r>'
          f'<w:r><w:tab/></w:r><w:r><w:t>riga due</w:t></w:r></w:p>'
    )
    doc = f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    return _zip(f"f-{'strict' if ns != W else 'ok'}.docx",
                {"word/document.xml": doc})


def test_docx_structure():
    text = search.office_text(docx_fixture())
    assert "# Manuale VEVOR" in text, text
    assert "## Coppie" in text, text
    # A table row must stay on ONE line: split per cell, "15-22 Nm" no longer
    # says which bolt it belongs to.
    row = [ln for ln in text.splitlines() if "15-22 Nm" in ln]
    assert row and row[0] == f"Testata{search.CELL_SEP}15-22 Nm", row
    # Runs inside one paragraph stay on one line; <w:tab/> is whitespace.
    assert "riga uno\triga due" in text, repr(text[-60:])


def test_docx_headings_feed_chunk_document():
    text = search.office_text(docx_fixture())
    chunks = search.chunk_document(text)
    headings = {h for h, _b, _l in chunks}
    assert "Manuale VEVOR" in headings, headings
    assert "Coppie" in headings, headings


def test_namespace_agnostic():
    # Same document in the STRICT namespace: a URI-keyed parser returns "".
    strict = search.office_text(docx_fixture(ns=W_STRICT))
    assert "# Manuale VEVOR" in strict, repr(strict[:200])
    assert "15-22 Nm" in strict


# --------------------------------------------------------------------------- #
# .pptx
# --------------------------------------------------------------------------- #
def pptx_fixture(n_slides=12):
    members = {}
    for i in range(1, n_slides + 1):
        members[f"ppt/slides/slide{i}.xml"] = (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
            f'<p:sp><p:txBody><a:p><a:r><a:t>Contenuto della slide {i}</a:t>'
            f'</a:r></a:p></p:txBody></p:sp>'
            f'</p:spTree></p:cSld></p:sld>'
        )
    return _zip("f.pptx", members)


def test_pptx_slide_numbering_is_numeric():
    text = search.office_text(pptx_fixture())
    order = [ln for ln in text.splitlines() if ln.startswith("## Slide")]
    assert order == [f"## Slide {i}" for i in range(1, 13)], order
    # The heading must sit with the right body, which is what a lexical sort
    # would break from slide 10 on.
    i = text.index("## Slide 10")
    assert "Contenuto della slide 10" in text[i:i + 120], text[i:i + 120]


# --------------------------------------------------------------------------- #
# .xlsx
# --------------------------------------------------------------------------- #
def xlsx_fixture(date1904=False):
    # Two sheets, declared in workbook order OPPOSITE to the file order, so a
    # parser that globs sheetN.xml gets both the order and the names wrong.
    workbook = (
        f'<workbook xmlns="{S}" xmlns:r="{R}">'
        f'{"<workbookPr date1904=\"1\"/>" if date1904 else ""}'
        f'<sheets><sheet name="Scadenze" sheetId="1" r:id="rId2"/>'
        f'<sheet name="Costi 2026" sheetId="2" r:id="rId1"/></sheets></workbook>'
    )
    rels = (f'<Relationships xmlns="{PKG_R}">'
            f'<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
            f'</Relationships>')
    shared = (f'<sst xmlns="{S}"><si><t>Descrizione</t></si>'
              f'<si><t>Visita cardiologica</t></si></sst>')
    # numFmtId 14 is a builtin date; 164 is custom "dd/mm/yyyy"; 165 has a 'd'
    # only inside a quoted literal and must NOT count as a date.
    styles = (f'<styleSheet xmlns="{S}"><numFmts>'
              f'<numFmt numFmtId="164" formatCode="dd/mm/yyyy"/>'
              f'<numFmt numFmtId="165" formatCode="0.00&quot; usd&quot;"/>'
              f'</numFmts><cellXfs count="4">'
              f'<xf numFmtId="0"/><xf numFmtId="14"/>'
              f'<xf numFmtId="164"/><xf numFmtId="165"/>'
              f'</cellXfs></styleSheet>')
    # 46069 = 2026-02-16 on the 1900 epoch — the serial LibreOffice actually
    # writes for that date (checked against a converted file, so the epoch is
    # pinned to a real producer and not to our own arithmetic).
    sheet2 = (f'<worksheet xmlns="{S}"><sheetData>'
              f'<row r="1"><c r="A1" t="s"><v>0</v></c>'
              f'<c r="B1" t="inlineStr"><is><t>Data</t></is></c></row>'
              f'<row r="2"><c r="A2" t="s"><v>1</v></c>'
              f'<c r="B2" s="1"><v>46069</v></c>'
              f'<c r="C2" s="2"><v>46069</v></c>'
              f'<c r="D2" s="3"><v>120.5</v></c></row>'
              f'<row r="3"></row>'
              f'<row r="4"><c r="A4" t="b"><v>1</v></c>'
              f'<c r="B4"/><c r="C4"/></row>'
              f'</sheetData></worksheet>')
    sheet1 = (f'<worksheet xmlns="{S}"><sheetData>'
              f'<row r="1"><c r="A1" t="str"><v>totale</v></c>'
              f'<c r="B1"><v>42</v></c></row>'
              f'</sheetData></worksheet>')
    return _zip(f"f-{1904 if date1904 else 1900}.xlsx", {
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": rels,
        "xl/sharedStrings.xml": shared,
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet1,
        "xl/worksheets/sheet2.xml": sheet2,
    })


def test_xlsx_sheet_names_and_order():
    text = search.office_text(xlsx_fixture())
    sheets = [ln for ln in text.splitlines() if ln.startswith("## Sheet")]
    assert sheets == ["## Sheet: Scadenze", "## Sheet: Costi 2026"], sheets


def test_xlsx_values_strings_and_dates():
    text = search.office_text(xlsx_fixture())
    sep = search.CELL_SEP
    # Shared string + builtin date + custom date + a quoted 'd' that is NOT one.
    assert f"Visita cardiologica{sep}2026-02-16{sep}2026-02-16{sep}120.5" in text, text
    assert f"Descrizione{sep}Data" in text, text          # inline string
    assert "TRUE" in text                                  # boolean
    # An empty row produces no line, and trailing empty cells are not padded.
    assert f"TRUE{sep}" not in text, text
    assert "\n\n\n" not in text


def test_xlsx_1904_epoch():
    text = search.office_text(xlsx_fixture(date1904=True))
    # The same serial is 1462 days (four years and a day) later on the 1904
    # epoch — a workbook saved by old Mac Excel, silently four years off if the
    # flag is ignored.
    assert "2030-02-17" in text, [ln for ln in text.splitlines() if "20" in ln] or text


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_not_a_zip_and_corrupt_are_none():
    plain = TMP / "fake.docx"
    plain.write_bytes(b"this is not a zip")
    assert search.office_text(str(plain)) is None
    truncated = TMP / "trunc.xlsx"
    truncated.write_bytes(zipfile.ZipFile(io.BytesIO(), "w") and b"PK\x03\x04broken")
    assert search.office_text(str(truncated)) is None
    assert search.office_text(str(TMP / "missing.pptx")) is None
    # A wrong extension is None too: office_text is not a sniffer.
    assert search.office_text(__file__) is None


def test_zip_bomb_refused_before_extracting():
    path = TMP / "bomb.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml",
                    f'<w:document xmlns:w="{W}"><w:body>'
                    + "<w:p><w:r><w:t>a</w:t></w:r></w:p>" * 10
                    + "</w:body></w:document>")
        # A member whose declared size alone is over the ceiling.
        zf.writestr("payload.bin", b"\0" * (search.MAX_OFFICE_UNCOMPRESSED + 1))
    assert search.office_text(str(path)) is None


def test_extracted_text_cap_is_declared():
    long_para = "parola " * 400
    body = "".join(_para(f"{i} {long_para}") for i in range(2000))
    path = _zip("big.docx",
                {"word/document.xml":
                 f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'})
    text = search.office_text(path)
    assert len(text) > search.MAX_OFFICE_CHARS       # the note is appended
    assert text.rstrip().endswith("[... document truncated]"), text[-80:]


# --------------------------------------------------------------------------- #
# The locator invariant
# --------------------------------------------------------------------------- #
def test_search_id_opens_the_same_line_in_local_read():
    """An f: id minted from Office text must open that passage in local_read."""
    folder = TMP / "lib"
    folder.mkdir(exist_ok=True)
    src = Path(docx_fixture())
    target = folder / "manuale.docx"
    target.write_bytes(src.read_bytes())

    hits = search.search_text_file(str(target), "manuale.docx",
                                   *search.parse_query("coppia testata"))
    assert hits, "the docx must be searchable"
    rid = hits[0]["id"]
    assert rid.startswith("f:manuale.docx:"), rid
    line = int(rid.rsplit(":", 1)[1])

    # local_read's OWN copy of the extractor must produce the same text, or the
    # line number points somewhere else.
    text_search = search.read_text_file(str(target))
    text_read = read.read_text_file(str(target))
    assert text_search == text_read, "the two copies extract different text"

    lines = text_read.splitlines()
    assert 1 <= line <= len(lines), (line, len(lines))
    window = "\n".join(lines[line - 1:line + 3])
    assert "15-22 Nm" in window, (line, window)


def test_office_files_are_collected_and_chunked():
    folder = TMP / "lib2"
    folder.mkdir(exist_ok=True)
    (folder / "note.md").write_text("# nota\n\ntesto qualunque\n", encoding="utf-8")
    Path(folder / "p.pptx").write_bytes(Path(pptx_fixture()).read_bytes())

    text_files, pdfs = search.collect_files(str(folder))
    names = sorted(os.path.basename(f) for f in text_files)
    assert names == ["note.md", "p.pptx"], names
    assert not pdfs

    # chunks_for is what the SEMANTIC index reads, and its locators must be the
    # ids a keyword hit would print.
    chunks = search.chunks_for(str(folder / "p.pptx"), "p.pptx")
    assert chunks, "an Office file must reach the semantic index"
    assert all(loc.startswith("f:p.pptx:") for loc, *_ in chunks), chunks[:3]
    assert any("Slide" in head for _loc, head, *_ in chunks), chunks[:3]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
