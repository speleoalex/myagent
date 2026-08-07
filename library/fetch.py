#!/usr/bin/env python3
"""Download offline knowledge archives into a MyAgent library folder.

    library/fetch.py --list
    library/fetch.py --preset base
    library/fetch.py --lang it --preset base
    library/fetch.py --preset health --dest /media/usb/library
    library/fetch.py --lang it --only wikipedia-it,wikimed-it --budget-gb 12

Companion to `catalog.json`, which says what is worth having and why. This
script turns that curation into files on a disk you choose.

Standard library only, on purpose: it runs before the venv exists, from a
rescue shell, or on the machine that owns the external drive. Nothing here is
imported by the server.

Why it resolves URLs instead of reading them from the catalog: Kiwix archives
are date-stamped (`wikipedia_it_all_nopic_2026-05.zim`) and there is no
`latest` symlink, so a hard-coded URL list starts rotting the day it is
written. The current filename, size and SHA-256 all come from Kiwix at run
time; the catalog only names the archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ATOM = "{http://www.w3.org/2005/Atom}"
METALINK = "{urn:ietf:params:xml:ns:metalink}"
USER_AGENT = "myagent-library-fetch/1"
CHUNK = 1 << 20
RETRIES = 4
DEFAULT_CATALOG = Path(__file__).resolve().parent / "catalog.json"

# `_2026-05` before the extension: Kiwix's edition stamp. Used to pick the
# newest build of an archive and to spot an older one already on disk.
DATED = re.compile(r"^(?P<stem>.+)_(?P<date>\d{4}-\d{2})\.zim$")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024 or unit == "TB":
            return f"{nbytes:,.0f} {unit}" if unit in ("B", "KB") else f"{nbytes:,.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes} B"


def fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def get(url: str, timeout: int = 60, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


# --------------------------------------------------------------------------
# catalog + resolution
# --------------------------------------------------------------------------

def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"catalog not found: {path}")
    except json.JSONDecodeError as exc:
        fail(f"{path} is not valid JSON: {exc}")
    if not isinstance(data.get("entries"), list):
        fail(f"{path}: no 'entries' list")
    return data


def overlay_path(base: Path, lang: str) -> Path:
    """catalog.json + 'it' -> catalog.it.json (next to whatever --catalog gave)."""
    return base.with_name(f"{base.stem}.{lang}{base.suffix}")


def available_langs(base: Path) -> list[str]:
    """Language overlays sitting next to the international catalog."""
    return sorted(p.name[len(base.stem) + 1:-len(base.suffix)]
                  for p in base.parent.glob(f"{base.stem}.*{base.suffix}"))


def load_catalog(path: Path, langs: list[str]) -> dict:
    """International catalog plus the requested language overlays.

    Merge rule, and the reason the split exists: a new id is ADDED, a reused
    id REPLACES. So `catalog.it.json` can add Italian Wikipedia next to the
    English one (different corpora, you want both) while replacing `ifixit`
    outright (same guides translated — a second copy is 3.6 GB of nothing).
    """
    catalog = read_json(path)
    for lang in langs:
        extra_path = overlay_path(path, lang)
        if not extra_path.exists():
            have = available_langs(path)
            fail(f"no catalog for '{lang}' ({extra_path.name}). "
                 f"Available: {', '.join(have) if have else 'none'}")
        extra = read_json(extra_path)
        catalog.setdefault("presets", {}).update(extra.get("presets") or {})
        position = {e["id"]: i for i, e in enumerate(catalog["entries"])}
        for entry in extra["entries"]:
            if entry["id"] in position:
                catalog["entries"][position[entry["id"]]] = entry
            else:
                catalog["entries"].append(entry)
    return catalog


def fetch_kiwix_index(url: str, soft: bool = False) -> dict[str, list[dict]] | None:
    """Kiwix OPDS catalog -> {zim name: [candidate builds]}.

    One request (~4 MB) covers every entry in the run, which is also the only
    network call a --dry-run makes. `soft` degrades to None instead of exiting,
    so --list still prints the curation when there is no network.
    """
    print(f"Resolving current editions from {url.split('/catalog')[0]} ...", flush=True)
    try:
        with get(url, timeout=120) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if soft:
            print(f"  (offline: {exc} — sizes unknown)")
            return None
        fail(f"cannot reach the Kiwix catalog ({exc}). Offline? Pass --catalog-xml FILE.")
    return parse_kiwix_index(raw)


def parse_kiwix_index(raw: bytes) -> dict[str, list[dict]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        fail(f"malformed Kiwix catalog: {exc}")

    index: dict[str, list[dict]] = {}
    for entry in root.findall(f"{ATOM}entry"):
        def text(tag: str) -> str:
            node = entry.find(f"{ATOM}{tag}")
            return (node.text or "").strip() if node is not None else ""

        meta4 = size = ""
        for link in entry.findall(f"{ATOM}link"):
            if "x-zim" in (link.get("type") or ""):
                meta4, size = link.get("href") or "", link.get("length") or "0"
        if not meta4:
            continue

        name = text("name")
        filename = meta4.rsplit("/", 1)[-1].removesuffix(".meta4")
        stamp = DATED.match(filename)
        index.setdefault(name, []).append({
            "name": name,
            "flavour": text("flavour"),
            "title": text("title"),
            "lang": text("language"),
            "filename": filename,
            # .meta4 is a Metalink sidecar (mirrors + hashes); the archive
            # itself is the same URL without that suffix.
            "url": meta4.removesuffix(".meta4"),
            "meta4": meta4,
            "size": int(size or 0),
            # Without a full-text index libzim can only match titles, which
            # is a different tool for the agent. Worth saying out loud.
            "ftindex": "_ftindex:no" not in text("tags"),
            "articles": text("articleCount"),
            "date": stamp.group("date") if stamp else text("updated")[:7],
        })
    return index


def resolve(entry: dict, index: dict[str, list[dict]]) -> dict | None:
    """Pick the current build of one catalog entry."""
    if entry.get("url"):  # verbatim source, nothing to resolve
        filename = entry.get("filename") or entry["url"].rsplit("/", 1)[-1].split("?")[0]
        return {
            "filename": filename, "url": entry["url"], "meta4": "",
            "size": int(entry.get("size_mb", 0)) * 1000 * 1000,
            "ftindex": True, "flavour": "", "date": "", "articles": "",
            "title": entry.get("id", filename),
        }

    builds = index.get(entry.get("kiwix", ""), [])
    if not builds:
        return None
    wanted = entry.get("flavour")
    if wanted:
        builds = [b for b in builds if b["flavour"] == wanted]
    elif any(b["flavour"] == "" for b in builds):
        # An archive that publishes flavours AND a plain build: the plain one
        # is what an entry without `flavour` asked for.
        builds = [b for b in builds if b["flavour"] == ""]
    if not builds:
        return None
    return max(builds, key=lambda b: (b["date"], b["size"]))


def select(catalog: dict, args) -> list[dict]:
    entries = catalog["entries"]
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        by_id = {e["id"]: e for e in entries}
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            fail(f"unknown id(s): {', '.join(unknown)}. Try --list.")
        return [by_id[w] for w in wanted]

    chosen = entries
    if args.preset:
        presets = {p.strip() for p in args.preset.split(",") if p.strip()}
        unknown = presets - set(catalog.get("presets", {}))
        if unknown:
            fail(f"unknown preset(s): {', '.join(sorted(unknown))}. Try --list.")
        chosen = [e for e in chosen if presets & set(e.get("presets", []))]
    if args.topic:
        topics = {t.strip() for t in args.topic.split(",") if t.strip()}
        chosen = [e for e in chosen if e.get("topic") in topics]
    return chosen


# --------------------------------------------------------------------------
# disk state
# --------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def enrich(info: dict) -> dict:
    """Add the byte-exact size and SHA-256 from the Metalink sidecar.

    The OPDS catalog's `length` is rounded up to the KB (observed: 20925440
    advertised for a 20924451-byte archive), so it can size a plan but it
    cannot decide when a download is complete. The .meta4 can, and carries the
    checksum in the same few-KB request. Best-effort: if it is unreachable the
    entry keeps the approximate size and the download falls back to trusting
    the server's Content-Length.
    """
    if "sha256" in info:  # already enriched
        return info
    info.setdefault("sha256", None)
    info["exact"] = False
    if not info.get("meta4"):
        # A plain `url` entry: the origin's Content-Length is byte-exact even
        # though there is no sidecar to check against.
        try:
            req = urllib.request.Request(info["url"], method="HEAD",
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                length = int(resp.headers.get("Content-Length") or 0)
            if length:
                info["size"], info["exact"] = length, True
        except Exception:
            pass
        return info
    try:
        with get(info["meta4"], timeout=60) as resp:
            root = ET.fromstring(resp.read())
    except Exception:
        return info
    for node in root.iter(f"{METALINK}hash"):
        if node.get("type") == "sha-256":
            info["sha256"] = (node.text or "").strip()
    for node in root.iter(f"{METALINK}size"):
        if (node.text or "").strip().isdigit():
            info["size"] = int(node.text.strip())
            info["exact"] = True
    return info


def has_libzim() -> bool:
    """Is libzim importable by the interpreter the library tools run under?"""
    venv_python = Path(__file__).resolve().parent.parent / "server" / ".venv" / "bin" / "python"
    if venv_python.exists():
        return subprocess.run([str(venv_python), "-c", "import libzim"],
                              capture_output=True).returncode == 0
    try:
        import libzim  # noqa: F401
        return True
    except ImportError:
        return False


def complete(target: Path, info: dict) -> bool:
    """Is the file already on disk the whole archive?

    Only an exact size can answer that. Without one, a `.sha256` sidecar left
    by a previous successful run is the remaining evidence; anything else is
    treated as missing and downloaded again.
    """
    if info.get("exact"):
        return target.stat().st_size == info["size"]
    return target.with_name(target.name + ".sha256").exists()


def older_editions(dest: Path, filename: str) -> list[Path]:
    """Same archive, earlier stamp, already on disk.

    Never deleted — a 9 GB file the user may still be reading is not ours to
    remove — but silently keeping two editions is how a disk fills up.
    """
    stamp = DATED.match(filename)
    if not stamp:
        return []
    stem = stamp.group("stem")
    return sorted(p for p in dest.glob(f"{stem}_*.zim") if p.name != filename)


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------

def download(url: str, target: Path, size: int = 0, exact: bool = False,
             quiet: bool = False) -> None:
    """Resumable download to `target`, via a .part file.

    `size` drives the progress read-out; only an `exact` size (from the
    Metalink sidecar) is allowed to declare the result truncated, since the
    OPDS figure is rounded and would reject every complete file.
    """
    part = target.with_name(target.name + ".part")
    done = part.stat().st_size if part.exists() else 0
    if exact and done > size:  # stale partial left by another edition
        part.unlink()
        done = 0
    declared = 0

    for attempt in range(1, RETRIES + 1):
        try:
            headers = {"Range": f"bytes={done}-"} if done else {}
            with get(url, timeout=120, headers=headers) as resp:
                if done and resp.status != 206:
                    # Mirror ignored the range: start over rather than
                    # concatenate the whole file onto a partial one.
                    done = 0
                    part.unlink(missing_ok=True)
                declared = int(resp.headers.get("Content-Length") or 0) + done
                total = size if exact else (declared or size)
                mode = "ab" if done else "wb"
                last = time.monotonic()
                with part.open(mode) as out:
                    while True:
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        done += len(block)
                        now = time.monotonic()
                        if not quiet and now - last > 1.0:
                            pct = f"{100 * done / total:5.1f}%" if total else "  ?  "
                            print(f"\r    {pct}  {human(done)}", end="", flush=True)
                            last = now
            if not quiet:
                print(f"\r    100.0%  {human(done)}          ")
            break
        except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as exc:
            if attempt == RETRIES:
                raise
            done = part.stat().st_size if part.exists() else 0
            wait = 2 ** attempt
            print(f"\r    {type(exc).__name__}: retrying in {wait}s "
                  f"(attempt {attempt + 1}/{RETRIES})", flush=True)
            time.sleep(wait)

    got = part.stat().st_size
    want = size if exact else declared
    if want and got != want:
        raise OSError(f"truncated: got {got} bytes, expected {want}")
    part.replace(target)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def print_catalog(catalog: dict, index: dict[str, list[dict]] | None,
                  base: Path, loaded: list[str]) -> None:
    others = [x for x in available_langs(base) if x not in loaded]
    if loaded:
        print(f"\nLanguage overlays loaded: {', '.join(loaded)}")
    if others:
        print(f"\nOther language catalogs available: {', '.join(others)}"
              f"   (add --lang {others[0]})")
    print("\nPresets:")
    for name, desc in catalog.get("presets", {}).items():
        print(f"  {name:<10} {desc}")
    print(f"\n{'id':<24} {'lang':<5} {'size':>10}  {'ft':<3} presets")
    print("-" * 78)
    total = 0
    for entry in catalog["entries"]:
        info = resolve(entry, index) if index is not None else None
        size = info["size"] if info else 0
        total += size
        ftx = "" if info is None else ("yes" if info["ftindex"] else "NO")
        print(f"{entry['id']:<24} {entry.get('lang', ''):<5} "
              f"{human(size) if size else '?':>10}  {ftx:<3} "
              f"{','.join(entry.get('presets', [])) or '-'}")
        print(f"{'':<24} {entry.get('note', '')}")
    if index is not None:
        print("-" * 78)
        print(f"{'everything':<24} {'':<5} {human(total):>10}")
    print("\n  ft=NO: the archive ships without a full-text index, so local_search\n"
          "  falls back to matching titles only.\n")


def plan_line(entry: dict, info: dict | None, state: str) -> str:
    if info is None:
        return f"  [!!] {entry['id']:<24} not in the Kiwix catalog any more"
    flag = {"have": "==", "resume": ">>", "get": "->"}[state]
    ftx = "" if info["ftindex"] else "  (title-only search)"
    return (f"  [{flag}] {entry['id']:<24} {human(info['size']):>10}  "
            f"{info['filename']}{ftx}")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download offline knowledge archives into a MyAgent library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The library is just a folder: files can be moved, copied to a\n"
               "second disk or deleted at any time. Nothing indexes them but the\n"
               "agent, and it rescans on every search.")
    ap.add_argument("--dest", metavar="DIR",
                    default=os.environ.get("MYAGENT_LIBRARY") or str(
                        Path(os.environ.get("MYAGENT_HOME") or (Path.home() / "myagent")) / "library"),
                    help="destination folder (default: $MYAGENT_LIBRARY, "
                         "else $MYAGENT_HOME/library, else ~/myagent/library)")
    ap.add_argument("--preset", metavar="A,B", help="download a named preset (see --list)")
    ap.add_argument("--only", metavar="ID,ID", help="download these catalog ids")
    ap.add_argument("--topic", metavar="T,T", help="filter by topic")
    ap.add_argument("--lang", metavar="it,fr",
                    help="also load these language catalogs (catalog.<lang>.json) "
                         "on top of the international one")
    ap.add_argument("--budget-gb", type=float, metavar="N",
                    help="stop once this much has been downloaded this run")
    ap.add_argument("--list", action="store_true", help="show the catalog and exit")
    ap.add_argument("--urls", action="store_true",
                    help="print resolved download URLs and exit (feed to wget/aria2)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, download nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the SHA-256 check (it re-reads each file once)")
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="catalog.json to use")
    ap.add_argument("--catalog-xml", type=Path,
                    help="read the Kiwix OPDS catalog from a file instead of the network")
    args = ap.parse_args()

    langs = [x.strip() for x in (args.lang or "").split(",") if x.strip()]
    catalog = load_catalog(args.catalog, langs)
    if not langs:
        # A hint, not a default: guessing the language from the environment
        # would silently change what a scripted run downloads.
        system = (os.environ.get("LANG") or "")[:2].lower()
        if system and system != "en" and system in available_langs(args.catalog):
            print(f"note: a '{system}' catalog exists — add --lang {system} "
                  f"for material in that language")

    if args.catalog_xml:
        index = parse_kiwix_index(args.catalog_xml.read_bytes())
    else:
        index = fetch_kiwix_index(catalog.get("kiwix_catalog", ""), soft=args.list)

    if args.list:
        print_catalog(catalog, index, args.catalog, langs)
        return 0

    if not (args.preset or args.only or args.topic):
        # --lang alone only decides WHICH catalog is in play, never what to get.
        ap.error("nothing selected: pass --preset, --only or --topic (see --list)")

    entries = select(catalog, args)
    if not entries:
        print("Nothing matches that selection.")
        return 0

    dest = Path(args.dest).expanduser()
    resolved = [(e, resolve(e, index)) for e in entries]

    if args.urls:
        for _, info in resolved:
            if info:
                print(info["url"])
        return 0

    dest.mkdir(parents=True, exist_ok=True)

    # --- plan -------------------------------------------------------------
    print(f"\nDestination: {dest}")
    todo, need = [], 0
    for entry, info in resolved:
        if info is None:
            print(plan_line(entry, None, "get"))
            continue
        enrich(info)  # exact size + checksum, one small request per archive
        target = dest / info["filename"]
        part = target.with_name(target.name + ".part")
        if target.exists() and complete(target, info):
            print(plan_line(entry, info, "have"))
            continue
        state = "resume" if part.exists() else "get"
        print(plan_line(entry, info, state))
        need += max(0, info["size"] - (part.stat().st_size if part.exists() else 0))
        todo.append((entry, info, target))
        for old in older_editions(dest, info["filename"]):
            print(f"       older edition on disk: {old.name} ({human(old.stat().st_size)}) "
                  f"— delete it once the new one works")

    if not todo:
        print("\nEverything selected is already here.")
        return 0

    free = shutil.disk_usage(dest).free
    print(f"\n  {len(todo)} archive(s), {human(need)} to download — {human(free)} free on {dest}")
    if need > free:
        fail("not enough free space. Pick a smaller --preset, use --budget-gb, "
             "or point --dest at another disk.")
    if args.budget_gb:
        print(f"  budget: {args.budget_gb} GB — the run stops when it is reached")

    if args.dry_run:
        return 0
    if not args.yes:
        try:
            if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes", "s", "si"):
                return 1
        except EOFError:
            fail("no TTY to confirm on; pass --yes")

    # --- download ---------------------------------------------------------
    budget = int(args.budget_gb * 1000 ** 3) if args.budget_gb else None
    spent = failures = 0
    for n, (entry, info, target) in enumerate(todo, 1):
        if budget is not None and spent + info["size"] > budget:
            print(f"\n[{n}/{len(todo)}] {entry['id']}: skipped, over the {args.budget_gb} GB budget")
            continue
        print(f"\n[{n}/{len(todo)}] {entry['id']}  {info['filename']}  ({human(info['size'])})")
        try:
            download(info["url"], target, info["size"], info.get("exact", False))
            spent += info["size"]
        except KeyboardInterrupt:
            print("\n\nInterrupted. Re-run the same command to resume.")
            return 130
        except Exception as exc:
            print(f"    FAILED: {exc}")
            failures += 1
            continue

        if args.no_verify:
            continue
        want = info.get("sha256")
        if not want:
            print("    (no published checksum, skipped verification)")
            continue
        print("    verifying...", end="", flush=True)
        got = sha256_of(target)
        if got != want:
            print(f" MISMATCH\n    expected {want}\n    got      {got}")
            corrupt = target.with_suffix(target.suffix + ".corrupt")
            target.replace(corrupt)
            print(f"    moved to {corrupt.name} — delete it and re-run")
            failures += 1
            continue
        # sha256sum-compatible, so `sha256sum -c *.sha256` works later.
        target.with_name(target.name + ".sha256").write_text(f"{want}  {target.name}\n")
        print(" ok")

    print(f"\nDone: {human(spent)} downloaded to {dest}"
          + (f", {failures} failure(s)" if failures else ""))

    # A .zim in the folder is inert until libzim is installed — and the check
    # has to run in the interpreter the tools use, not in whichever python
    # happened to launch this script.
    if spent and any(dest.glob("*.zim")) and not has_libzim():
        venv = Path(__file__).resolve().parent.parent / "server" / ".venv"
        pip = f"{venv}/bin/pip" if venv.is_dir() else "pip"
        print(f"\nZIM archives need libzim to be searchable:\n  {pip} install libzim")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
