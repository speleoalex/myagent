#!/usr/bin/env python3
"""The package installer: what it accepts, what it refuses, what it leaves behind.

`app/plugin_installer.py` is the one code path that puts third-party code where
the server will run it — from Settings, from the Channels page and from
`connectors/install.sh` alike. Everything pinned here fails silently when it
breaks:

  * the archive is extracted by hand (Python 3.10 has no extraction filter), so
    a link or a `..` member must be dropped, never followed;
  * an index entry must not be able to install over ANOTHER package (the id in
    the package's own manifest has to match what was asked for);
  * the previous version survives as a rollback, and a failed install leaves the
    working one untouched;
  * the shipped index and the manifest it points at agree on the version —
    otherwise the UI offers an "update" forever.

The network is faked at `_download` (a tarball shaped like GitHub's: one top
folder, the commit in the pax header) and pip is never run: the fixture has no
requirements. State root is a temporary MYAGENT_HOME, set before the import.

Run:  server/.venv/bin/python tests/test_plugin_installer.py
"""
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="myagent-installer-"))
os.environ["MYAGENT_HOME"] = str(HOME)
os.environ["MYAGENT_PLUGIN_INDEX"] = "http://127.0.0.1:9/unreachable.json"

sys.path.insert(0, str(ROOT / "server"))

from app import config, plugin_installer as pi  # noqa: E402
from app.plugin_installer import InstallError  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def refused(fn, label: str, needle: str = "") -> None:
    try:
        fn()
    except InstallError as e:
        check(needle in str(e), f"{label} (refused: {e})")
    else:
        check(False, f"{label} (was NOT refused)")


COMMIT = "0123456789abcdef0123456789abcdef01234567"


