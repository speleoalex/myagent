"""Install, update and remove extension packages (plugins, and what plugins
themselves extend, such as the connectors plugin's channels).

A package is a directory with a JSON manifest. It comes either from GitHub — a
``repo`` + ``ref`` + ``subdir`` triple, fetched as a tarball over HTTPS, so
neither git nor a checkout is needed and a service-account install can do it —
or from a local directory (the checkout, for ``connectors/install.sh`` and
``update.sh``). The triple is what makes one repository a catalog of several
packages: today every entry of ``server/plugins-index.json`` points into the
myagent repository itself.

This is the ONE code path for installing a package. The shell scripts call the
CLI at the bottom instead of carrying a second copy of the rules:

    server/.venv/bin/python -m app.plugin_installer install --path connectors/plugin

Rules, each one inherited from a past incident (see docs/PLUGINS.md):

- **Never --upgrade.** A package's requirements must not move the core's
  dependencies underneath a working install.
- **Prove the core still imports** before the new code replaces the old one.
- **Swap, don't overwrite.** The new tree is staged beside the target under a
  dot-name (which every loader skips) and renamed into place; the previous one
  is kept as ``.<id>.prev`` so a package that fails to load can be rolled back.
- **A package is arbitrary code running with every secret this server holds.**
  Nothing here makes an untrusted package safe: the checks below (HTTPS only,
  validated ids, no path may leave the target) protect the INSTALL step, and the
  resolved commit is recorded so what was installed can be audited.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from app import config
from app.ids import check_id

log = logging.getLogger("myagent.plugin_installer")

# What the core promises to plugins (docs/PLUGINS.md, "What a plugin may import
# from the core"). Bumped when that surface changes incompatibly; a manifest
# asking for a newer one (``requires_api``) is refused with "update MyAgent
# first" instead of being installed to fail at import.
PLUGIN_API = 1

PLUGIN_MANIFEST = "plugin.json"
PROVENANCE_FILE = ".installed.json"

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT = 60
INDEX_TIMEOUT = 10

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")

Log = Callable[[str], None]


class InstallError(Exception):
    """A refusal or failure whose message is safe and useful to show verbatim."""


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

def github_source(repo: str, ref: str = "main", subdir: str = "") -> dict:
    """Validate and normalize a GitHub source. Every part ends up in a URL or
    in a path comparison, so each is held to a charset that can do neither
    harm; ``..`` is refused outright rather than normalized away."""
    repo, ref = (repo or "").strip(), (ref or "").strip() or "main"
    # Accept the URL people paste, store the owner/name it contains.
    repo = re.sub(r"^https://github\.com/", "", repo).removesuffix(".git").strip("/")
    subdir = (subdir or "").strip().strip("/")
    if not _REPO.match(repo) or ".." in repo:
        raise InstallError("repo must look like 'owner/name' (a GitHub repository)")
    if not _REF.match(ref) or ".." in ref:
        raise InstallError("ref must be a branch, tag or commit")
    if subdir and (".." in subdir.split("/") or subdir.startswith("/")
                   or "\\" in subdir):
        raise InstallError("subdir must be a relative path inside the repository")
    return {"repo": repo, "ref": ref, "subdir": subdir}


def _download(url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "myagent-plugins"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, \
            open(target, "wb") as out:
        total = 0
        while chunk := response.read(1 << 16):
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise InstallError("the repository archive is too large")
            out.write(chunk)


def _extract_subdir(archive: Path, subdir: str, out: Path) -> str:
    """Extract ``<top>/<subdir>/`` of a GitHub tarball into ``out``; return the
    commit it was cut from.

    Written by hand instead of ``extractall``: Python 3.10 (the minimum we
    support) has no extraction filter, and only regular files and directories
    are wanted anyway — a link is how an archive writes outside its target.
    """
    wanted = tuple(p for p in subdir.split("/") if p)
    total = 0
    found = False
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            parts = tuple(p for p in member.name.split("/") if p)[1:]  # drop <top>
            if parts[:len(wanted)] != wanted or len(parts) == len(wanted):
                continue
            relative = parts[len(wanted):]
            if any(p in ("..", "") for p in relative) or "__pycache__" in relative:
                continue
            target = out.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                found = True
                continue
            if not member.isfile():
                continue  # symlinks, devices, fifos: never
            total += member.size
            if total > MAX_PACKAGE_BYTES:
                raise InstallError("the package is too large")
            source = tar.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            # Keep the executable bit and nothing else of the archived mode.
            target.chmod(0o755 if member.mode & 0o100 else 0o644)
            found = True
        # `git archive` (what GitHub serves) records the commit in the global
        # pax header, which saves an API call to resolve a branch name.
        commit = str(tar.pax_headers.get("comment", ""))
    if not found:
        raise InstallError(f"'{subdir or '/'}' was not found in the repository")
    return commit


def _fetch(source: dict, work: Path, say: Log) -> tuple[Path, dict]:
    """Materialize ``source`` as a directory under ``work``; return it with the
    provenance to record."""
    if source.get("path"):
        path = Path(source["path"]).expanduser().resolve()
        if not path.is_dir():
            raise InstallError(f"no such directory: {path}")
        return path, {"path": str(path)}
    src = github_source(source.get("repo", ""), source.get("ref", ""),
                        source.get("subdir", ""))
    url = f"https://codeload.github.com/{src['repo']}/tar.gz/{src['ref']}"
    say(f"Downloading {src['repo']}@{src['ref']}…")
    archive = work / "source.tar.gz"
    try:
        _download(url, archive)
    except InstallError:
        raise
    except Exception as e:
        raise InstallError(f"download failed ({type(e).__name__}: {e})") from e
    tree = work / "tree"
    tree.mkdir()
    try:
        commit = _extract_subdir(archive, src["subdir"], tree)
    except tarfile.TarError as e:
        raise InstallError(f"not a valid archive: {e}") from e
    if commit:
        say(f"Commit {commit[:12]}")
    return tree, {**src, "commit": commit}


# --------------------------------------------------------------------------- #
# Installed packages
# --------------------------------------------------------------------------- #

def read_json(path: Path) -> dict:
    """A JSON object from disk, or {} — a missing or broken manifest must show
    up as "unknown version", never as an exception in a listing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _staging(parent: Path, package_id: str, suffix: str) -> Path:
    return parent / f".{package_id}.{suffix}"


