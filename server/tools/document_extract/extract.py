"""Extract text + images from a document (PDF / HTML / image / audio) into Markdown.

Wraps battle-tested system binaries:
  - PDF  : pdftotext + pdfimages (poppler-utils); OCR fallback via pdftoppm+tesseract
  - HTML : pandoc (html -> gfm), with images decoded/copied to disk
  - image: copied to disk + optional OCR (tesseract)
  - audio: ffmpeg (any codec -> 16k mono wav) + faster-whisper transcription

Extracted images are written as files under image_dir (default /tmp/...) and
linked inline in the Markdown — never inlined as base64, so the output stays
small and a vision step can open the files afterwards. Audio is transcribed to
plain text (faster-whisper is imported lazily, only when an audio file is
processed, so the other formats keep working without it).
"""
import base64
import hashlib
import json
import mimetypes
import os
import datetime
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ppm", ".pgm"}
AUDIO_EXTS = {".oga", ".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac",
              ".wma", ".amr", ".webm", ".weba"}
# What `pdfimages -all` writes that a viewer can actually open: it also emits
# raw .ccitt/.jbig2 streams with .params sidecars for bilevel scans, and those
# are links to nothing.
VIEWABLE_IMG_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
# Output budget for a PDF window, kept under the tool's max_output so the cut
# happens HERE, at a page boundary we can name, instead of mid-page in the
# registry.
MAX_CHARS = 18000


def err(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run_cmd(cmd: list[str], input_bytes: bytes | None = None, timeout: int = 90) -> tuple[int, bytes, bytes]:
    """Run a command; return (rc, stdout, stderr). rc=127 if the binary is missing."""
    try:
        p = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, b"", f"{cmd[0]} not found".encode()
    except subprocess.TimeoutExpired:
        return 124, b"", f"{cmd[0]} timed out".encode()


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def detect_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".html", ".htm", ".xhtml"):
        return "html"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in OFFICE_LEGACY_EXTS:
        return "office_legacy"
    # Fall back to the mime type reported by `file`
    rc, out, _ = run_cmd(["file", "--mime-type", "-b", str(path)])
    mime = out.decode(errors="replace").strip() if rc == 0 else (mimetypes.guess_type(str(path))[0] or "")
    if mime == "application/pdf":
        return "pdf"
    if mime in ("text/html", "application/xhtml+xml"):
        return "html"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/") or mime in ("application/json", "application/xml",
                                            "application/x-yaml", "application/toml"):
        return "text"
    return "unknown"