def github_tarball(files: dict[str, str], *, links: dict[str, str] | None = None) -> bytes:
    """A tar.gz as codeload.github.com serves it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": COMMIT}) as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"repo-main/{name}")
            info.size = len(data)
            info.mode = 0o755 if name.endswith("run") else 0o644
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(f"repo-main/{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buf.getvalue()


def serve(payload: bytes) -> None:
    pi._download = lambda url, target: Path(target).write_bytes(payload)


def manifest(version: str, **extra) -> str:
    return json.dumps({"id": "demo", "name": "Demo", "version": version, **extra})


SOURCE = {"repo": "someone/repo", "ref": "main", "subdir": "pkg"}
DEST = HOME / "plugins"

print("github_source")
check(pi.github_source("https://github.com/a/b.git", "", "/x/y/")
      == {"repo": "a/b", "ref": "main", "subdir": "x/y"}, "a pasted URL is normalized")
refused(lambda: pi.github_source("a/b/../c"), "repo with a path is refused")
refused(lambda: pi.github_source("a/b", "main", "../etc"), "subdir escaping the repo is refused")
refused(lambda: pi.github_source("a/b", "x y"), "ref with a space is refused")

print("install from an archive")
secret = HOME / "secret.txt"
secret.write_text("token")
serve(github_tarball(
    {"pkg/plugin.json": manifest("1.0"), "pkg/plugin.py": "def register(app): pass\n",
     "pkg/bin/run": "#!/bin/sh\n", "pkg/__pycache__/x.pyc": "junk",
     "other/plugin.py": "OUTSIDE THE SUBDIR", "pkg/../../evil.py": "traversal"},
    links={"pkg/leak": str(secret)}))
info = pi.install(SOURCE, DEST, expect_id="demo", say=lambda _: None)
demo = DEST / "demo"
check(info["id"] == "demo" and info["version"] == "1.0", "id and version come from the manifest")
check((demo / "plugin.py").is_file(), "the subdir's files are installed")
check(os.access(demo / "bin" / "run", os.X_OK), "the executable bit is kept")
check(not (demo / "leak").exists(), "a symlink in the archive is dropped")
check(not (demo / "__pycache__").exists(), "bytecode is not installed")
check(not list(HOME.rglob("evil.py")), "a '..' member writes nothing")
check(sorted(p.name for p in DEST.iterdir()) == ["demo"], "no staging directory is left")
check(info["source"].get("commit") == COMMIT and info["source"]["repo"] == "someone/repo",
      "the resolved commit is recorded")

print("update, rollback, uninstall")
serve(github_tarball({"pkg/plugin.json": manifest("2.0"),
                      "pkg/plugin.py": "def register(app): pass\n"}))
info = pi.install(SOURCE, DEST, say=lambda _: None)
check(info["version"] == "2.0" and info["can_rollback"], "an update keeps the previous version")
check(not (demo / "bin").exists(), "files gone from the new version are gone (swap, not overlay)")
pi.rollback(DEST, "demo")
check(pi.installed_info(demo)["version"] == "1.0", "rollback restores the previous version")
pi.rollback(DEST, "demo")
check(pi.installed_info(demo)["version"] == "2.0", "a rollback can be rolled back")

print("refusals leave the installed version alone")
serve(github_tarball({"pkg/plugin.json": manifest("3.0", id="other"),
                      "pkg/plugin.py": ""}))
refused(lambda: pi.install(SOURCE, DEST, expect_id="demo", say=lambda _: None),
        "a package declaring another id is refused", "expected 'demo'")
serve(github_tarball({"pkg/plugin.json": manifest("3.0", id="../x"), "pkg/plugin.py": ""}))
refused(lambda: pi.install(SOURCE, DEST, say=lambda _: None), "an unsafe id is refused")
serve(github_tarball({"pkg/plugin.json": manifest("3.0")}))
refused(lambda: pi.install(SOURCE, DEST, say=lambda _: None),
        "a plugin without plugin.py is refused", "plugin.py")
serve(github_tarball({"pkg/plugin.json": manifest("3.0", requires_api=pi.PLUGIN_API + 1),
                      "pkg/plugin.py": ""}))
refused(lambda: pi.install(SOURCE, DEST, say=lambda _: None),
        "a package needing a newer core is refused", "update MyAgent")
serve(github_tarball({"elsewhere/plugin.json": manifest("3.0")}))
refused(lambda: pi.install(SOURCE, DEST, say=lambda _: None),
        "a subdir missing from the repo is refused", "not found")
serve(b"this is not a tarball")
refused(lambda: pi.install(SOURCE, DEST, say=lambda _: None), "a non-archive is refused")
check(pi.installed_info(demo)["version"] == "2.0", "after all refusals the installed version is intact")
check(sorted(p.name for p in DEST.iterdir()) == [".demo.prev", "demo"], "and no staging directory is left")

pi.uninstall(DEST, "demo")
check(not demo.exists() and not (DEST / ".demo.prev").exists(), "uninstall removes code and rollback copy")
refused(lambda: pi.uninstall(DEST, "../plugins"), "uninstall validates the id")

print("a channel: other manifest, other id key, no entry point")
serve(github_tarball({"pkg/channel.json": json.dumps({"type": "demochan", "version": "0.1"}),
                      "pkg/channel.py": ""}))
info = pi.install(SOURCE, HOME / "channels", manifest_name="channel.json",
                  id_key="type", required=(), say=lambda _: None)
check(info["id"] == "demochan" and (HOME / "channels" / "demochan" / "channel.py").is_file(),
      "installed under its type")

print("catalog")
index = pi.load_index()
check(index["origin"] == "bundled" and index["entries"], "offline, the bundled index is used")
for entry in index["entries"]:
    if entry["repo"] != "speleoalex/myagent":
        continue
    name = "channel.json" if entry["kind"] == "channel" else "plugin.json"
    shipped = pi.read_json(ROOT / entry["subdir"] / name)
    check(shipped.get("version") == entry["version"],
          f"index and {entry['subdir']}/{name} agree on the version ({entry['version']})")
    check(shipped.get("type" if entry["kind"] == "channel" else "id") == entry["id"],
          f"index and manifest agree on the id ({entry['id']})")
cat = pi.catalog("plugin", DEST)
check(all(not e["installed"] for e in cat["entries"]), "nothing is reported installed in an empty home")
refused(lambda: pi.find_entry("plugin", "nope"), "an id outside the catalog is refused")

shutil.rmtree(HOME, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("all passed")