def installed_info(directory: Path, manifest_name: str = PLUGIN_MANIFEST) -> dict:
    """What is known about an installed package without importing it."""
    manifest = read_json(directory / manifest_name)
    return {
        "name": manifest.get("name") or manifest.get("label") or "",
        "version": str(manifest.get("version") or ""),
        "description": manifest.get("description") or "",
        "source": read_json(directory / PROVENANCE_FILE),
        "can_rollback": _staging(directory.parent, directory.name, "prev").is_dir(),
    }


def _install_requirements(tree: Path, say: Log) -> None:
    """Every requirements.txt of the package, into the interpreter that is
    running — which is the server's own virtualenv, from the API and from the
    scripts alike. A failure is a warning: the package is still installed and
    the part that needed the dependency reports itself as degraded."""
    for req in sorted(tree.rglob("requirements.txt")):
        lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
        if not any(line.strip() and not line.strip().startswith("#") for line in lines):
            continue  # comments only
        name = req.parent.name
        say(f"Installing dependencies for '{name}' (can be hundreds of MB)…")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q",
             "--disable-pip-version-check", "-r", str(req)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        if result.returncode != 0:
            say(f"WARNING: dependencies for '{name}' could not be installed; "
                f"that part degrades.\n{result.stdout[-2000:]}")


def _core_imports() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import fastapi, httpx, pydantic"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0


