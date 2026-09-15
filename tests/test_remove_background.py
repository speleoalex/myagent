#!/usr/bin/env python3
"""remove_background must composite exactly, name files safely and fail in sentences.

The segmentation network is the one part that is slow, 178 MB and someone
else's; everything around it is ours and is what drifts: how a background
argument is read, how a smaller/larger background picture is fitted, that the
subject's pixels are copied untouched, that outputs never overwrite, that the
mask is white where the BACKGROUND is (the convention edit_image forwards to
A1111), and that every refusal is one ERROR sentence. So the module is imported
from the tool folder and predict_alpha is replaced by a synthetic alpha (a
centred square), then main() is driven in-process with JSON on stdin and cwd
set to a throwaway workspace, exactly as the registry would.

Needs pillow and numpy in the venv (onnxruntime too, for the import check the
tool performs — it is never called).

Run: server/.venv/bin/python tests/test_remove_background.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image

TOOL = Path(__file__).resolve().parent.parent / "server/tools/images/remove_background/remove_background.py"
FAILURES: list[str] = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


spec = importlib.util.spec_from_file_location("remove_background", TOOL)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)

W, H = 60, 40


def synthetic_alpha(img, name):
    """Subject = a centred square 20x20 (1/6 of the picture), hard edges."""
    a = Image.new("L", img.size, 0)
    a.paste(255, (20, 10, 40, 30))
    return a


rb.predict_alpha = synthetic_alpha


def run(params, cwd):
    """main() in-process: (exit code, stdout)."""
    old = os.getcwd()
    os.chdir(cwd)
    buf = io.StringIO()
    code = 0
    try:
        sys.stdin = io.StringIO(json.dumps(params))
        with contextlib.redirect_stdout(buf):
            try:
                rb.main()
            except SystemExit as e:
                code = int(e.code or 0)
    finally:
        sys.stdin = sys.__stdin__
        os.chdir(old)
    return code, buf.getvalue()


with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    src = Image.new("RGB", (W, H), (200, 30, 30))          # red picture
    src.putpixel((30, 20), (1, 2, 3))                       # a marker pixel inside the subject
    src.save(ws / "photo.png")
    Image.new("RGB", (10, 100), (0, 200, 0)).save(ws / "tall.png")   # green, very tall
    (ws / "note.txt").write_text("not an image")

    # ---- pure helpers
    check("parse: default is transparent", rb.parse_background(None) == ("transparent", None))
    check("parse: 'transparent' word", rb.parse_background(" Transparent ") == ("transparent", None))
    check("parse: colour name", rb.parse_background("blue") == ("colour", (0, 0, 255)))
    check("parse: hex colour", rb.parse_background("#1e3a8a") == ("colour", (0x1e, 0x3a, 0x8a)))
    kind, val = rb.parse_background(str(ws / "tall.png"))
    check("parse: existing file is an image background", kind == "image" and val.name == "tall.png")
    code, out = run({"image": "photo.png", "background": "nosuchcolour"}, ws)
    check("parse: unknown word is one ERROR sentence", code == 1 and out.startswith("ERROR:") and "neither a colour" in out, out)

    fitted = rb.cover(Image.open(ws / "tall.png"), (W, H))
    check("cover: output has the source size", fitted.size == (W, H), str(fitted.size))
    check("cover: no borders (corner pixel is background colour)",
          fitted.convert("RGB").getpixel((0, 0)) == (0, 200, 0), str(fitted.convert("RGB").getpixel((0, 0))))

    alpha = synthetic_alpha(src, "x")
    check("coverage: 20x20 of 60x40 is 1/6", abs(rb.coverage(alpha) - 400 / 2400) < 1e-6)

    comp = rb.compose(src, alpha, "colour", (0, 0, 255))
    check("compose: subject pixel copied untouched", comp.getpixel((30, 20)) == (1, 2, 3, 255), str(comp.getpixel((30, 20))))
    check("compose: background pixel is the colour", comp.getpixel((0, 0)) == (0, 0, 255, 255))
    cut = rb.compose(src, alpha, "transparent", None)
    check("compose: transparent background has alpha 0 outside", cut.getpixel((0, 0))[3] == 0)
    check("compose: transparent keeps alpha 255 inside", cut.getpixel((30, 20)) == (1, 2, 3, 255))

    (ws / "taken.png").write_bytes(b"x")
    (ws / "taken-2.png").write_bytes(b"x")
    old = os.getcwd(); os.chdir(ws)
    try:
        check("free_name: skips existing numbered files", rb.free_name("taken") == "taken-3.png", rb.free_name("taken"))
        check("free_name: free base is used as is", rb.free_name("fresh") == "fresh.png")
    finally:
        os.chdir(old)

    # ---- end to end
    before = (ws / "photo.png").read_bytes()
    code, out = run({"image": "photo.png", "background": "blue"}, ws)
    check("e2e colour: exit 0", code == 0, out)
    check("e2e colour: output named <stem>-on-<colour>.png", (ws / "photo-on-blue.png").is_file(), out)
    check("e2e colour: mask saved", (ws / "photo-mask.png").is_file())
    check("e2e colour: resource marker", "[[resource:photo-on-blue.png|image/png|photo-on-blue]]" in out, out)
    check("e2e colour: coverage reported", "covers 17%" in out, out)
    check("e2e colour: no WARNING for a normal subject", "WARNING" not in out, out)
    check("e2e colour: source untouched", (ws / "photo.png").read_bytes() == before)
    mask = Image.open(ws / "photo-mask.png")
    check("e2e mask: white where the background is", mask.getpixel((0, 0)) == 255, str(mask.getpixel((0, 0))))
    check("e2e mask: black on the subject", mask.getpixel((30, 20)) == 0, str(mask.getpixel((30, 20))))
    check("e2e mask: same size as the source", mask.size == (W, H))
    res = Image.open(ws / "photo-on-blue.png")
    check("e2e colour: result is RGBA at source size", res.mode == "RGBA" and res.size == (W, H))

    code, out = run({"image": "photo.png", "background": "blue"}, ws)
    check("e2e repeat: second run does not overwrite", code == 0 and (ws / "photo-on-blue-2.png").is_file()
          and (ws / "photo-mask-2.png").is_file(), out)

    code, out = run({"image": "photo.png"}, ws)
    check("e2e transparent: output named <stem>-cutout.png", code == 0 and (ws / "photo-cutout.png").is_file(), out)

    code, out = run({"image": "photo.png", "background": "tall.png", "filename": "My Poster!"}, ws)
    check("e2e image bg: filename slugged", code == 0 and (ws / "my-poster.png").is_file(), out)
    check("e2e image bg: describes the background file", "tall.png" in out and "centre-cropped" in out, out)
    res = Image.open(ws / "my-poster.png")
    check("e2e image bg: background pixel comes from the picture", res.getpixel((0, 0)) == (0, 200, 0, 255), str(res.getpixel((0, 0))))
    check("e2e image bg: subject pixel untouched", res.getpixel((30, 20)) == (1, 2, 3, 255))

    # ---- refusals
    code, out = run({}, ws)
    check("error: missing image param", code == 1 and out.startswith("ERROR:") and "image is required" in out, out)
    code, out = run({"image": "missing.png"}, ws)
    check("error: missing file names list_dir", code == 1 and "not found" in out and "list_dir" in out, out)
    code, out = run({"image": "note.txt"}, ws)
    check("error: not an image", code == 1 and "not an image" in out, out)
    code, out = run({"image": "photo.png", "background": "photo.png"}, ws)
    check("error: background == source", code == 1 and "different file" in out, out)
    code, out = run({"image": "photo.png", "background": "#ff"}, ws)
    check("error: malformed hex", code == 1 and "neither a colour" in out, out)

    # WARNING when the alpha is (almost) empty or full
    rb.predict_alpha = lambda img, name: Image.new("L", img.size, 0)
    code, out = run({"image": "photo.png"}, ws)
    check("warning: empty subject", code == 0 and "WARNING: almost nothing" in out, out)
    rb.predict_alpha = lambda img, name: Image.new("L", img.size, 255)
    code, out = run({"image": "photo.png"}, ws)
    check("warning: whole picture as subject", code == 0 and "WARNING: almost the whole" in out, out)

    # model name override validation
    os.environ["MYAGENT_BGREMOVE_MODEL"] = "nope"
    code, out = run({"image": "photo.png"}, ws)
    check("error: unknown model name lists the choices", code == 1 and "unknown background-removal model" in out
          and "isnet-general-use" in out, out)
    del os.environ["MYAGENT_BGREMOVE_MODEL"]

if FAILURES:
    print(f"\n{len(FAILURES)} failure(s):", *FAILURES, sep="\n  ")
    sys.exit(1)
print("\nall ok")