def extract_text(path: Path, from_page: int = 1) -> str:
    """A plain-text file (Markdown, .txt, .csv, .json, code) returned as is.

    Not a conversion — but an agent granted document_extract and NOT file_read
    (librarian-style agents) could otherwise read PDFs and never the .md next
    to them ("Unsupported file type", observed). Same window contract as the
    PDF path: MAX_CHARS per call, cut declared, `from_page` continues (here a
    "page" is one MAX_CHARS window — the parameter already exists and the
    model already knows the footer)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        err(f"Cannot read '{path.name}': {e}")
    header, body, _first = window_text(text, path, from_page)
    return header + body


def window_text(text: str, path: Path, from_page: int = 1) -> tuple[str, str, bool]:
    """One MAX_CHARS window of *text*, with the header and continuation footer.

    Split out of extract_text so the Office path reuses this VERBATIM: the
    window contract (cut declared, `from_page` continues) is then identical for
    a .md and a .docx, and the model already knows the footer. Returns
    ``(header, body_with_footer, is_first_window)`` — the caller needs the
    third to decide whether to attach images once rather than on every window.
    """
    total = len(text)
    pages = max(1, -(-total // MAX_CHARS))
    page = max(1, from_page)
    if page > pages:
        err(f"'{path.name}' has {pages} window(s) of {MAX_CHARS} chars: "
            f"from_page={page} is past the end.")
    start = (page - 1) * MAX_CHARS
    chunk = text[start:start + MAX_CHARS]
    header = f"# {path.name}\n\n_[{path.suffix.lstrip('.') or 'text'}, {total} chars"
    if pages > 1:
        header += f", window {page} of {pages}"
    header += "]_\n\n"
    footer = ""
    if page < pages:
        footer = f"\n\n_[continua con document_extract from_page={page + 1}]_"
    return header, chunk + footer + "\n", page == 1


# --------------------------------------------------------------- Office (OOXML)
# THIRD copy of this extractor (local_search and local_read hold the other two).
# Forced by the same rule that forces those: a CoW override copies ONE leaf
# folder, so tools cannot share a module. Unlike those two this copy exchanges
# no computed ids with anyone, so a divergence here cannot open the wrong
# passage — but a fix landing in two places out of three is still a bug, so
# tests/test_office_extract.py compares all THREE.
#
# Image extraction is deliberately kept OUT of office_text (see extract_office):
# that is what lets this copy stay identical to the library ones.
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


# Office is recognised by EXTENSION only — deliberately not through the
# `file --mime-type` fallback the other kinds use. office_text dispatches on the
# extension too (it has to: the three formats share one container), so a mime
# branch here would route a file that office_text then refuses, turning a clear
# "unsupported, here is the list" into "not a valid .docx/.xlsx/.pptx" for a file
# that is perfectly valid. One rule instead of two that can disagree; a
# mis-named document gets the supported-extensions message and is renamed.
#
# Embedded pictures live under these prefixes in the package.
# The pre-2007 OLE2 formats, and what to convert each one INTO so the error can
# name the command. Deliberately only these three: OpenDocument (.odt/.ods/.odp)
# is a ZIP like OOXML and could be read here one day rather than converted, and
# .rtf already degrades to plain text through the mime fallback — turning either
# into an error would take away something that works.
OFFICE_LEGACY_EXTS = {".doc": "docx", ".xls": "xlsx", ".ppt": "pptx"}
OFFICE_MEDIA_DIRS = ("word/media/", "ppt/media/", "xl/media/")
MAX_OFFICE_IMAGES = 8
# Per picture. A document can embed a full-resolution photo, and the point of
# these links is to let the user SEE a diagram, not to copy a photo library into
# /tmp on every extraction.
MAX_IMAGE_BYTES = 2_000_000


def office_images(path: Path, out_dir: Path) -> list[Path]:
    """Save the document's embedded pictures and return what is viewable.

    stdlib again: the pictures are just members of the ZIP, so this needs no
    converter. Numeric order for the same reason the slides use it — the parts
    are named image1/image2/…/image10 and a lexical sort reads that as 1, 10, 2.
    """
    saved: list[Path] = []
    try:
        # Own the directory rather than assume the caller made it: every write
        # below fails with OSError otherwise, and the `continue` that follows
        # would drop every picture in SILENCE.
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            members = [n for n in zf.namelist()
                       if n.startswith(OFFICE_MEDIA_DIRS)
                       and os.path.splitext(n)[1].lower() in VIEWABLE_IMG_EXTS]

            def key(name):
                m = re.search(r"(\d+)(?=\.[^.]+$)", name)
                return (int(m.group(1)) if m else 0, name)

            for name in sorted(members, key=key)[:MAX_OFFICE_IMAGES]:
                info = zf.getinfo(name)
                if info.file_size > MAX_IMAGE_BYTES:
                    continue
                dest = out_dir / f"media-{os.path.basename(name)}"
                try:
                    with zf.open(name) as src, open(dest, "wb") as fh:
                        shutil.copyfileobj(src, fh)
                except OSError:
                    continue
                saved.append(dest)
    except (OSError, zipfile.BadZipFile):
        return []
    return dedup_and_clean(saved)


def extract_office(path: Path, out_dir: Path, from_page: int, images: bool) -> str:
    """A .docx/.xlsx/.pptx as text, in MAX_CHARS windows like a text file."""
    text = office_text(str(path))
    if text is None:
        err(f"Cannot read '{path.name}': not a valid .docx/.xlsx/.pptx "
            f"(the pre-2007 .doc/.xls/.ppt are a different format — convert "
            f"them with: libreoffice --headless --convert-to docx '{path.name}')")
    if not text.strip():
        err(f"'{path.name}' contains no extractable text.")
    header, body, first = window_text(text, path, from_page)
    out = header + body
    # Once, on the first window: the pictures have no window to belong to, and
    # re-attaching them to every window would repeat the same links (the rule
    # local_read already applies to ZIM article images).
    if images and first:
        links = [f"![{path.stem} · image {i}]({f})"
                 for i, f in enumerate(office_images(path, out_dir), 1)]
        if links:
            out += "\n\n" + "\n".join(links) + "\n"
    return out


def default_image_dir(path: Path) -> Path:
    h = hashlib.md5(str(path.resolve()).encode()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", path.stem)[:40] or "doc"
    return Path("/tmp/myagent_extract") / f"{safe}-{h}"


def dedup_and_clean(files: list[Path]) -> list[Path]:
    """Drop byte-identical duplicates (common in PDFs); keep first occurrence."""
    seen: dict[str, Path] = {}
    kept: list[Path] = []
    for f in files:
        try:
            digest = hashlib.md5(f.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest in seen:
            f.unlink(missing_ok=True)  # remove the duplicate file
            continue
        seen[digest] = f
        kept.append(f)
    return kept


# --------------------------------------------------------------------------- PDF
def normalize_layout(text: str) -> str:
    """Squeeze `pdftotext -layout` output: whitespace runs to two spaces, at
    most one blank line. Mirrored in the library tools' pdf_pages().

    `-layout` is what keeps a table ROW together ("Clutch cover bolt  15-22
    1.5-2.2  11-15"); plain pdftotext puts every cell on a line of its own and
    a torque table stops saying which value belongs to which bolt. The price is
    padding with the page geometry — measured on a scanned service manual, 45%
    of the output was indentation, i.e. half the model's budget for the page.
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


def _pdf_image_types(path: Path, first: int, last: int) -> dict[tuple[int, int], str]:
    """Map (page, num) -> image type from `pdfimages -list` (to skip smasks)."""
    rc, out, _ = run_cmd(["pdfimages", "-list", "-f", str(first), "-l", str(last),
                          str(path)])
    types: dict[tuple[int, int], str] = {}
    if rc != 0:
        return types
    for line in out.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            page, num = int(parts[0]), int(parts[1])
        except ValueError:
            continue  # header / separator rows
        types[(page, num)] = parts[2]
    return types


def extract_pdf(path: Path, out_dir: Path, max_pages: int,
                from_page: int = 1, images: bool = True) -> str:
    if not have("pdftotext"):
        err("pdftotext (poppler-utils) is not installed")

    # 1) Text, page by page (pages are separated by form-feed). The WHOLE
    # document is extracted even for a windowed read — it costs well under a
    # second and is the only honest source for the total page count.
    rc, out, serr = run_cmd(["pdftotext", "-layout", str(path), "-"], timeout=90)
    if rc == 127:
        err("pdftotext (poppler-utils) is not installed")
    text = out.decode(errors="replace") if rc == 0 else ""
    text_pages = [normalize_layout(p) for p in text.split("\x0c")]
    while text_pages and not text_pages[-1].strip():
        text_pages.pop()
    has_text = any(p.strip() for p in text_pages)

    # The window to render. from_page is what makes a 200-page manual readable
    # past its first pages: max_pages alone only ever moved the END.
    #
    # The page total comes from pdfinfo when available (10-40ms, same package as
    # pdftotext) because a SCAN has no text pages to count, and the window would
    # collapse to one page — the OCR fallback below has to know how far to go.
    total_pages = len(text_pages)
    rc_i, info, _ = run_cmd(["pdfinfo", str(path)])
    if rc_i == 0:
        m = re.search(r"^Pages:\s+(\d+)", info.decode(errors="replace"), re.M)
        if m:
            total_pages = max(total_pages, int(m.group(1)))
    first = max(1, from_page)
    last = min(total_pages, first + max_pages - 1) if total_pages else first

    # 2) Embedded images for the WINDOW only, skipping soft-masks; then dedup.
    # Restricted with -f/-l because a scanned manual carries one full-page
    # image per page: extracting all 214 of them to read page 12 was tens of
    # MB of /tmp and seconds of work, thrown away.
    img_by_page: dict[int, list[Path]] = {}
    if images and have("pdfimages"):
        types = _pdf_image_types(path, first, last)
        prefix = out_dir / "img"
        run_cmd(["pdfimages", "-all", "-p", "-f", str(first), "-l", str(last),
                 str(path), str(prefix)], timeout=90)
        raw = sorted(out_dir.glob("img-*"))
        keep: list[Path] = []
        for f in raw:
            m = re.match(r"img-(\d+)-(\d+)\.", f.name)
            if not m:
                continue
            page, num = int(m.group(1)), int(m.group(2))
            # Keep only real images (drop smask/stencil alpha masks), and only
            # what can actually be VIEWED: `-all` writes a bilevel scan as a
            # raw .ccitt stream plus a .params sidecar, and linking those put
            # 21 dead links in the output of a 14-page manual (measured).
            if (types.get((page, num), "image") != "image"
                    or f.suffix.lower() not in VIEWABLE_IMG_EXTS):
                f.unlink(missing_ok=True)
                continue
            keep.append(f)
        for f in dedup_and_clean(keep):
            m = re.match(r"img-(\d+)-", f.name)
            page = int(m.group(1)) if m else 0
            img_by_page.setdefault(page, []).append(f)

    # 3) OCR fallback for scanned PDFs (no extractable text) — render + OCR the
    # window's pages.
    ocr_pages: dict[int, str] = {}
    if not has_text and have("pdftoppm") and have("tesseract"):
        # Last resort when pdfinfo is absent: the image page numbers.
        if not total_pages and img_by_page:
            total_pages = max(img_by_page)
            last = min(total_pages, first + max_pages - 1)
        for pg in range(first, (last if total_pages else first) + 1):
            base = out_dir / f"page-{pg:03d}"
            run_cmd(["pdftoppm", "-png", "-r", "150", "-f", str(pg), "-l", str(pg),
                     str(path), str(base)], timeout=60)
            rendered = sorted(out_dir.glob(f"page-{pg:03d}*.png"))
            if not rendered:
                continue
            page_img = rendered[0]
            if images:
                img_by_page.setdefault(pg, []).append(page_img)
            rc2, oout, _ = run_cmd(["tesseract", str(page_img), "stdout", "-l", "eng+ita"], timeout=60)
            if rc2 == 0:
                ocr_pages[pg] = oout.decode(errors="replace").strip()

    if total_pages and first > total_pages:
        err(f"'{path.name}' has {total_pages} pages: from_page={from_page} is "
            f"past the end")

    # 4) Assemble Markdown, interleaving each page's text with its images, and
    # stop at a PAGE boundary once MAX_CHARS is spent. The registry would
    # otherwise cut mid-page at max_output, and the model has no way to tell
    # where to resume from — which is how a 214-page manual read as "only the
    # first four pages exist".
    lines = [f"# {path.name}", ""]
    shown = first - 1
    budget = MAX_CHARS
    for pg in range(first, (last or first) + 1):
        body = ""
        if pg - 1 < len(text_pages) and text_pages[pg - 1].strip():
            body = text_pages[pg - 1]
        elif ocr_pages.get(pg):
            body = ocr_pages[pg]
        page_lines = [f"## Pagina {pg}\n"]
        if body.strip():
            page_lines += [body.strip(), ""]
        for i, img in enumerate(img_by_page.get(pg, [])):
            page_lines.append(f"![page {pg} · image {i}]({img})")
        if img_by_page.get(pg):
            page_lines.append("")
        cost = sum(len(x) + 1 for x in page_lines)
        if shown >= first and cost > budget:
            break
        budget -= cost
        lines += page_lines
        shown = pg

    if not (first == 1 and shown == total_pages):
        lines.insert(2, f"_[Documento di {total_pages} pagine — mostrate "
                        f"{first}-{shown}]_\n")
    if shown < total_pages:
        lines.append(f"\n_[continua con document_extract from_page={shown + 1}]_")
    if not has_text and not ocr_pages:
        lines.append("_[No extractable text — the PDF is probably scanned and no OCR is available]_")
    return "\n".join(lines).rstrip() + "\n"


# -------------------------------------------------------------------------- HTML
def _img_ext_from_mime(mime: str) -> str:
    return {
        "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
        "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
        "image/bmp": ".bmp",
    }.get(mime.lower(), ".img")


def extract_html(path: Path, out_dir: Path) -> str:
    if not have("pandoc"):
        err("pandoc is not installed")
    rc, out, serr = run_cmd(["pandoc", "-f", "html", "-t", "gfm", str(path)], timeout=60)
    if rc == 127:
        err("pandoc is not installed")
    if rc != 0:
        err(f"pandoc failed: {serr.decode(errors='replace')[:200]}")
    md = out.decode(errors="replace")

    counter = {"n": 0}

    def save_bytes(data: bytes, ext: str) -> Path:
        counter["n"] += 1
        dest = out_dir / f"img-{counter['n']:03d}{ext}"
        dest.write_bytes(data)
        return dest

    def repl(m: re.Match) -> str:
        alt, raw_url = m.group(1), m.group(2).strip()
        url = raw_url[1:-1] if raw_url.startswith("<") and raw_url.endswith(">") else raw_url
        # data: URI -> decode to a file
        dm = re.match(r"data:([^;,]*)(;base64)?,(.*)$", url, re.DOTALL)
        if dm:
            mime, is_b64, payload = dm.group(1), dm.group(2), dm.group(3)
            try:
                data = base64.b64decode(payload) if is_b64 else payload.encode()
            except Exception:
                return m.group(0)
            dest = save_bytes(data, _img_ext_from_mime(mime))
            return f"![{alt}]({dest})"
        # remote URL -> leave as-is (already a usable link)
        if re.match(r"https?://", url):
            return m.group(0)
        # local/relative path -> copy next to the others
        src = (path.parent / url).resolve()
        if src.is_file():
            dest = out_dir / f"img-{counter['n'] + 1:03d}{src.suffix or '.img'}"
            try:
                shutil.copyfile(src, dest)
                counter["n"] += 1
                return f"![{alt}]({dest})"
            except OSError:
                return m.group(0)
        return m.group(0)

    md = re.sub(r"!\[([^\]]*)\]\((<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\)", repl, md)
    return f"# {path.name}\n\n{md.strip()}\n"


# ------------------------------------------------------------------------- image
def extract_image(path: Path, out_dir: Path, ocr: bool) -> str:
    # Ensure the image lives under out_dir so the link is stable.
    if path.parent.resolve() == out_dir.resolve():
        dest = path
    else:
        dest = out_dir / path.name
        try:
            shutil.copyfile(path, dest)
        except OSError:
            dest = path  # fall back to linking the original location

    dims = ""
    if have("identify"):
        rc, out, _ = run_cmd(["identify", "-format", "%wx%h", str(dest)], timeout=20)
        if rc == 0 and out.strip():
            dims = out.decode(errors="replace").split()[0]

    lines = [f"# {path.name}", ""]
    lines.append(f"![{path.name}]({dest})")
    if dims:
        lines.append(f"\n_Dimensioni: {dims} px_")

    if ocr and have("tesseract"):
        rc, out, _ = run_cmd(["tesseract", str(dest), "stdout", "-l", "eng+ita"], timeout=60)
        ocr_text = out.decode(errors="replace").strip() if rc == 0 else ""
        lines.append("\n## Testo (OCR)\n")
        lines.append(ocr_text if ocr_text else "_(nessun testo riconosciuto)_")
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------------- audio
def extract_audio(path: Path, out_dir: Path, language: str | None) -> str:
    """Transcribe an audio file to text with faster-whisper. Any codec is first
    normalized to 16 kHz mono WAV via ffmpeg so we don't depend on the Whisper
    decoder supporting every container (opus/oga/mp3/m4a/...)."""
    audio_path = str(path)
    if have("ffmpeg"):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", path.stem)[:40] or "audio"
        wav = out_dir / f"{safe}.16k.wav"
        rc, _, _ = run_cmd(["ffmpeg", "-nostdin", "-y", "-i", str(path),
                            "-ar", "16000", "-ac", "1", str(wav)], timeout=120)
        if rc == 0 and wav.exists():
            audio_path = str(wav)

    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        err(f"faster-whisper is not installed ({e}) — it is required to "
            f"transcribe audio. It ships with the connectors plugin; to install "
            f"it on its own: <app>/server/.venv/bin/pip install faster-whisper")

    model_size = os.environ.get("MYAGENT_WHISPER_MODEL", "small")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio_path, language=(language or None))
    text = "".join(seg.text for seg in segments).strip()

    header = f"# {path.name}\n\n_Audio transcription"
    lang = getattr(info, "language", None)
    if lang:
        header += f" · detected language: {lang}"
    header += "_\n"
    return header + "\n" + (text if text else "_(no speech recognized)_") + "\n"


