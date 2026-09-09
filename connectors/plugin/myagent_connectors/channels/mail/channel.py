"""Email connector — a mailbox polled over IMAP or POP3, replies over SMTP.

Standard library only (``imaplib``, ``poplib``, ``smtplib``, ``email``): mail is
the one transport every Python has built in, and the plugin promises that a
channel with no requirements.txt to install still works offline. The protocol
libraries are blocking, so every network call runs in ``asyncio.to_thread``
behind a timeout — the agent's own event loop must never wait on a mail server.

How a conversation maps onto the shared pipeline:

* **chat_id ≡ the sender's address**, lowercased. One person = one session,
  whatever the subject line says: an email thread is a much weaker notion of
  "conversation" than a chat (people start new threads for follow-ups, reply
  to old ones for new topics), and the agent's memory of the exchange is what
  the person actually wants to keep. The same address is handed over as
  ``user_id`` AND ``username`` so both allowlist columns match it, and so the
  address book (``Contact.handles["mail"]``) resolves it to a name.
* **A reply is a reply**: ``Re: <subject>`` with ``In-Reply-To``/``References``
  pointing at the mail being answered, so it lands in the person's thread.
  Everything the turn produced — the AI disclosure, the answer, the files it
  delivered — is buffered while the inbound mail is being handled and goes out
  as ONE email (see ``_Pending``): three separate mails for one question is the
  experience that makes people stop writing to a bot. An unsolicited
  ``notify_user`` WITH a subject is the one exception: it is a new mail under
  that subject, in a thread of its own (``notify`` / ``_build_reply``).
* **Loops are the failure mode of mail.** Two auto-responders facing each
  other, a vacation notice, a bounce quoting our own reply: each is a message
  from a machine that would earn a fresh agent turn and another machine
  answer. ``is_automated`` drops anything flagged ``Auto-Submitted``, bulk
  precedence, or coming from a daemon/no-reply address, and our own replies
  carry ``Auto-Submitted: auto-replied`` (RFC 3834) so a peer with the same
  rule drops US.
* **A message is answered once**, across restarts: a persisted store of
  Message-IDs (or POP3 UIDLs) per binding. IMAP also flags what it read as
  ``\\Seen``; POP3 has no flags, so the store is the only memory it has.
* **The first run never answers the backlog.** A mailbox handed to the agent
  usually has unread mail in it, and replying to all of it at once — to people
  who did not write to a bot — is the kind of mistake that cannot be undone.
  The first poll of a binding records what is pending and answers nothing;
  from then on (restarts included) everything new gets a reply.

Authentication is a plain login over TLS (IMAP LOGIN / POP3 USER+PASS /
SMTP AUTH) with the password in the token field — any generic IMAP/POP3
mailbox. Providers that refuse the account password for mail clients (Gmail,
Outlook.com) want an *app password* there instead; OAuth2 (XOAUTH2) is
deliberately not here: it needs a registered OAuth client and a refresh-token
dance that no form field can hold. There is no provider preset: the operator
types host and port, the form's per-section tests tell whether they work.
"""
from __future__ import annotations

import asyncio
import base64
import email
import email.utils
import imaplib
import json
import logging
import os
import poplib
import re
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from email.policy import default as default_policy
from html.parser import HTMLParser
from pathlib import Path

from app.storage.store import JsonStore

from myagent_connectors import config
from myagent_connectors.channels.base import (BaseConnector, Unreachable,
                                             redact)

log = logging.getLogger("connectors.mail")

# Every blocking protocol call gets this wall (seconds). A mail server that
# accepts the TCP connection and then says nothing would otherwise pin a worker
# thread forever, and the poll loop with it.
TIMEOUT = float(os.environ.get("MYAGENT_MAIL_TIMEOUT") or 30)
# Shortest poll interval a binding may ask for: mail servers throttle clients
# that hammer them (Gmail counts IMAP logins per hour), and nobody expects an
# email answered in under a quarter of a minute.
MIN_POLL_SECONDS = 15
# Messages handed to the agent per poll; the rest wait for the next one. Keeps a
# mailbox that suddenly fills (a forwarded thread, a mailing list) from turning
# one poll into a hundred concurrent agent turns.
MAX_PER_POLL = 10
MAX_ATTACHMENTS = 5
MAX_FILE = 15 * 1024 * 1024
# Message ids remembered per binding. Bounded because a JSON file is rewritten
# whole on every change; 5000 is months of a busy mailbox, and an id older than
# that cannot come back as "new" on IMAP (it is flagged) nor on POP3 (the UIDL
# list is the live mailbox, which is smaller than this).
MAX_SEEN = 5000

