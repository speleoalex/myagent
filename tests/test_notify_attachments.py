#!/usr/bin/env python3
"""notify_user with a subject and attachments — the core side.

Born from a live turn: asked for "a mail with subject ciao", the model wrote
"Oggetto: ciao" as the first line of the body, because the body was the only
parameter there was. Now there are ``subject`` and ``attachments``, and the
connector decides how they render (``notify``). What is pinned here, all
against fakes (no plugin, no transport, temporary workspace):

  * the attachment argument in every shape a model writes it (array, one
    string with commas or newlines, quoted, absolute as file_write prints it,
    ``~``, relative to the workspace) resolves to the same files;
  * a path outside the workspace and the agent's folder is REFUSED and named,
    a missing file is named, the caps (5 files, 15 MB) are enforced — and one
    bad path among good ones does not stop the good ones;
  * the connector's ``notify`` receives subject and files separately, and the
    files it could not carry come back in the result ("NOT delivered");
  * a connector WITHOUT ``notify`` (older plugin) still gets the message, with
    the subject folded into the text, and the files reported as not delivered;
  * the chat history records what was said: subject first, then the text,
    then the names of the files that arrived;
  * an http(s) URL is downloaded and attached (named by Content-Disposition,
    by the path, by the type), but never from a private address — redirect
    hops included — never a web page, never past the size cap;
  * a text that PROMISES an attachment ("in allegato", "please find
    attached") with none is refused before anything is sent, while a text
    saying there is none ("non ho potuto allegare") goes out.

Run:  server/.venv/bin/python tests/test_notify_attachments.py
"""
import asyncio
import http.server
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="myagent-notifytest-"))
os.environ["MYAGENT_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT / "server"))

from app import config as app_config  # noqa: E402
from app.tools import internal  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


WS = HOME / "workspace"
WS.mkdir(parents=True, exist_ok=True)
app_config.WORKSPACE_DIR = WS
AGENT_DIR = HOME / "agentdir"
AGENT_DIR.mkdir()
OUTSIDE = HOME / "outside"
OUTSIDE.mkdir()

(WS / "report.pdf").write_bytes(b"%PDF-1.4 fake")
(WS / "sub").mkdir()
(WS / "sub" / "photo.jpg").write_bytes(b"\xff\xd8 fake jpeg")
(WS / "with space.txt").write_text("ciao")
(AGENT_DIR / "notes.md").write_text("# notes")
(OUTSIDE / "secret.txt").write_text("nope")
(WS / "escape").symlink_to(OUTSIDE / "secret.txt")
for i in range(6):
    (WS / f"f{i}.txt").write_text(str(i))


class FakeConnector:
    """A chat-like connector with the new hook: records what it was handed."""

    def __init__(self, cannot_carry=()):
        self.calls: list[dict] = []
        self.cannot_carry = set(cannot_carry)

    async def notify(self, chat_id, text, subject="", files=None):
        files = list(files or [])
        self.calls.append(dict(chat_id=chat_id, text=text, subject=subject, files=files))
        return True, [n for n, _, _ in files if n in self.cannot_carry]

    def session_id_for(self, chat_id):
        return f"fake:{chat_id}"


class OldConnector:
    """A plugin predating ``notify``: only ``send`` (and ``send_file``)."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.files: list[tuple] = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    async def send_file(self, chat_id, name, data, mime, title):
        self.files.append((chat_id, name))
        return True

    def session_id_for(self, chat_id):
        return f"old:{chat_id}"


class FakeNamed:
    """Enough of NamedSessionStore for _log_to_channel."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}

    def lock(self, sid):
        return asyncio.Lock()

    def get(self, sid, agent_id):
        return self.sessions.setdefault(sid, {"messages": [], "conversation": []})

    def save_rotating(self, sid, session):
        self.sessions[sid] = session


def state_for(connector):
    manager = SimpleNamespace(get_connector=lambda bid: connector if bid == "tg" else None)
    return SimpleNamespace(connectors=SimpleNamespace(manager=manager))


def executor(folder=None):
    agent = SimpleNamespace(id="a", autonomous=None,
                            folder=SimpleNamespace(path=str(folder)) if folder else None)
    return SimpleNamespace(agent=agent)


def test_split_paths():
    print("Attachment argument shapes:")
    sp = internal._split_paths
    check(sp(None) == [] and sp("") == [] and sp("none") == [], "empty / placeholder = no files")
    check(sp(["a.txt", "b.pdf"]) == ["a.txt", "b.pdf"], "JSON array kept as is")
    check(sp("a.txt, b.pdf;c.png\nd.jpg") == ["a.txt", "b.pdf", "c.png", "d.jpg"],
          "one string split on commas, semicolons and newlines")
    check(sp('["a.txt", "with space.txt"]') == ["a.txt", "with space.txt"],
          "a stringified JSON array is unwrapped, spaces inside a path kept")
    check(sp(["a.txt", "a.txt"]) == ["a.txt"], "duplicates collapsed")


def test_resolve():
    print("Path resolution and containment:")
    rf = internal._resolve_notify_files
    files, problems = rf(["report.pdf", "sub/photo.jpg", "with space.txt"], executor())
    names = [f[0] for f in files]
    check(names == ["report.pdf", "photo.jpg", "with space.txt"] and not problems,
          f"relative paths resolve against the workspace: {names}")
    check(files[0][2] == "application/pdf" and files[1][2] == "image/jpeg",
          "mime guessed from the name")
    check(files[0][1] == b"%PDF-1.4 fake", "bytes are the file's content")

    files, problems = rf([str(WS / "report.pdf")], executor())
    check(len(files) == 1 and not problems, "absolute path inside the workspace (as file_write prints it)")

    home_rel = "~/" + str((WS / "report.pdf").relative_to(Path.home())) \
        if str(WS).startswith(str(Path.home())) else None
    if home_rel:
        files, problems = rf([home_rel], executor())
        check(len(files) == 1 and not problems, "~ is expanded")

    files, problems = rf([str(OUTSIDE / "secret.txt")], executor())
    check(not files and problems and "outside the workspace" in problems[0],
          f"absolute path outside the workspace refused and named: {problems}")
    files, problems = rf(["../outside/secret.txt"], executor())
    check(not files and problems and "outside" in problems[0], "..-escape refused")
    files, problems = rf(["escape"], executor())
    check(not files and problems and "outside" in problems[0],
          "a symlink pointing outside is refused (resolved before the check)")

    files, problems = rf([str(AGENT_DIR / "notes.md")], executor(folder=AGENT_DIR))
    check(len(files) == 1 and files[0][0] == "notes.md", "the agent's own folder is a second root")
    files, problems = rf([str(AGENT_DIR / "notes.md")], executor())
    check(not files and problems, "…but only for an agent that HAS a folder")

    files, problems = rf(["missing.txt", "report.pdf"], executor())
    check([f[0] for f in files] == ["report.pdf"] and "no such file" in problems[0],
          "a missing file is named and the others still go")
    files, problems = rf(["sub"], executor())
    check(not files and "no such file" in problems[0], "a directory is not a file")

    files, problems = rf([f"f{i}.txt" for i in range(6)], executor())
    check(len(files) == 5 and len(problems) == 1 and "at most 5" in problems[0],
          "cap of 5 files, the sixth named")

    big = WS / "big.bin"
    with open(big, "wb") as fh:
        fh.truncate(internal.MAX_NOTIFY_FILE_BYTES + 1)
    files, problems = rf(["big.bin"], executor())
    check(not files and "larger than 15 MB" in problems[0], "size cap enforced")
    big.unlink()


async def test_handler():
    print("Handler, connector with notify:")
    conn = FakeConnector(cannot_carry={"photo.jpg"})
    named = FakeNamed()
    out = await internal.notify_user_handler(
        text="prova due", subject="ciao", attachments=["report.pdf", "sub/photo.jpg"],
        binding_id="tg", chat_id="42", executor=executor(), _named=named, _state=state_for(conn))
    print("   ->", out)
    check(out.startswith("Message sent via binding 'tg' to: 42."), "reported as sent")
    check(len(conn.calls) == 1, "one notify per recipient")
    call = conn.calls[0]
    check(call["subject"] == "ciao" and call["text"] == "prova due",
          "subject and text reach the connector SEPARATELY")
    check([f[0] for f in call["files"]] == ["report.pdf", "photo.jpg"], "files reach the connector")
    check("Subject: ciao." in out and "Attached: report.pdf, photo.jpg." in out,
          "result names subject and attachments")
    check("NOT delivered" in out and "photo.jpg" in out.split("NOT delivered")[1],
          "the file the channel could not carry is reported")
    hist = named.sessions["fake:42"]
    said = hist["conversation"][-1]["content"]
    check(said.startswith("ciao\n\nprova due") and "[attached: report.pdf]" in said
          and "photo.jpg" not in said,
          "history: subject first line, then text, then only the files that arrived")
    check(hist["messages"][-1].get("notification") is True, "history entry flagged as notification")

    conn = FakeConnector()
    out = await internal.notify_user_handler(
        text="solo testo", binding_id="tg", chat_id="42", executor=executor(),
        _named=named, _state=state_for(conn))
    check(conn.calls[0]["subject"] == "" and conn.calls[0]["files"] == []
          and "Subject:" not in out and "Attached:" not in out,
          "no subject, no files: nothing extra in the call or the result")
    check(named.sessions["fake:42"]["conversation"][-1]["content"] == "solo testo",
          "history holds the bare text")

    conn = FakeConnector()
    out = await internal.notify_user_handler(
        text="x", subject="none", attachments="none", binding_id="tg", chat_id="42",
        executor=executor(), _named=named, _state=state_for(conn))
    check(conn.calls[0]["subject"] == "" and conn.calls[0]["files"] == [] and "ERROR" not in out,
          "placeholder words for subject/attachments mean 'none'")

    conn = FakeConnector()
    out = await internal.notify_user_handler(
        text="x", attachments=["missing.pdf"], binding_id="tg", chat_id="42",
        executor=executor(), _named=named, _state=state_for(conn))
    check(out.startswith("ERROR:") and "missing.pdf" in out and not conn.calls,
          "the only attachment missing: ERROR, nothing sent")

    conn = FakeConnector()
    out = await internal.notify_user_handler(
        text="x", attachments=str(OUTSIDE / "secret.txt"), binding_id="tg", chat_id="42",
        executor=executor(), _named=named, _state=state_for(conn))
    check(out.startswith("ERROR:") and "outside the workspace" in out and not conn.calls,
          "a path outside the workspace: ERROR, nothing sent")

    conn = FakeConnector()
    out = await internal.notify_user_handler(
        text="x", attachments="report.pdf, missing.pdf", binding_id="tg", chat_id="42",
        executor=executor(), _named=named, _state=state_for(conn))
    check(not out.startswith("ERROR") and [f[0] for f in conn.calls[0]["files"]] == ["report.pdf"]
          and "Skipped:" in out and "missing.pdf" in out,
          "one good, one bad: sent with the good one, the bad one named under Skipped")

    conn = FakeConnector()
    out = await internal.notify_user_handler(
        text="x", subject="s", binding_id="tg", chat_id="1, 2", executor=executor(),
        _named=named, _state=state_for(conn))
    check([c["chat_id"] for c in conn.calls] == ["1", "2"], "one notify per recipient, never a joined id")

    print("Handler, connector WITHOUT notify (older plugin):")
    old = OldConnector()
    out = await internal.notify_user_handler(
        text="prova due", subject="ciao", attachments=["report.pdf"],
        binding_id="tg", chat_id="42", executor=executor(), _named=named, _state=state_for(old))
    print("   ->", out)
    check(old.sent == [("42", "ciao\n\nprova due")], "subject folded into the text for send()")
    check(not old.files, "send_file is NOT called: the fallback does not guess the transport")
    check("NOT delivered" in out and "report.pdf" in out.split("NOT delivered")[1],
          "the file is reported as not delivered")
    check("[attached" not in named.sessions["old:42"]["conversation"][-1]["content"],
          "history does not claim a file that never left")


class _Files(http.server.BaseHTTPRequestHandler):
    """The web the URL attachments are fetched from, on 127.0.0.1."""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/caves/view/1/":
            body = b"%PDF-1.4 the sheet"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition",
                             'inline; filename="scheda_catastale_0001.pdf";')
        elif path == "/img/rilievo.png":
            body = b"\x89PNG fake"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
        elif path == "/noname/7/":
            body = b"%PDF-1.4 nameless"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
        elif path == "/page":
            body = b"<html><body>a page</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif path == "/big":
            body = b"x" * 4096
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
        elif path == "/to-secret":
            body = b""
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/secret")
        else:
            body = b"nope"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_url_attachments():
    print("Attachments given as a URL:")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Files)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    rf = internal._resolve_notify_files
    try:
        files, problems = rf([f"{base}/caves/view/1/?export_pdf=1&format=pdf"], executor())
        check(problems and "local or private" in problems[0] and not files,
              f"a private address is refused by default: {problems}")

        internal._ALLOW_PRIVATE_URLS = True
        files, problems = rf([f"{base}/caves/view/1/?export_pdf=1&format=pdf",
                              f"{base}/img/rilievo.png", f"{base}/noname/7/?x=1"], executor())
        check(not problems and [f[0] for f in files]
              == ["scheda_catastale_0001.pdf", "rilievo.png", "7.pdf"],
              f"named by Content-Disposition, by the path, by the type: {[f[0] for f in files]}")
        check(files[0][1] == b"%PDF-1.4 the sheet" and files[0][2] == "application/pdf"
              and files[1][2] == "image/png", "bytes and mime from the response")

        files, problems = rf([f"{base}/page"], executor())
        check(not files and "web page" in problems[0] and "link" in problems[0],
              "an HTML page is refused, with the advice to link it")
        files, problems = rf([f"{base}/missing.pdf"], executor())
        check(not files and "HTTP 404" in problems[0], "an HTTP error is named")
        files, problems = rf(["ftp://example.org/a.pdf"], executor())
        check(not files and problems and "no such file" in problems[0],
              "ftp:// is not a URL attachment (and not a workspace file either)")

        cap = internal.MAX_NOTIFY_FILE_BYTES
        internal.MAX_NOTIFY_FILE_BYTES = 1000
        try:
            files, problems = rf([f"{base}/big"], executor())
            check(not files and "larger than" in problems[0], "the size cap holds for downloads")
        finally:
            internal.MAX_NOTIFY_FILE_BYTES = cap

        # Redirect hops are checked again: allow the server, refuse /secret.
        internal._ALLOW_PRIVATE_URLS = False
        orig = internal._url_problem
        internal._url_problem = lambda u: "a private address" if "/secret" in u else ""
        try:
            files, problems = rf([f"{base}/to-secret"], executor())
            check(not files and "redirected to a refused address" in problems[0],
                  f"a redirect to a refused address is refused: {problems}")
        finally:
            internal._url_problem = orig

        internal._ALLOW_PRIVATE_URLS = True
        files, problems = rf(["report.pdf", f"{base}/img/rilievo.png"], executor())
        check([f[0] for f in files] == ["report.pdf", "rilievo.png"] and not problems,
              "workspace files and URLs mix in one call")

        # Provenance: only an address the turn was GIVEN is downloaded. The
        # real AgentExecutor.saw_text, on a stand-in holding seen_texts.
        from app.engine.executor import AgentExecutor
        given = f"{base}/caves/view/1/?export_pdf=1&format=pdf"
        turn = SimpleNamespace(
            agent=executor().agent,
            seen_texts=[f"code,LI1\npdf_url,{given}\n",
                        f'<a href="{base}/img/rilievo.png?a=1&amp;b=2">x</a>'])
        turn.saw_text = lambda needle: AgentExecutor.saw_text(turn, needle)
        built = f"{base}/caves/view/571/?export_pdf=1&format=pdf"
        files, problems = rf([given, built], turn)
        check([f[0] for f in files] == ["scheda_catastale_0001.pdf"]
              and len(problems) == 1 and built in problems[0]
              and "built by hand" in problems[0] and "pdf_url" in problems[0],
              "an address no tool returned is refused, the given one goes")
        check(turn.saw_text(f"{base}/img/rilievo.png?a=1&b=2"),
              "an address quoted in markup (&amp;) counts as given")
        check(not AgentExecutor.saw_text(SimpleNamespace(seen_texts=[]), given),
              "an empty turn record gives nothing")
    finally:
        internal._ALLOW_PRIVATE_URLS = False
        server.shutdown()