def main():
    try:
        args = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        err("invalid JSON input")
    raw = args.get("path")
    if not raw:
        err("missing required parameter: path")
    path = Path(raw).expanduser()
    if not path.exists():
        err(f"File not found: {path}")
    if not path.is_file():
        err(f"Not a file: {path}")

    out_dir = Path(args["image_dir"]).expanduser() if args.get("image_dir") else default_image_dir(path)
    out_dir.mkdir(parents=True, exist_ok=True)

    max_pages = int(args.get("max_pages") or 50)
    from_page = int(args.get("from_page") or 1)
    ocr = args.get("ocr", True)
    # Tolerate the string forms small models produce for booleans.
    images = args.get("images", True)
    if isinstance(images, str):
        images = images.strip().lower() in ("1", "true", "yes", "si", "sì")

    kind = detect_kind(path)
    if kind == "pdf":
        md = extract_pdf(path, out_dir, max_pages, from_page, bool(images))
    elif kind == "html":
        md = extract_html(path, out_dir)
    elif kind == "image":
        md = extract_image(path, out_dir, bool(ocr))
    elif kind == "audio":
        md = extract_audio(path, out_dir, args.get("language"))
    elif kind == "office":
        md = extract_office(path, out_dir, from_page, bool(images))
    elif kind == "office_legacy":
        # Named separately from "unknown" only to give the ONE piece of advice
        # that works: the generic supported-types list reads as "this file is
        # not a document", which is exactly wrong here.
        err(f"'{path.name}' is a pre-2007 Office file (OLE2), a different format "
            f"from .docx/.xlsx/.pptx and not readable here. Convert it once: "
            f"libreoffice --headless --convert-to "
            f"{OFFICE_LEGACY_EXTS[path.suffix.lower()]} '{path.name}'")
    elif kind == "text":
        md = extract_text(path, from_page)
    else:
        err(f"Unsupported file type for '{path.name}'. Supported: PDF, HTML, "
            f"Office (.docx, .xlsx, .pptx), images, audio, plain text "
            f"(.md, .txt, .csv, .json, code).")

    print(md, end="")


if __name__ == "__main__":
    main()
