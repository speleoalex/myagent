#!/usr/bin/env python3
"""edit_image must speak both wire formats correctly and never touch the source.

The tool is stdlib-only and talks HTTP, so this exercises it the way the
registry does (JSON on stdin, cwd = workspace, backend in the environment)
against a throwaway HTTP server that records what arrived and answers a 1x1
PNG. What is asserted is the CONTRACT with the two backends, the part that a
refactor drifts on silently:

  a1111  init_images[0] is the source, denoising_strength is the strength,
         the mask travels base64 with inpainting_mask_invert=0, and width/
         height from the model's stored options are NOT forwarded (they would
         resize the picture).
  openai multipart/form-data with image, mask and prompt as parts, no
         response_format when the host is api.openai.com, and strength/steps
         reported as ignored instead of dropped.

Plus: the result is a NEW file (the source is byte-identical afterwards, a
second edit does not overwrite the first) and a missing source or model is one
ERROR sentence, not a traceback.

Run: python3 tests/test_edit_image_payload.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

RUN = Path(__file__).resolve().parent.parent / "server/tools/images/edit_image/run"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
FAILURES: list[str] = []
RECEIVED: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        RECEIVED.append({"path": self.path, "ctype": self.headers.get("Content-Type", ""),
                         "auth": self.headers.get("Authorization", ""), "body": body})
        if self.path.startswith("/sdapi/"):
            out = {"images": [base64.b64encode(PNG).decode()]}
        else:
            out = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")


def run(args: dict, cwd: str, env: dict) -> tuple[int, str]:
    p = subprocess.run([str(RUN)], input=json.dumps(args), capture_output=True,
                       text=True, cwd=cwd, env={**os.environ, **env}, timeout=30)
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> int:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with tempfile.TemporaryDirectory() as ws:
        (Path(ws) / "bee.png").write_bytes(PNG)
        (Path(ws) / "mask.png").write_bytes(PNG)
        a1111 = {"MYAGENT_IMAGE_URL": base + "/sdapi/v1/txt2img",
                 "MYAGENT_IMAGE_EDIT_URL": base + "/sdapi/v1/img2img",
                 "MYAGENT_IMAGE_FORMAT": "a1111", "MYAGENT_IMAGE_NAME": "fake-sd",
                 "MYAGENT_IMAGE_KEY": "k123",
                 "MYAGENT_IMAGE_OPTIONS": json.dumps({"width": 512, "height": 512, "steps": 4,
                                                      "cfg_scale": 1, "sampler": "euler_a"})}

        print("a1111 img2img")
        rc, out = run({"image": "bee.png", "prompt": "a bee at night", "strength": 0.35,
                       "steps": 20, "seed": 7, "negative_prompt": "text"}, ws, a1111)
        req = RECEIVED[-1]
        body = json.loads(req["body"])
        check("exit 0", rc == 0, out)
        check("POST /sdapi/v1/img2img", req["path"] == "/sdapi/v1/img2img", req["path"])
        check("bearer key from env", req["auth"] == "Bearer k123", req["auth"])
        check("init_images[0] is the source", body.get("init_images") == [base64.b64encode(PNG).decode()])
        check("denoising_strength = strength", body.get("denoising_strength") == 0.35, str(body.get("denoising_strength")))
        check("steps/seed/negative forwarded", (body.get("steps"), body.get("seed"), body.get("negative_prompt")) == (20, 7, "text"))
        check("cfg_scale/sampler_name from options", (body.get("cfg_scale"), body.get("sampler_name")) == (1, "euler_a"))
        check("width/height NOT forwarded", "width" not in body and "height" not in body, str(sorted(body)))
        check("no mask -> no mask field", "mask" not in body)
        check("result is bee-edited.png", (Path(ws) / "bee-edited.png").is_file(), out)
        check("source untouched", (Path(ws) / "bee.png").read_bytes() == PNG)
        check("resource marker printed", "[[resource:bee-edited.png|image/png|" in out, out)

        print("a1111 inpaint + never overwrite")
        rc, out = run({"image": "bee.png", "prompt": "a flower", "mask": "mask.png"}, ws, a1111)
        body = json.loads(RECEIVED[-1]["body"])
        check("exit 0", rc == 0, out)
        check("mask travels base64", body.get("mask") == base64.b64encode(PNG).decode())
        check("inpainting_mask_invert = 0", body.get("inpainting_mask_invert") == 0)
        check("default strength 0.6", body.get("denoising_strength") == 0.6, str(body.get("denoising_strength")))
        check("second edit -> bee-edited-2.png", (Path(ws) / "bee-edited-2.png").is_file(), out)

        print("openai edits")
        openai = {"MYAGENT_IMAGE_URL": base + "/v1/images/generations",
                  "MYAGENT_IMAGE_EDIT_URL": base + "/v1/images/edits",
                  "MYAGENT_IMAGE_FORMAT": "openai", "MYAGENT_IMAGE_NAME": "fake-openai",
                  "MYAGENT_IMAGE_MODEL": "gpt-image-1"}
        rc, out = run({"image": "bee.png", "prompt": "a bee in winter", "mask": "mask.png",
                       "strength": 0.5, "steps": 4, "filename": "winter"}, ws, openai)
        req = RECEIVED[-1]
        check("exit 0", rc == 0, out)
        check("POST /v1/images/edits", req["path"] == "/v1/images/edits", req["path"])
        check("multipart/form-data", req["ctype"].startswith("multipart/form-data; boundary="), req["ctype"])
        boundary = req["ctype"].split("boundary=")[1].encode()
        parts = [p for p in req["body"].split(b"--" + boundary) if p.strip() and p.strip() != b"--"]
        names = {}
        for part in parts:
            head, _, payload = part.partition(b"\r\n\r\n")
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            names[name] = (head, payload[:-2])
        check("parts image, mask, prompt, n, model, response_format",
              set(names) == {"image", "mask", "prompt", "n", "model", "response_format"}, str(sorted(names)))
        check("image part carries the bytes + content type",
              names["image"][1] == PNG and b"Content-Type: image/png" in names["image"][0])
        check("prompt part", names["prompt"][1] == b"a bee in winter")
        check("model part", names["model"][1] == b"gpt-image-1")
        check("strength/steps reported as ignored", "ignored" in out and "strength" in out and "steps" in out, out)
        check("filename honoured", (Path(ws) / "winter.png").is_file(), out)

        print("errors are sentences")
        rc, out = run({"image": "nope.png", "prompt": "x"}, ws, a1111)
        check("missing source -> ERROR, no traceback", rc == 1 and out.startswith("ERROR:") and "Traceback" not in out, out)
        rc, out = run({"image": "bee.png", "prompt": "x"}, ws, {"MYAGENT_IMAGE_URL": "", "MYAGENT_IMAGE_EDIT_URL": ""})
        check("no model -> points at Settings", rc == 1 and "Settings" in out, out)
        rc, out = run({"image": "bee.png", "prompt": ""}, ws, a1111)
        check("empty prompt -> ERROR", rc == 1 and out.startswith("ERROR:"), out)
    srv.shutdown()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
