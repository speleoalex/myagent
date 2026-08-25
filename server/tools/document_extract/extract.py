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
import re
import shutil
import subprocess
import sys
from pathlib import Path

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
    return header + chunk + footer + "\n"


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
    elif kind == "text":
        md = extract_text(path, from_page)
    else:
        err(f"Unsupported file type for '{path.name}'. Supported: PDF, HTML, images, "
            f"audio, plain text (.md, .txt, .csv, .json, code).")

    print(md, end="")


if __name__ == "__main__":
    main()
