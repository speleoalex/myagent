#!/usr/bin/env python3
"""A stored attachment is named, with its path, in the user message itself.

Observed 2026-11-26 on a Telegram bot: a PDF was sent in one message and the
question about it in the next. The path was only in that first turn's
manifest (system prompt), which the stored conversation does not keep, so the
follow-up turn had nothing to go by and the model made a path up.

Run:  server/.venv/bin/python tests/test_attachment_path.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["MYAGENT_HOME"] = tempfile.mkdtemp(prefix="myagent-attpath-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from app.engine.executor import AgentExecutor  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def build(message, attachments, vision=False):
    stub = SimpleNamespace(model_config=SimpleNamespace(
        supports_vision=vision, supports_audio=False))
    return AgentExecutor._build_user_content(stub, message, attachments)


PDF = {"kind": "file", "name": "payslip.pdf", "data": "data:application/pdf;base64,AAAA",
       "path": "_attachments/payslip-f9ee6eca.pdf"}
IMAGE = {"kind": "image", "name": "photo.png", "data": "data:image/png;base64,AAAA",
         "path": "_attachments/photo-12345678.png"}
TEXT = {"kind": "text", "name": "notes.txt", "data": "hello"}

content = build("", [PDF])
check(content == "[Attached file: payslip.pdf, saved at _attachments/payslip-f9ee6eca.pdf]",
      "a file sent with no text still leaves its path in the message")

content = build("read this", [PDF, TEXT])
check(content.startswith("read this\n\n") and PDF["path"] in content,
      "the path follows the user's own words")
check("[File: notes.txt]\nhello" in content and "notes.txt, saved at" not in content,
      "a text attachment stays inline and claims no path")

content = build("look", [IMAGE], vision=True)
flat = AgentExecutor._flatten_content(content)
check(isinstance(content, list) and IMAGE["path"] in flat and "base64" not in flat,
      "an inlined image keeps its path once the history drops the pixels")

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