# Where the per-binding "already answered" state lives — under the plugin's
# state root, beside grants/ and disclosed/, never inside the plugin folder.
SEEN_DIR = config.STATE_DIR / "mail"

# The manifest is the single definition of the settings and their defaults
# (the UI reads the same file through /bindings/types): a second copy here
# would be free to drift from what the form shows.
_MANIFEST = json.loads((Path(__file__).with_name("channel.json")).read_text())
_FIELDS: list[dict] = _MANIFEST.get("settings") or []
_DEFAULTS: dict = {f["key"]: f.get("default") for f in _FIELDS if "key" in f}
# The per-section test buttons the form shows ("receive", "send"): verify()
# accepts exactly these ids, from the same manifest the UI reads.
_CHECKS: tuple[str, ...] = tuple(t["id"] for sec in _MANIFEST.get("sections") or []
                                 for t in sec.get("tests") or [])

# Same acceptance sets as the Telegram channel: a document we can hand the
# model as text, and audio we hand it as audio.
_TEXT_MIMES = {
    "application/json", "application/xml", "application/x-yaml",
    "application/yaml", "application/javascript", "application/x-sh",
    "application/x-python", "application/toml", "application/csv",
}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".ini", ".cfg", ".conf", ".toml", ".log", ".py", ".js", ".ts",
    ".html", ".htm", ".css", ".sh", ".sql", ".c", ".cpp", ".h", ".java",
    ".go", ".rs", ".rb", ".php", ".srt", ".vtt",
}
_AUDIO_EXTS = {
    ".oga", ".ogg", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac",
    ".wma", ".amr", ".weba",
}
# Binary documents worth materializing for document_extract. Anything else
# binary (an .exe, a .zip) is refused with a note rather than fed to the model.
_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".epub"}

# Local parts that are machines, not people. Matched on the address, since an
# auto-responder is under no obligation to set Auto-Submitted.
_ROBOT_LOCALPARTS = re.compile(
    r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce|bounces|"
    r"notification|notifications|noreply-)", re.IGNORECASE)

# Where quoted history starts. Each pattern anchors a line; the earliest match
# cuts the text. Multilingual on purpose: the marker is written by the SENDER's
# mail client in the sender's language, and an Italian user's Gmail says
# "Il ... ha scritto:". A wrapped "On ..., X wrote:" spans two lines, hence the
# tolerance for one embedded newline.
_QUOTE_MARKERS = [
    re.compile(r"^On\s[^\n]{0,200}(?:\n[^\n]{0,120})?\swrote:\s*$", re.MULTILINE),
    re.compile(r"^Il\s[^\n]{0,200}(?:\n[^\n]{0,120})?\sha\sscritto:\s*$", re.MULTILINE),
    re.compile(r"^Le\s[^\n]{0,200}(?:\n[^\n]{0,120})?\sa\sécrit\s?:\s*$", re.MULTILINE),
    re.compile(r"^Am\s[^\n]{0,200}(?:\n[^\n]{0,120})?\sschrieb[^\n]{0,80}:\s*$", re.MULTILINE),
    re.compile(r"^El\s[^\n]{0,200}(?:\n[^\n]{0,120})?\sescribió:\s*$", re.MULTILINE),
    re.compile(r"^-{3,}\s*(Original Message|Messaggio originale|Message d'origine|"
               r"Ursprüngliche Nachricht|Mensaje original)\s*-{3,}\s*$",
               re.MULTILINE | re.IGNORECASE),
    # Outlook-style header block at the top of the quote.
    re.compile(r"^(From|Da|De|Von):\s[^\n]+\n(Sent|Inviato|Envoyé|Gesendet|Enviado|"
               r"Date|Data|To|A|An|Para):\s", re.MULTILINE),
    re.compile(r"^_{8,}\s*$", re.MULTILINE),
    # Signature separator (RFC 3676): everything below is the signature.
    re.compile(r"^-- $", re.MULTILINE),
]
_RE_SUBJECT = re.compile(r"^\s*((re|r|aw|sv|antw|ref|fwd?|fw|wg|tr|i|vs)\s*:\s*)+",
                         re.IGNORECASE)