async def test_attachment_claim():
    print("A text that promises an attachment:")
    named = FakeNamed()

    async def send(text, attachments=None, subject=""):
        conn = FakeConnector()
        out = await internal.notify_user_handler(
            text=text, subject=subject, attachments=attachments, binding_id="tg",
            chat_id="42", executor=executor(), _named=named, _state=state_for(conn))
        return out, conn.calls

    out, calls = await send("Ciao,\n\nin allegato trovi il rilievo della grotta.")
    check(out.startswith("ERROR") and "in allegato" in out and "http(s)" in out and not calls,
          "'in allegato' with no file: ERROR naming the phrase, nothing sent")
    out, calls = await send("Please find attached the report.")
    check(out.startswith("ERROR") and not calls, "English phrasing caught too")
    out, calls = await send("Dettagli", subject="Ti allego la scheda")
    check(out.startswith("ERROR") and not calls, "a promise in the subject counts")
    out, calls = await send("In allegato la scheda.", attachments=["report.pdf"])
    check(not out.startswith("ERROR") and calls, "the same text WITH a file goes out")
    for text in ("Non ho potuto allegare il file: ecco il link.",
                 "Il rilievo non ha file allegati nel catasto.",
                 "Nessun allegato disponibile, trovi la scheda qui: https://x.it/1"):
        out, calls = await send(text)
        check(not out.startswith("ERROR") and calls, f"not a promise: {text[:40]!r}")


def test_schema():
    print("Tool schema:")
    import json
    d = json.loads((ROOT / "server/tools/autonomy/notify_user/tool.json").read_text())
    props = d["parameters"]["properties"]
    check("subject" in props and props["subject"]["type"] == "string", "subject declared")
    check(props.get("attachments", {}).get("type") == "array"
          and props["attachments"]["items"]["type"] == "string", "attachments declared as string array")
    check(d["parameters"].get("required") == ["text"], "only text is required")
    check("subject" in d["description"].lower(), "description tells the model where a subject goes")


try:
    test_split_paths()
    test_resolve()
    asyncio.run(test_handler())
    test_url_attachments()
    asyncio.run(test_attachment_claim())
    test_schema()
finally:
    shutil.rmtree(HOME, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
