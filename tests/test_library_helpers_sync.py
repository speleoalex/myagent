#!/usr/bin/env python3
"""The two library tools duplicate helpers on purpose — this proves they agree.

``local_search`` and ``local_read`` cannot share a module: editing a native
tool copies ONE leaf folder into the user layer (copy-on-write), so a shared
import would break the moment either tool is overridden. The copies are
therefore deliberate, and the risk they carry is drift — which is not
theoretical, because the ids the two tools exchange are *computed*:

  * ``list_zims`` defines the ``z<N>`` index by its sort order,
  * ``pdf_pages`` + ``_pdf_trim`` define what page ``p:<file>:<N>`` means,
  * ``pdf_cache_dir`` / ``default_library_dir`` decide WHERE both of them look.

If any of those diverge, ``local_read`` resolves an id to a different document
than the one ``local_search`` offered, and nothing raises: the user just gets
the wrong article.

Compared as CODE, not as text: docstrings and comments are stripped first, so
each copy stays free to describe itself in its own words ("duplicated in
local_read" vs "duplicated from local_search"). That is also why the old
"keep them byte-for-byte in sync" comment could never have been true.

Run:  python3 tests/test_library_helpers_sync.py
"""
import ast
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "server" / "tools" / "library"
SEARCH = LIB / "local_search" / "search.py"
READ = LIB / "local_read" / "read.py"

# Helpers that MUST behave identically in both tools.
SHARED_FUNCS = [
    "myagent_home",         # where the runtime layout is rooted
    "default_library_dir",  # which library both tools read
    "pdf_cache_dir",        # which text-layer cache both tools share
    "_walk",                # symlink-following discovery (defines what exists)
    "list_zims",            # sort order == the z<N> id index
    "normalize_layout",     # extracted text must be char-for-char equal:
    "_pdf_trim",            # local_read's `offset` indexes into it
    "_pdf_cache_key",
    "_pdf_cache_write",
    "pdf_pages",            # page numbering behind every p: id
    # Office (OOXML). Same rule as the PDF helpers above and for the same
    # reason: local_search mints `f:<relpath>:<line>` from THIS text and
    # local_read indexes its `offset` into it, so a divergence opens a
    # different passage — or nothing — in silence.
    "read_text_file",       # the single seam both tools read a file through
    "office_text",
    "_xml_tag",
    "_xml_root",
    "_docx_text",
    "_docx_para_text",
    "_docx_heading_level",
    "_pptx_text",
    "_pptx_slide_order",    # numeric order == the slide number printed
    "_xlsx_text",
    "_xlsx_cell_text",
    "_xlsx_sheets",
    "_xlsx_shared_strings",
    "_xlsx_date_styles",
    "_xlsx_serial_to_date",
    "_xlsx_epoch_1904",
]

# Module-level constants the shared code reads. Identical bodies are not enough
# if the values they close over differ.
SHARED_CONSTS = ["MAX_TEXT_BYTES", "PDF_CACHE_MAX_BYTES", "BLOCK_TAGS", "SKIP_TAGS",
                 "OFFICE_EXTS", "MAX_OFFICE_CHARS", "MAX_OFFICE_UNCOMPRESSED",
                 "CELL_SEP", "_XLS_EPOCH_1900", "_XLS_EPOCH_1904",
                 "_XLS_DATE_FMT_IDS"]

# Present in both, DELIBERATELY different — listed so the divergence is a
# recorded decision rather than an oversight. local_read's copy also collects
# <img> tags, to deliver article images to the chat; local_search's must not
# (snippets have no use for images).
KNOWN_DIVERGENT = {"_TextExtractor", "main"}


def _strip_docstrings(node):
    """Drop docstring expressions everywhere in the tree (comments are already
    gone: they never reach the AST)."""
    for n in ast.walk(node):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module)):
            continue
        body = getattr(n, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            # Never leave an empty body behind (a docstring-only function).
            n.body = body[1:] or [ast.Pass()]
    return node


def _top_level(path):
    """{name: normalized code} for top-level defs and UPPERCASE constants."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = ast.unparse(_strip_docstrings(node))
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.isupper()):
            out[node.targets[0].id] = ast.unparse(node.value)
    return out


def main():
    search, read = _top_level(SEARCH), _top_level(READ)
    failures = []

    for name in SHARED_FUNCS + SHARED_CONSTS:
        in_search, in_read = search.get(name), read.get(name)
        if in_search is None or in_read is None:
            missing = "local_search" if in_search is None else "local_read"
            failures.append(
                f"{name}: missing from {missing} — if it was renamed or removed, "
                f"do the same in the other tool and update SHARED_FUNCS here")
        elif in_search != in_read:
            failures.append(
                f"{name}: DIFFERS between the two tools. They exchange computed "
                f"ids, so this makes local_read resolve to the wrong document. "
                f"Port the change to both copies.")

    # A helper that quietly appears in both files is a duplicate nobody
    # declared: catch it now, while making the copies agree is still cheap.
    undeclared = (set(search) & set(read)) - set(SHARED_FUNCS) \
        - set(SHARED_CONSTS) - KNOWN_DIVERGENT
    for name in sorted(undeclared):
        verdict = "already differs" if search[name] != read[name] else "identical today"
        failures.append(
            f"{name}: defined in BOTH tools but not declared here ({verdict}). "
            f"Add it to SHARED_FUNCS/SHARED_CONSTS, or to KNOWN_DIVERGENT with "
            f"a comment saying why the copies must differ.")

    checked = len(SHARED_FUNCS) + len(SHARED_CONSTS)
    if failures:
        print(f"FAIL — local_search and local_read have drifted ({len(failures)} problem(s)):\n")
        for f in failures:
            print(f"  * {f}")
        print("\nSee the note at the top of both tools' module docstrings.")
        return 1
    print(f"OK — {checked} duplicated helpers/constants agree between "
          f"local_search and local_read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