def install(source: dict, dest_parent: Path, *,
            manifest_name: str = PLUGIN_MANIFEST, id_key: str = "id",
            required: tuple[str, ...] = ("plugin.py",),
            expect_id: str = "", say: Log = log.info) -> dict:
    """Install the package ``source`` describes into ``dest_parent/<id>``.

    ``source`` is ``{"repo","ref","subdir"}`` or ``{"path"}``. The id comes from
    the package's own manifest (``id_key``), and when the caller already knows
    which package it asked for (``expect_id``, a catalog entry) a different one
    is refused: an index entry must not be able to install over another package.
    Returns what ``installed_info`` reports plus the id.
    """
    dest_parent = Path(dest_parent)
    with tempfile.TemporaryDirectory(prefix="myagent-install-") as tmp:
        tree, provenance = _fetch(source, Path(tmp), say)
        manifest = read_json(tree / manifest_name)
        if not manifest:
            raise InstallError(f"the package has no valid {manifest_name}")
        try:
            package_id = check_id(str(manifest.get(id_key) or ""))
        except ValueError as e:
            raise InstallError(f"{manifest_name}: '{id_key}' {e}") from e
        if expect_id and package_id != expect_id:
            raise InstallError(
                f"expected '{expect_id}' but the package declares '{package_id}'")
        for name in required:
            if not (tree / name).is_file():
                raise InstallError(f"the package has no {name}")
        try:
            needs = int(manifest.get("requires_api") or 0)
        except (TypeError, ValueError):
            needs = 0
        if needs > PLUGIN_API:
            raise InstallError(
                f"'{package_id}' needs plugin API {needs}, this MyAgent provides "
                f"{PLUGIN_API}: update MyAgent first")

        dest_parent.mkdir(parents=True, exist_ok=True)
        dest = dest_parent / package_id
        new = _staging(dest_parent, package_id, "new")
        prev = _staging(dest_parent, package_id, "prev")
        shutil.rmtree(new, ignore_errors=True)
        say(f"Installing '{package_id}' to {dest}")
        shutil.copytree(tree, new, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", PROVENANCE_FILE))
        try:
            (new / PROVENANCE_FILE).write_text(json.dumps(
                {**provenance, "installed_at": int(time.time())}, indent=2))
            _install_requirements(new, say)
            if not _core_imports():
                raise InstallError(
                    "the server's virtualenv no longer imports after installing "
                    "the dependencies: NOT activating the package. Reinstall "
                    "MyAgent (./install.sh) to repair the environment.")
            if dest.exists():
                shutil.rmtree(prev, ignore_errors=True)
                dest.rename(prev)
            new.rename(dest)
        finally:
            shutil.rmtree(new, ignore_errors=True)
    say(f"Installed '{package_id}'.")
    return {"id": package_id, **installed_info(dest, manifest_name)}


def _package_dir(dest_parent: Path, package_id: str) -> Path:
    try:
        return Path(dest_parent) / check_id(package_id)
    except ValueError as e:
        raise InstallError(str(e)) from e


def uninstall(dest_parent: Path, package_id: str) -> None:
    """Remove the package's CODE (and its rollback copy). Its state lives
    elsewhere by contract and is never touched."""
    dest = _package_dir(dest_parent, package_id)
    if not dest.is_dir():
        raise InstallError(f"'{package_id}' is not installed")
    shutil.rmtree(dest)
    shutil.rmtree(_staging(dest.parent, package_id, "prev"), ignore_errors=True)


def rollback(dest_parent: Path, package_id: str) -> None:
    """Swap the package with the version it replaced (so a rollback can itself
    be rolled back)."""
    dest = _package_dir(dest_parent, package_id)
    prev = _staging(dest.parent, package_id, "prev")
    if not prev.is_dir():
        raise InstallError(f"no previous version of '{package_id}' is kept")
    aside = _staging(dest.parent, package_id, "new")
    shutil.rmtree(aside, ignore_errors=True)
    if dest.exists():
        dest.rename(aside)
    prev.rename(dest)
    if aside.exists():
        aside.rename(prev)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def load_index() -> dict:
    """The catalog of installable packages: ``{"entries": [...], "origin"}``.

    Read from GitHub so a package published after this install shows up, with
    the copy shipped in the install dir as the fallback — offline is this
    project's normal case, and there the bundled list is still correct for
    installing from a local network mirror or simply for showing what exists.
    """
    origin, data = "remote", {}
    try:
        request = urllib.request.Request(
            config.PLUGIN_INDEX_URL, headers={"User-Agent": "myagent-plugins"})
        with urllib.request.urlopen(request, timeout=INDEX_TIMEOUT) as response:
            data = json.loads(response.read(1 << 20))
    except Exception as e:
        log.info("Plugin index not reachable (%s): using the bundled copy", e)
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        origin, data = "bundled", read_json(config.PLUGIN_INDEX_FILE)
    entries = []
    for raw in data.get("packages") or []:
        try:
            entry = {
                "id": check_id(str(raw["id"])),
                "kind": str(raw.get("kind") or "plugin"),
                # name and description: a string, or {"en": …, "it": …}.
                "name": raw.get("name") or str(raw["id"]),
                "description": raw.get("description") or "",
                "version": str(raw.get("version") or ""),
                **github_source(raw["repo"], raw.get("ref", ""), raw.get("subdir", "")),
            }
        except (KeyError, TypeError, ValueError, InstallError) as e:
            log.warning("Plugin index: entry skipped (%s)", e)
            continue
        entries.append(entry)
    return {"entries": entries, "origin": origin}


def catalog(kind: str, dest_parent: Path, manifest_name: str = PLUGIN_MANIFEST) -> dict:
    """The index entries of one kind, each with what is installed locally."""
    index = load_index()
    entries = []
    for entry in index["entries"]:
        if entry["kind"] != kind:
            continue
        directory = Path(dest_parent) / entry["id"]
        installed = directory.is_dir()
        version = installed_info(directory, manifest_name)["version"] if installed else ""
        entries.append({
            **entry, "installed": installed, "installed_version": version,
            "update_available": installed and bool(entry["version"])
                                and entry["version"] != version,
        })
    return {"entries": entries, "origin": index["origin"]}


def find_entry(kind: str, package_id: str) -> dict:
    for entry in load_index()["entries"]:
        if entry["kind"] == kind and entry["id"] == package_id:
            return entry
    raise InstallError(f"'{package_id}' is not in the catalog")


# --------------------------------------------------------------------------- #
# The one background job
# --------------------------------------------------------------------------- #

class InstallJob:
    """At most one install at a time, process-wide: they all run pip in the
    same virtualenv. Runs in a thread (download, extraction and pip are
    blocking) and keeps its log for the page that started it to poll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "idle"  # idle | running | done | error
        self.target = ""
        self.error = ""
        self.lines: list[str] = []

    def say(self, line: str) -> None:
        self.lines.append(line)
        log.info("%s", line)
        try:
            config.PLUGIN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(config.PLUGIN_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass  # the log file is a convenience; the job is not

    def start(self, target: str, work: Callable[[Log], object]) -> None:
        """Run ``work(say)`` in the background. Raises InstallError when another
        install is under way."""
        with self._lock:
            if self.state == "running":
                raise InstallError(f"another install is running ({self.target})")
            self.state, self.target, self.error, self.lines = "running", target, "", []
        threading.Thread(target=self._run, args=(work,), daemon=True,
                         name="plugin-install").start()

    def _run(self, work: Callable[[Log], object]) -> None:
        try:
            work(self.say)
            self.state = "done"
        except InstallError as e:
            self.error = str(e)
        except Exception as e:  # a bug here must still end the job
            log.exception("Install of '%s' crashed", self.target)
            self.error = f"{type(e).__name__}: {e}"
        if self.error:
            self.say(f"ERROR: {self.error}")
            self.state = "error"

    def status(self) -> dict:
        return {"state": self.state, "target": self.target, "error": self.error,
                "log": "\n".join(self.lines)[-6000:]}


JOB = InstallJob()


# --------------------------------------------------------------------------- #
# CLI — what connectors/install.sh and update.sh call
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.plugin_installer",
        description="Install a MyAgent plugin from a local directory or GitHub.")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("install")
    cmd.add_argument("--path", help="a local plugin directory (holds plugin.json)")
    cmd.add_argument("--repo", help="GitHub repository, owner/name")
    cmd.add_argument("--ref", default="main")
    cmd.add_argument("--subdir", default="")
    args = parser.parse_args(argv)
    if bool(args.path) == bool(args.repo):
        parser.error("give exactly one of --path or --repo")
    source = ({"path": args.path} if args.path
              else {"repo": args.repo, "ref": args.ref, "subdir": args.subdir})
    try:
        install(source, config.PLUGINS_DIR, say=print)
    except InstallError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
