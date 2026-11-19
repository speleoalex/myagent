#!/usr/bin/env python3
"""write_pdf: Markdown in, a delivered PDF out.

Born from a live mail (2026-11-11, catasto mailbox): asked for "un report pdf",
the agent answered "non ho la possibilità di generare o allegare un file PDF".
It was right: no tool made one.

What this pins:
  * the PDF exists, is a PDF, and is announced with the resource marker (the
    marker is what a messaging channel turns into an attachment);
  * the text is in the PDF and raw HTML in the Markdown is NOT interpreted
    (the document is mailed out: a script or a file:// embed must stay text);
  * a path outside the workspace is refused in-band, nothing is written;
  * ".pdf" is appended to a bare name;
  * images: a workspace image is embedded, an address on this machine and a
    path out of the workspace are left out and named in the output, and
    Chrome prints with no network (an image it could fetch is not in the PDF).

Needs a Chrome/Chromium; without one the rendering checks are skipped.

Run:  python3 tests/test_write_pdf.py
"""
import http.server
import importlib.machinery
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "server" / "tools" / "file_management" / "write_pdf" / "run"

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def call(workspace: Path, **args) -> str:
    env = dict(os.environ, MYAGENT_APP_DIR=str(ROOT / "server"))
    return subprocess.run([sys.executable, str(RUN)], input=json.dumps(args),
                          capture_output=True, text=True, cwd=workspace,
                          env=env, timeout=150).stdout


def main() -> int:
    with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
        ws = Path(tmp)
        print("Refusals:")
        out = call(ws, path="../escape.pdf", content="# x")
        check(out.startswith("ERROR") and not (ws.parent / "escape.pdf").exists(),
              "a path outside the workspace is refused")
        check(call(ws, path="a.pdf", content="  ").startswith("ERROR"),
              "empty content is refused")

        if not any(shutil.which(n) for n in ("chromium", "chromium-browser",
                                             "google-chrome", "google-chrome-stable")):
            print("  skip rendering: no Chrome/Chromium on this machine")
            return 1 if failures else 0

        print("Rendering:")
        marker_text = "Sviluppo àèìòù"
        out = call(ws, path="out/report", title="Borgio | Verezzi",
                   content=f"# Report\n\n{marker_text}\n\n<script>document.write('INJECTED')</script>\n"
                           "\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        pdf = ws / "out" / "report.pdf"
        check(pdf.is_file() and pdf.read_bytes().startswith(b"%PDF"),
              "a PDF is written, with .pdf appended to the name")
        check("[[resource:out/report.pdf|application/pdf|Borgio / Verezzi]]" in out,
              "the resource marker announces it, title sanitised")
        check(list(ws.rglob("*.html")) == [], "the intermediate HTML page is removed")
        if shutil.which("pdftotext"):
            text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True,
                                  text=True).stdout
            check(marker_text in text, "the text is in the PDF")
            check("<script>" in text and "INJECTED" not in text.replace(
                "document.write('INJECTED')", ""), "raw HTML stays text, never runs")
        print("Images:")
        png = ROOT / "ui" / "icons" / "icon-192.png"
        (ws / "pic.png").write_bytes(png.read_bytes())
        out = call(ws, path="img.pdf", content="# I\n\n![ok](pic.png)\n\n"
                   "![local](http://127.0.0.1:9/x.png)\n\n![out](../pic.png)\n")
        check("1 image(s)" in out, "the workspace image is embedded")
        check("127.0.0.1:9/x.png: the address is not public" in out,
              "an address on this machine is refused and named")
        check("../pic.png:" in out, "a path out of the workspace is refused and named")
        if shutil.which("pdfimages"):
            listing = subprocess.run(["pdfimages", "-list", str(ws / "img.pdf")],
                                     capture_output=True, text=True).stdout
            check(" 192 " in listing, "the embedded picture is in the PDF at full size")

        # Chrome itself: a page that references a server on this machine must
        # not reach it. The server counts the requests it receives.
        hits = []

        class Probe(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Probe)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            sys.path.insert(0, str(ROOT / "server"))
            loader = importlib.machinery.SourceFileLoader("write_pdf_run", str(RUN))
            source = loader.get_data(str(RUN)).decode().replace("\nmain()\n", "\n")
            module = types.ModuleType("write_pdf_run")
            # The run script resolves its import root from __file__ when the
            # server has not exported MYAGENT_APP_DIR: an exec'd module has none.
            module.__dict__["__file__"] = str(RUN)
            exec(compile(source, str(RUN), "exec"), module.__dict__)
            page = ws / "probe.html"
            page.write_text(f'<img src="http://127.0.0.1:{srv.server_port}/probe.png">')
            subprocess.run([module.find_browser(), "--headless", "--no-sandbox",
                            "--disable-gpu", *module.NO_NETWORK,
                            f"--print-to-pdf={ws / 'probe.pdf'}", page.as_uri()],
                           capture_output=True, timeout=120)
            check((ws / "probe.pdf").is_file() and hits == [],
                  f"Chrome prints with no network: {len(hits)} request(s) reached the server")
        finally:
            srv.shutdown()
    return 1 if failures else 0


if __name__ == "__main__":
    code = main()
    print("\nFAILED: " + "; ".join(failures) if failures else "\nall ok")
    sys.exit(code)