# ----------------------------------------------------------------- settings
def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _as_int(v, fallback: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback


def resolve_settings(raw: dict | None) -> dict:
    """The effective mail settings for a binding: manifest defaults, then what
    the operator typed (an EMPTY value keeps the default). Unknown keys — a
    `preset` left by a form that used to have one — ride along unused.
    """
    raw = dict(raw or {})
    s: dict = dict(_DEFAULTS)
    for k, v in raw.items():
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        s[k] = v.strip() if isinstance(v, str) else v
    s["protocol"] = str(s.get("protocol") or "imap").lower()
    s["port"] = _as_int(s.get("port"), 993 if _as_bool(s.get("ssl", True)) else 143)
    s["ssl"] = _as_bool(s.get("ssl", True))
    s["folder"] = str(s.get("folder") or "INBOX")
    s["poll_seconds"] = max(MIN_POLL_SECONDS, _as_int(s.get("poll_seconds"), 60))
    s["pop_delete"] = _as_bool(s.get("pop_delete", False))
    s["smtp_port"] = _as_int(s.get("smtp_port"), 587)
    s["smtp_security"] = str(s.get("smtp_security") or "starttls").lower()
    s["username"] = str(s.get("username") or "")
    s["host"] = str(s.get("host") or "")
    s["smtp_host"] = str(s.get("smtp_host") or "") or s["host"]
    s["from_addr"] = str(s.get("from_addr") or "") or s["username"]
    return s


# ----------------------------------------------------------- pure parsing
class _HtmlText(HTMLParser):
    """HTML → readable text: block tags become line breaks, script/style
    vanish, entities are decoded by the parser itself."""

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "table", "section", "article", "hr"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self._out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        raw = "".join(self._out)
        lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(markup: str) -> str:
    p = _HtmlText()
    try:
        p.feed(markup or "")
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup or "").strip()
    return p.text()


