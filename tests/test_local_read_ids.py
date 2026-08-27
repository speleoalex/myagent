#!/usr/bin/env python3
"""How local_read resolves the ids local_search minted.

Run: server/.venv/bin/python tests/test_local_read_ids.py

An id travels through the MODEL — printed by local_search, copied into a
local_read call — and the commonest way a small model mangles one is dropping
the trailing `:<n>`: `p:referti/visita.pdf:1` comes back as
`p:referti/visita.pdf`. Observed on a real chat, where the tool refused, the
model spent its retry on another search and then told the user the document did
not exist — while the document was the very one the first search had found.

So a `p:`/`f:` id with no line/page number opens the file AT THE TOP instead of
being refused. That is not the tolerant matching file_edit deliberately refuses
to do: there, guessing could rewrite the wrong line silently. Here the path is
exact, nothing is written, and the header states what was opened.

The limit is what keeps it honest — it only applies when the path RESOLVES:

  1. a complete id reads what it names;
  2. an id missing its page/line number opens the file at the top, and SAYS so
     in the header (`id p:x.pdf:1`);
  3. an INVENTED filename is still refused — otherwise a hallucinated id would
     quietly open whatever happened to be nearby;
  4. traversal is still refused;
  5. a filename that really does contain a colon and a number is untouched
     (`f:notes/chapter:3` is line 3 of notes/chapter, as it always was).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "server" / "tools" / "library" / "local_read" / "run"

failures = []


def check(label, cond):
    if not cond:
        failures.append(label)


def read(root, **args):
    env = {**os.environ,
           "MYAGENT_APP_DIR": str(ROOT / "server"),
           "MYAGENT_AGENT_DIR": str(root)}
    p = subprocess.run([str(RUN)], input=json.dumps(args), capture_output=True,
                       text=True, env=env, timeout=60)
    # A refusal goes to stderr with a non-zero exit; the registry folds that
    # back into the tool result, so the model sees both. Read them the same way.
    return p.stdout + p.stderr


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "notes").mkdir()
    (root / "notes" / "cave.md").write_text(
        "# Ingresso\n\nIl pozzo iniziale misura 12 metri.\n\n"
        "# Fondo\n\nIl ramo attivo prosegue per 300 metri.\n")
    # a file whose NAME ends in ':<digits>' — the case the rule must not touch
    (root / "notes" / "chapter:3").write_text("Testo del capitolo tre.\n")

    out = read(root, id="f:notes/cave.md:3")
    check("a complete id reads the passage it names", "pozzo iniziale" in out)

    out = read(root, id="f:notes/cave.md")
    check("an id with no line number opens the file instead of refusing",
          "NOTHING WAS READ" not in out and "pozzo iniziale" in out)
    check("...and the header says WHERE it opened",
          "id f:notes/cave.md:1" in out)

    out = read(root, id="f:notes/does-not-exist.md")
    check("an invented filename is still refused", "NOTHING WAS READ" in out)

    out = read(root, id="f:../../../etc/passwd")
    check("traversal is still refused", "NOTHING WAS READ" in out)
    # Check for passwd CONTENT, not for the word "root": the refusal legitimately
    # says "escapes the library root: ...", which a naive substring test reads
    # as a leak.
    check("...and nothing of the target leaks", ":x:0:0" not in out)

    # 5. `f:notes/chapter:3` must keep meaning "line 3 of notes/chapter",
    #    which is what it meant before: the new branch only runs when the
    #    trailing segment is NOT a digit.
    (root / "notes" / "chapter").write_text("a\nb\nterza riga\n")
    out = read(root, id="f:notes/chapter:3")
    check("a trailing number is still a line number, not part of the name",
          "terza riga" in out and "id f:notes/chapter:3" in out)

if failures:
    print(f"FAIL — {len(failures)} case(s)")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK — an id missing its page/line opens the file it names; an invented "
      "one and a traversal are still refused")