def strip_quoted(text: str) -> str:
    """Drop the quoted history and signature from a reply, keep what the
    person typed. Cuts at the earliest marker, then removes ``>`` lines that
    survive (a quote without a marker line above it)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cut = len(text)
    for pat in _QUOTE_MARKERS:
        m = pat.search(text)
        if m and m.start() < cut:
            cut = m.start()
    kept = text[:cut]
    lines = [ln for ln in kept.split("\n") if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_subject(subject: str) -> str:
    """The subject without its Re:/Fwd: prefixes, whitespace collapsed."""
    return _RE_SUBJECT.sub("", re.sub(r"\s+", " ", subject or "")).strip()


def is_reply_subject(subject: str) -> bool:
    return bool(_RE_SUBJECT.match(subject or ""))


def message_body(msg: email.message.Message) -> str:
    """The text a person wrote: text/plain when there is one, else the HTML
    rendered to text. Decoding errors are replaced, never raised — a mail with
    a wrong charset declaration is still a mail somebody sent."""
    def content(part) -> str:
        try:
            return part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")

    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        text = content(part)
        if text.strip():
            return text
    part = msg.get_body(preferencelist=("html",))
    if part is not None:
        return html_to_text(content(part))
    return ""


def is_automated(msg: email.message.Message, own_addresses: set[str]) -> str:
    """Why this message must not earn a reply, or "" when it may.

    Machines write to mailboxes constantly (bounces, receipts, vacation
    notices, our own replies coming back through a forward) and each one
    answered is another machine mail — possibly to a machine that answers
    back. Everything here is a positive signal from the sender's side; a
    person's message has none of them."""
    _, addr = email.utils.parseaddr(str(msg.get("From") or ""))
    addr = addr.strip().lower()
    if not addr or "@" not in addr:
        return "no sender address"
    if addr in own_addresses:
        return "sent by this mailbox"
    if _ROBOT_LOCALPARTS.match(addr.split("@", 1)[0]):
        return f"robot address {addr}"
    auto = str(msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    if str(msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return f"Precedence: {msg.get('Precedence')}"
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return "mailing list"
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return "auto-reply header"
    ctype = msg.get_content_type()
    if ctype in ("multipart/report",):
        return "delivery report"
    return ""


def _text_attachment(content: bytes, name: str) -> dict:
    return {"name": name, "kind": "text",
            "data": content.decode("utf-8", errors="replace")}


def _b64_attachment(kind: str, content: bytes, mime: str, name: str) -> dict:
    mime = mime or "application/octet-stream"
    b64 = base64.b64encode(content).decode()
    return {"name": name, "kind": kind, "mime": mime,
            "data": f"data:{mime};base64,{b64}"}


def extract_attachments(msg: email.message.Message) -> tuple[list[dict], list[str]]:
    """Attachment dicts for the agent, plus notes about what was refused.

    Inline images referenced by the HTML (Content-ID) are skipped: they are
    signature logos and tracking pixels far more often than content, and the
    person did not "attach" them."""
    out: list[dict] = []
    notes: list[str] = []
    for part in msg.iter_attachments():
        if len(out) >= MAX_ATTACHMENTS:
            notes.append(f"only the first {MAX_ATTACHMENTS} attachments were read")
            break
        if part.get("Content-ID") and part.get_content_disposition() != "attachment":
            continue
        name = part.get_filename() or "attachment"
        mime = (part.get_content_type() or "").lower()
        ext = os.path.splitext(name)[1].lower()
        try:
            content = part.get_payload(decode=True) or b""
        except Exception:
            content = b""
        if not content:
            continue
        if len(content) > MAX_FILE:
            notes.append(f"{name} is too large ({len(content) // (1024 * 1024)} MB)")
            continue
        is_text = mime.startswith("text/") or mime in _TEXT_MIMES or ext in _TEXT_EXTS
        is_audio = mime.startswith("audio/") or ext in _AUDIO_EXTS
        if mime.startswith("image/"):
            out.append(_b64_attachment("image", content, mime, name))
        elif is_text:
            out.append(_text_attachment(content, name))
        elif is_audio:
            out.append(_b64_attachment("audio", content, mime or "audio/mpeg", name))
        elif ext in _DOC_EXTS or mime == "application/pdf":
            out.append(_b64_attachment("file", content, mime or "application/pdf", name))
        else:
            notes.append(f"{name}: unsupported type {mime or ext or '?'}")
    return out, notes


def parse_inbound(raw: bytes, own_addresses: set[str]) -> dict:
    """Everything the pipeline needs from one raw message, or ``{"skip": why}``.

    Returns: ``chat_id`` (sender address, lowercased), ``sender_name``,
    ``text`` (subject + body, quotes stripped), ``attachments``, ``notes``,
    ``message_id``, ``subject``, ``references``."""
    msg = email.message_from_bytes(raw, policy=default_policy)
    why = is_automated(msg, own_addresses)
    if why:
        return {"skip": why}
    name, addr = email.utils.parseaddr(str(msg.get("From") or ""))
    addr = addr.strip().lower()
    subject = re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip()
    body = strip_quoted(message_body(msg))
    attachments, notes = extract_attachments(msg)
    # The subject is part of what the person said — unless it is a "Re:" of
    # an earlier exchange, where it only repeats the thread's title.
    topic = clean_subject(subject)
    if topic and not is_reply_subject(subject) and topic.lower() not in body.lower():
        text = f"{topic}\n\n{body}".strip()
    else:
        text = body
    refs = str(msg.get("References") or "").split()
    mid = str(msg.get("Message-ID") or "").strip()
    if mid and mid not in refs:
        refs.append(mid)
    return {
        "chat_id": addr,
        "sender_name": (name or "").strip(),
        "text": text,
        "attachments": attachments,
        "notes": notes,
        "message_id": mid,
        "subject": subject,
        "references": refs[-20:],
    }


# ------------------------------------------------------------- seen store
class SeenStore:
    """Per-binding memory of which messages were already handled.

    ``baselined`` records that the first poll ran and swallowed the backlog:
    it is what makes "answer everything new" safe to apply on every later
    start. Reads degrade to "nothing seen, not baselined", which is the safe
    direction — a lost file means one silent pass, never a duplicate reply."""

    def __init__(self, base_dir: Path):
        base_dir.mkdir(parents=True, exist_ok=True)
        self._store = JsonStore(base_dir)

    def load(self, binding_id: str) -> tuple[list[str], bool]:
        data = self._store.get(binding_id) or {}
        ids = data.get("message_ids") or []
        if not isinstance(ids, list):
            ids = []
        return [str(x) for x in ids if str(x).strip()], bool(data.get("baselined"))

    def save(self, binding_id: str, ids: list[str], baselined: bool) -> None:
        self._store.save(binding_id, {"id": binding_id, "baselined": baselined,
                                      "message_ids": ids[-MAX_SEEN:]})

    def clear(self, binding_id: str) -> None:
        self._store.delete(binding_id)


# ------------------------------------------------------- blocking transport
def _unreachable(host: str, port: int, e: BaseException) -> Unreachable:
    return Unreachable(f"cannot reach {host}:{port}: {e or type(e).__name__}")


def imap_fetch(s: dict, password: str, known: set[str],
               limit: int = MAX_PER_POLL) -> tuple[list[tuple[str, bytes]], int]:
    """New messages from the IMAP folder: ``[(key, raw)]`` plus the folder's
    total count (for the status line). Fetched with BODY.PEEK so a message we
    fail to hand over is not flagged; the \\Seen flag is set explicitly once the
    bytes are in hand. ``known`` keys are skipped without being fetched."""
    host, port = s["host"], s["port"]
    if not host:
        raise RuntimeError("no IMAP host configured")
    try:
        if s["ssl"]:
            box = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
        else:
            box = imaplib.IMAP4(host, port, timeout=TIMEOUT)
            if "STARTTLS" in box.capabilities:
                box.starttls()
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"IMAP {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)
    try:
        try:
            box.login(s["username"], password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"IMAP login refused for {s['username']}: "
                               f"{str(e).strip() or 'authentication failed'}")
        typ, data = box.select(_imap_quote(s["folder"]))
        if typ != "OK":
            raise RuntimeError(f"IMAP folder {s['folder']!r} not found")
        total = int((data[0] or b"0").decode(errors="replace") or 0)
        typ, data = box.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError("IMAP SEARCH failed")
        uids = [u.decode() for u in (data[0] or b"").split()]
        out: list[tuple[str, bytes]] = []
        for uid in uids:
            key = f"uid:{uid}"
            if key in known:
                continue
            if len(out) >= limit:
                break
            typ, parts = box.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            raw = parts[0][1]
            box.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            out.append((key, raw))
        return out, total
    except OSError as e:
        raise _unreachable(host, port, e)
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _imap_quote(folder: str) -> str:
    """A folder name with spaces or brackets ("[Gmail]/All Mail") must be
    quoted on the wire; imaplib does not do it for select()."""
    if re.fullmatch(r"[A-Za-z0-9._/-]+", folder or ""):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pop3_fetch(s: dict, password: str, known: set[str],
               limit: int = MAX_PER_POLL) -> tuple[list[tuple[str, bytes]], int]:
    """New messages over POP3, keyed by UIDL: ``[(key, raw)]`` and the
    mailbox count. POP3 has no flags, so ``known`` (the persisted store) is the
    only thing that stops a message from being answered on every poll — unless
    ``pop_delete`` is on, in which case fetched mail is removed from the
    server (the classic POP behaviour; the store still guards a crash between
    RETR and QUIT)."""
    host, port = s["host"], s["port"]
    if not host:
        raise RuntimeError("no POP3 host configured")
    try:
        if s["ssl"]:
            box = poplib.POP3_SSL(host, port, timeout=TIMEOUT)
        else:
            box = poplib.POP3(host, port, timeout=TIMEOUT)
            try:
                box.stls()
            except poplib.error_proto:
                pass  # server without STLS: stay in the clear, as configured
    except poplib.error_proto as e:
        raise RuntimeError(f"POP3 {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)
    try:
        try:
            box.user(s["username"])
            box.pass_(password)
        except poplib.error_proto as e:
            raise RuntimeError(f"POP3 login refused for {s['username']}: "
                               f"{str(e).strip() or 'authentication failed'}")
        total = box.stat()[0]
        _, listing, _ = box.uidl()
        out: list[tuple[str, bytes]] = []
        for entry in listing:
            num, _, uidl = entry.decode(errors="replace").partition(" ")
            key = f"uidl:{uidl.strip()}"
            if key in known:
                continue
            if len(out) >= limit:
                break
            _, lines, _ = box.retr(int(num))
            out.append((key, b"\r\n".join(lines)))
            if s["pop_delete"]:
                box.dele(int(num))
        box.quit()  # commits the deletions
        return out, total
    except poplib.error_proto as e:
        raise RuntimeError(f"POP3 {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)


def pop3_list(s: dict, password: str) -> tuple[list[str], int]:
    """Every UIDL currently in the mailbox, for the baseline pass."""
    host, port = s["host"], s["port"]
    try:
        box = (poplib.POP3_SSL(host, port, timeout=TIMEOUT) if s["ssl"]
               else poplib.POP3(host, port, timeout=TIMEOUT))
        try:
            box.user(s["username"])
            box.pass_(password)
        except poplib.error_proto as e:
            raise RuntimeError(f"POP3 login refused for {s['username']}: {e}")
        total = box.stat()[0]
        _, listing, _ = box.uidl()
        box.quit()
    except poplib.error_proto as e:
        raise RuntimeError(f"POP3 {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)
    keys = []
    for entry in listing:
        _, _, uidl = entry.decode(errors="replace").partition(" ")
        keys.append(f"uidl:{uidl.strip()}")
    return keys, total


def imap_list_unseen(s: dict, password: str) -> tuple[list[str], int]:
    """Every UNSEEN uid in the folder, for the baseline pass (nothing fetched,
    nothing flagged: the mail stays unread for the human who owns the box)."""
    host, port = s["host"], s["port"]
    try:
        if s["ssl"]:
            box = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
        else:
            box = imaplib.IMAP4(host, port, timeout=TIMEOUT)
            if "STARTTLS" in box.capabilities:
                box.starttls()
        try:
            box.login(s["username"], password)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"IMAP login refused for {s['username']}: {e}")
        typ, data = box.select(_imap_quote(s["folder"]))
        if typ != "OK":
            raise RuntimeError(f"IMAP folder {s['folder']!r} not found")
        total = int((data[0] or b"0").decode(errors="replace") or 0)
        typ, data = box.uid("SEARCH", None, "UNSEEN")
        uids = [f"uid:{u.decode()}" for u in (data[0] or b"").split()]
        box.logout()
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"IMAP {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)
    return uids, total


def smtp_send(s: dict, password: str, msg: EmailMessage) -> None:
    host, port = s["smtp_host"], s["smtp_port"]
    if not host:
        raise RuntimeError("no SMTP host configured")
    try:
        if s["smtp_security"] == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
            smtp.ehlo()
            if s["smtp_security"] == "starttls":
                smtp.starttls()
                smtp.ehlo()
        try:
            if password and s["username"]:
                smtp.login(s["username"], password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(f"SMTP login refused for {s['username']}: "
                           f"{e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e.smtp_error}")
    except smtplib.SMTPException as e:
        raise RuntimeError(f"SMTP {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)


def smtp_check(s: dict, password: str) -> None:
    """Connect and log in without sending: the test button's half of SMTP."""
    host, port = s["smtp_host"], s["smtp_port"]
    if not host:
        raise RuntimeError("no SMTP host configured")
    try:
        if s["smtp_security"] == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
            smtp.ehlo()
            if s["smtp_security"] == "starttls":
                smtp.starttls()
                smtp.ehlo()
        try:
            if password and s["username"]:
                smtp.login(s["username"], password)
            smtp.noop()
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(f"SMTP login refused for {s['username']}: "
                           f"{e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else e.smtp_error}")
    except smtplib.SMTPException as e:
        raise RuntimeError(f"SMTP {host}:{port}: {e}")
    except OSError as e:
        raise _unreachable(host, port, e)


# ---------------------------------------------------------------- connector
class _Pending:
    """What one inbound mail has earned so far: texts and files, flushed as a
    single reply when the turn is over."""

    def __init__(self):
        self.texts: list[str] = []
        self.files: list[tuple[str, bytes, str]] = []


class MailConnector(BaseConnector):
    type = "mail"

    def __init__(self, binding, client, grants):
        super().__init__(binding, client, grants)
        self.settings = resolve_settings(getattr(binding, "settings", None) or {})
        self._stop = asyncio.Event()
        self._seen_store = SeenStore(SEEN_DIR)
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        self._baselined = False
        # Last inbound mail per chat: what a reply threads onto.
        self._threads: dict[str, dict] = {}
        # Chats with an inbound mail being handled right now — sends to them
        # are buffered into one reply (see module docstring).
        self._pending: dict[str, _Pending] = {}
        self._tasks: set[asyncio.Task] = set()
        self._account = self.settings["username"]

    # ------------------------------------------------------------ helpers
    @property
    def _own(self) -> set[str]:
        return {a.lower() for a in (self.settings["username"], self.settings["from_addr"]) if a}

    def _receive_label(self) -> str:
        s = self.settings
        return f"{s['protocol'].upper()} {s['host']}:{s['port']}"

    def _smtp_label(self) -> str:
        s = self.settings
        return f"SMTP {s['smtp_host']}:{s['smtp_port']}"

    def _redact(self, text: str) -> str:
        return redact(str(text), self.binding.token)

    # --------------------------------------------------------------- verify
    async def verify(self, check: str | None = None) -> dict:
        """Log in on the side asked for — ``receive`` (the mailbox we read),
        ``send`` (the SMTP we answer through) — or on BOTH when no ``check`` is
        given, because a binding that can read but not reply looks, from the
        outside, exactly like one that ignores people. The two halves are
        separate buttons in the form: an IMAP typo and an SMTP typo are found
        one at a time, next to the fields that caused them."""
        s = self.settings
        if not s["username"]:
            raise RuntimeError("no username (mailbox login) configured")
        if check and check not in _CHECKS:
            raise ValueError(f"unknown check {check!r} (expected one of: {', '.join(_CHECKS)})")
        parts = []
        if not check or check == "receive":
            lister = pop3_list if s["protocol"] == "pop3" else imap_list_unseen
            _, total = await asyncio.to_thread(lister, s, self.binding.token)
            parts.append(f"{self._receive_label()}, {total} messages")
        if not check or check == "send":
            await asyncio.to_thread(smtp_check, s, self.binding.token)
            parts.append(f"{self._smtp_label()} ok")
        return {"account": s["username"],
                "name": s["username"],
                "check": check or "",
                "detail": "; ".join(parts)}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.status.state = "starting"
        if not self.settings["username"] or not self.settings["host"]:
            raise RuntimeError("mail settings incomplete: username and host are required")
        self._seen, self._baselined = self._seen_store.load(self.binding.id)
        self._seen_set = set(self._seen)
        # Boot contract shared with Telegram: no network yet is a wait, a
        # refused login is a failure.
        backoff = 1
        last_logged = ""
        while True:
            try:
                await self._poll_once()
                break
            except Unreachable as e:
                if self._stop.is_set():
                    return
                self.status.state = "error"
                self.status.detail = self._redact(str(e))
                if self.status.detail != last_logged:
                    last_logged = self.status.detail
                    log.warning("start blocked (%s): %s — retrying, backoff %ss",
                                self.binding.id, self.status.detail, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self.status.state = "error"
                self.status.detail = self._redact(str(e))
                raise
        self.status.state = "running"
        self.status.detail = f"{self._account} ({self._receive_label()})"
        log.info("Mail connector '%s' running as %s", self.binding.id, self.status.detail)
        await self._poll_loop()

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self._tasks):
            t.cancel()
        self.status.state = "stopped"

    async def _sleep(self, seconds: float) -> None:
        """Wait, but wake at once on stop()."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _poll_loop(self) -> None:
        backoff = 1
        last_logged = ""
        await self._sleep(self.settings["poll_seconds"])
        while not self._stop.is_set():
            try:
                await self._poll_once()
                backoff = 1
                self.status.errors = 0
                last_logged = ""
                if self.status.state == "error":
                    self.status.state = "running"
                    self.status.detail = f"{self._account} ({self._receive_label()})"
                await self._sleep(self.settings["poll_seconds"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop.is_set():
                    break
                self.status.errors += 1
                self.status.state = "error"
                self.status.detail = self._redact(str(e))
                if self.status.errors >= config.MAX_CONSECUTIVE_ERRORS:
                    self.status.state = "paused"
                    log.warning(
                        "connector '%s' paused after %d consecutive errors: %s "
                        "(POST /api/connectors/bindings/%s/resume to retry)",
                        self.binding.id, self.status.errors, self.status.detail,
                        self.binding.id,
                    )
                    return
                if self.status.detail != last_logged:
                    last_logged = self.status.detail
                    log.warning("poll error (%s): %s — retrying, backoff %ss",
                                self.binding.id, self.status.detail, backoff)
                await self._sleep(max(backoff, self.settings["poll_seconds"]))
                backoff = min(backoff * 2, 60)

    # -------------------------------------------------------------- polling
    async def _poll_once(self) -> None:
        s = self.settings
        if not self._baselined:
            # First contact with this mailbox: remember the backlog, answer
            # nothing (module docstring). Nothing is fetched or flagged.
            if s["protocol"] == "pop3":
                keys, total = await asyncio.to_thread(pop3_list, s, self.binding.token)
            else:
                keys, total = await asyncio.to_thread(imap_list_unseen, s, self.binding.token)
            self._remember(keys, baselined=True)
            if keys:
                log.info("[%s] first run: %d pending message(s) left unanswered",
                         self.binding.id, len(keys))
            return
        if s["protocol"] == "pop3":
            batch, _ = await asyncio.to_thread(pop3_fetch, s, self.binding.token,
                                               self._seen_set)
        else:
            batch, _ = await asyncio.to_thread(imap_fetch, s, self.binding.token,
                                               self._seen_set)
        for key, raw in batch:
            self._remember([key])
            self._spawn(self._handle_raw(key, raw))

    def _remember(self, keys: list[str], baselined: bool | None = None) -> None:
        new = [k for k in keys if k and k not in self._seen_set]
        if baselined is not None:
            self._baselined = baselined
        elif not new:
            return
        self._seen.extend(new)
        self._seen_set.update(new)
        if len(self._seen) > MAX_SEEN:
            self._seen = self._seen[-MAX_SEEN:]
            self._seen_set = set(self._seen)
        try:
            self._seen_store.save(self.binding.id, self._seen, self._baselined)
        except Exception as e:
            log.warning("[%s] cannot persist seen ids: %s", self.binding.id, e)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_raw(self, key: str, raw: bytes) -> None:
        try:
            parsed = parse_inbound(raw, self._own)
        except Exception as e:
            log.warning("[%s] unparseable message %s: %s", self.binding.id, key, e)
            return
        if parsed.get("skip"):
            log.info("[%s] skipped %s: %s", self.binding.id, key, parsed["skip"])
            return
        # A Message-ID is a second dedup key: the same mail can show up under a
        # new UID (moved back to the folder, re-delivered by a forward rule).
        mid = parsed.get("message_id")
        if mid:
            mkey = f"mid:{mid}"
            if mkey in self._seen_set:
                log.info("[%s] duplicate Message-ID %s ignored", self.binding.id, mid)
                return
            self._remember([mkey])
        chat_id = parsed["chat_id"]
        self._threads[chat_id] = {"message_id": mid, "subject": parsed["subject"],
                                  "references": parsed["references"]}
        text = parsed["text"]
        if parsed["notes"]:
            text = (text + "\n\n" if text else "") + "[" + "; ".join(parsed["notes"]) + "]"
        self.status.last_update = datetime.now().isoformat(timespec="seconds")
        self._pending[chat_id] = _Pending()
        try:
            await self.process_message(
                chat_id, chat_id, text, username=chat_id,
                attachments=parsed["attachments"] or None,
                sender_name=parsed["sender_name"])
        finally:
            pending = self._pending.pop(chat_id, None)
        if pending and (pending.texts or pending.files):
            await self._compose_and_send(chat_id, "\n\n".join(pending.texts), pending.files)

    # ------------------------------------------------------------ transport
    async def send(self, chat_id, text: str) -> bool:
        chat_id = str(chat_id).strip().lower()
        pending = self._pending.get(chat_id)
        if pending is not None:
            pending.texts.append(text or "")
            return True
        return await self._compose_and_send(chat_id, text or "", [])

    async def send_file(self, chat_id, name: str, data: bytes, mime: str,
                        title: str) -> bool:
        chat_id = str(chat_id).strip().lower()
        pending = self._pending.get(chat_id)
        if pending is not None:
            pending.files.append((name, data, mime))
            return True
        return await self._compose_and_send(chat_id, title or "", [(name, data, mime)])

    async def notify(self, chat_id, text: str, subject: str = "",
                     files: list[tuple[str, bytes, str]] | None = None,
                     ) -> tuple[bool, list[str]]:
        """One mail: the subject as the Subject header, the files as
        attachments — never the base class' "subject as first line, one send
        per file", which on this transport would be N+1 emails for one
        notification.

        With no subject the notification joins whatever is pending for the
        chat, exactly like ``send``/``send_file``: an agent that answers an
        inbound mail by calling ``notify_user`` still produces ONE reply. An
        explicit subject is the agent saying "a new mail, about this": it goes
        out on its own, now, under that subject and in a thread of its own
        (see ``_build_reply``)."""
        chat_id = str(chat_id).strip().lower()
        files = list(files or [])
        subject = (subject or "").strip()
        pending = self._pending.get(chat_id)
        if pending is not None and not subject:
            pending.texts.append(text or "")
            pending.files.extend(files)
            return True, []
        ok = await self._compose_and_send(chat_id, text or "", files, subject=subject)
        return ok, ([] if ok else [name for name, _, _ in files])

    def _build_reply(self, chat_id: str, text: str,
                     files: list[tuple[str, bytes, str]],
                     subject: str = "") -> EmailMessage:
        """The outgoing message. Without ``subject`` it is a REPLY: ``Re:`` the
        last inbound subject and threaded onto that mail. With one, it is a
        NEW mail under exactly that subject and deliberately NOT threaded — an
        ``In-Reply-To`` pointing at last week's question would file "Report
        di oggi" under "Domanda sulle grotte" in the reader's client, which is
        the opposite of what a subject is for."""
        s = self.settings
        thread = {} if subject else (self._threads.get(chat_id) or {})
        msg = EmailMessage()
        msg["From"] = s["from_addr"]
        msg["To"] = chat_id
        if subject:
            msg["Subject"] = subject
        elif thread.get("subject"):
            inbound = thread["subject"]
            msg["Subject"] = inbound if is_reply_subject(inbound) else f"Re: {inbound}"
        else:
            msg["Subject"] = self.binding.name or "MyAgent"
        if thread.get("message_id"):
            msg["In-Reply-To"] = thread["message_id"]
            msg["References"] = " ".join(thread.get("references") or [thread["message_id"]])
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=_domain_of(s["from_addr"]))
        # RFC 3834: says "a machine wrote this" to any peer that checks, which
        # is how two bots facing each other stop after one round.
        msg["Auto-Submitted"] = "auto-replied"
        msg["X-Auto-Response-Suppress"] = "All"
        msg.set_content(text or "(no reply)")
        for name, data, mime in files:
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            if not subtype:
                maintype, subtype = "application", "octet-stream"
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        return msg

    async def _compose_and_send(self, chat_id: str, text: str,
                                files: list[tuple[str, bytes, str]],
                                subject: str = "") -> bool:
        if not chat_id or "@" not in chat_id:
            log.warning("[%s] refusing to mail an address without '@': %r",
                        self.binding.id, chat_id)
            return False
        msg = self._build_reply(chat_id, text, files, subject=subject)
        try:
            await asyncio.to_thread(smtp_send, self.settings, self.binding.token, msg)
            return True
        except Exception as e:
            log.warning("[%s] send to %s failed: %s", self.binding.id, chat_id,
                        self._redact(str(e)))
            return False


def _domain_of(addr: str) -> str | None:
    _, _, domain = (addr or "").partition("@")
    return domain or None
