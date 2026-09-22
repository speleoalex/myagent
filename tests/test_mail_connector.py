#!/usr/bin/env python3
"""The mail channel: what it reads, what it refuses, what it writes back.

Everything here runs against fakes — no mail server, no model, no real
binding. The state root is a temporary MYAGENT_CONNECTORS_DIR, set BEFORE the
plugin is imported (the invariant every connector test honours: the real
~/myagent/connectors holds live credentials and is never read or written).

Pinned properties, each of which fails silently in production:

  * the text handed to the agent is what the PERSON typed — subject plus
    body, minus quoted history and signature, HTML rendered to text;
  * machines never earn a reply (our own address, Auto-Submitted, bulk
    precedence, mailer-daemon), or two auto-responders loop forever;
  * a message is answered ONCE across restarts (persisted seen store, capped),
    and the first poll of a mailbox answers NOTHING (baseline);
  * the allowlist and the address book both match an email address, in any
    case — the same address goes out as user_id AND username;
  * the reply is ONE mail, threaded onto the inbound (In-Reply-To /
    References), flagged Auto-Submitted, with the disclosure, the answer and
    the delivered files inside it;
  * a notify_user with a subject is ONE new mail under that Subject, with the
    files attached and NOT threaded; without a subject it behaves as send()
    (a threaded reply, or buffered into the turn's single reply);
  * a body the agent wrote as HTML goes out as multipart/alternative with a
    rendered text twin, while prose stays a single text/plain part;
  * verify() maps a socket failure to Unreachable (the manager keeps
    retrying) and a refused login to a plain error (it stops).

Run:  server/.venv/bin/python tests/test_mail_connector.py
"""
import asyncio
import email
import os
import shutil
import sys
import tempfile
from email.policy import default as default_policy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(tempfile.mkdtemp(prefix="myagent-mailtest-"))
os.environ["MYAGENT_CONNECTORS_DIR"] = str(STATE)

sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "connectors" / "plugin"))

from myagent_connectors import config  # noqa: E402
from myagent_connectors.channels import registry  # noqa: E402
from myagent_connectors.channels.base import (  # noqa: E402
    DEFAULT_AI_DISCLOSURE, Unreachable)
from myagent_connectors.models import Binding, Contact  # noqa: E402
from myagent_connectors.services import Connectors, _looks_like_handle  # noqa: E402
from myagent_connectors.storage import GrantStore  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402

config.ensure_dirs()

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


# The channel is loaded the way the plugin loads it: through the registry,
# from its folder. If the manifest or the module breaks, this is the line
# that says so.
channels = {c["type"]: c for c in registry.all_channels()}
check("mail" in channels and channels["mail"]["loaded"],
      f"mail channel discovered and loaded ({channels.get('mail', {}).get('error') or 'no error'})")
check(bool(channels["mail"].get("settings")), "manifest exposes settings descriptors to the UI")
Mail = registry.get_channel("mail").connector
mail = sys.modules[Mail.__module__]


class FakeClient:
    """Stands in for CoreClient: records the turn, answers a fixed reply and
    optionally 'delivers' a file."""

    def __init__(self, resources=None):
        self.calls: list[dict] = []
        self.resources = resources or []

    async def chat(self, agent_id, message, session_id, attachments=None, source="",
                   sender_id="", sender_username="", sender_name="", transcribed=False):
        self.calls.append(dict(agent_id=agent_id, message=message, session_id=session_id,
                               attachments=attachments, source=source, sender_id=sender_id,
                               sender_username=sender_username, sender_name=sender_name))
        return f"Risposta a: {message.splitlines()[0]}", list(self.resources)


class FakeSMTP:
    """Captures what smtp_send would put on the wire."""
    sent: list = []

    @staticmethod
    def send(settings, password, msg):
        FakeSMTP.sent.append((settings, password, msg))


def binding(**kw) -> Binding:
    base = dict(
        id="mailbot", type="mail", agent_id="a", name="Assistente",
        token="app-password-secret", access_mode="allowlist",
        allowed_usernames=["mario@example.com"],
        settings={"username": "bot@myagent.test",
                  "host": "imap.myagent.test", "smtp_host": "smtp.myagent.test",
                  "poll_seconds": 15},
    )
    return Binding(**(base | kw))


def make(b: Binding | None = None, client=None):
    conn = Mail(b or binding(), client or FakeClient(), GrantStore(config.GRANTS_DIR))
    return conn


RAW_PLAIN = b"""From: Mario Rossi <Mario@Example.com>
To: bot@myagent.test
Subject: Domanda sulle grotte
Message-ID: <q1@example.com>
Date: Mon, 07 Sep 2026 10:00:00 +0200
Content-Type: text/plain; charset=utf-8

Quante grotte ci sono in Liguria?

Il lun 7 set 2026 alle ore 09:00 Assistente <bot@myagent.test> ha scritto:
> Messaggio precedente citato
> su due righe
--\x20
Mario
"""

RAW_HTML = b"""From: anna@example.com
Subject: Re: Report
Message-ID: <h1@example.com>
References: <r0@example.com>
Content-Type: text/html; charset=utf-8

<html><head><style>p{color:red}</style></head><body>
<p>Mandami il <b>report</b> di ieri, grazie &amp; ciao</p>
<div class="gmail_quote">On Mon, Sep 7, 2026 at 9:00 AM Assistente
&lt;bot@myagent.test&gt; wrote:<br><blockquote>vecchio</blockquote></div>
</body></html>
"""


def test_parsing():
    print("Parsing:")
    p = mail.parse_inbound(RAW_PLAIN, {"bot@myagent.test"})
    check(p.get("chat_id") == "mario@example.com", "sender address lowercased is the chat id")
    check(p.get("sender_name") == "Mario Rossi", "display name extracted")
    check(p.get("text") == "Domanda sulle grotte\n\nQuante grotte ci sono in Liguria?",
          f"subject + body, quote and signature stripped: {p.get('text')!r}")
    check(p.get("references") == ["<q1@example.com>"], "references end with the Message-ID")

    p = mail.parse_inbound(RAW_HTML, {"bot@myagent.test"})
    check(p.get("text") == "Mandami il report di ieri, grazie & ciao",
          f"HTML rendered to text, style dropped, English quote marker cut: {p.get('text')!r}")
    check(not p["text"].startswith("Report"), "a 'Re:' subject is NOT prepended")
    check(p["references"] == ["<r0@example.com>", "<h1@example.com>"], "References chain kept")

    # Outlook-style block and Italian wrapped marker
    t = mail.strip_quoted("Sì, va bene.\n\n-----Messaggio originale-----\nDa: x\nInviato: y\n\nvecchio")
    check(t == "Sì, va bene.", f"Outlook 'Messaggio originale' block cut: {t!r}")
    t = mail.strip_quoted("Ok\n\nIl giorno lun 7 set 2026 alle ore 09:00 Bot\n<bot@x.it> ha scritto:\n> a")
    check(t == "Ok", f"two-line Italian marker cut: {t!r}")
    check(mail.clean_subject("R: Fwd: RE: Ciao") == "Ciao", "reply prefixes stripped, any language")

    # Attachments: text goes in as text, image as data URL, .exe refused
    msg = email.message.EmailMessage(policy=default_policy)
    msg["From"] = "mario@example.com"
    msg["Subject"] = "File"
    msg.set_content("vedi allegati")
    msg.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="dati.csv")
    msg.add_attachment(b"\x89PNG....", maintype="image", subtype="png", filename="foto.png")
    msg.add_attachment(b"MZ....", maintype="application", subtype="x-msdownload", filename="virus.exe")
    p = mail.parse_inbound(msg.as_bytes(), set())
    kinds = [(a["kind"], a["name"]) for a in p["attachments"]]
    check(kinds == [("text", "dati.csv"), ("image", "foto.png")], f"attachments classified: {kinds}")
    check(p["attachments"][0]["data"] == "a,b\n1,2\n", "text attachment decoded")
    check(p["attachments"][1]["data"].startswith("data:image/png;base64,"), "image as data URL")
    check(any("virus.exe" in n for n in p["notes"]), "unsupported binary refused with a note")


def test_loop_guards():
    print("Loop guards:")
    own = {"bot@myagent.test"}
    cases = [
        (b"From: bot@myagent.test\nSubject: x\n\nhi\n", "sent by this mailbox"),
        (b"From: a@b.c\nAuto-Submitted: auto-replied\nSubject: x\n\nhi\n", "Auto-Submitted"),
        (b"From: a@b.c\nAuto-Submitted: no\nSubject: x\n\nhi\n", ""),
        (b"From: a@b.c\nPrecedence: bulk\nSubject: x\n\nhi\n", "Precedence"),
        (b"From: MAILER-DAEMON@mx.example\nSubject: Undelivered\n\nhi\n", "robot address"),
        (b"From: noreply@shop.example\nSubject: order\n\nhi\n", "robot address"),
        (b"From: a@b.c\nList-Id: <dev.lists.example>\nSubject: x\n\nhi\n", "mailing list"),
        (b"Subject: no sender\n\nhi\n", "no sender"),
    ]
    for raw, expect in cases:
        why = mail.parse_inbound(raw, own).get("skip", "")
        ok = (expect in why) if expect else (why == "")
        check(ok, f"{raw.splitlines()[0].decode()!r}: {why or 'accepted'}")
    reply = make()._build_reply("mario@example.com", "ciao", [])
    check(reply["Auto-Submitted"] == "auto-replied", "our replies carry Auto-Submitted: auto-replied")
    check(mail.is_automated(email.message_from_bytes(reply.as_bytes(), policy=default_policy),
                            set()) != "", "…so a peer with the same rule (us) drops them")


def test_seen_store():
    print("Seen store:")
    store = mail.SeenStore(mail.SEEN_DIR)
    ids, baselined = store.load("nope")
    check(ids == [] and baselined is False, "missing file = nothing seen, not baselined")
    store.save("b1", [f"uid:{i}" for i in range(mail.MAX_SEEN + 50)], True)
    ids, baselined = store.load("b1")
    check(len(ids) == mail.MAX_SEEN and ids[0] == "uid:50" and baselined,
          f"capped at {mail.MAX_SEEN}, oldest dropped, baseline flag kept")
    check(str(mail.SEEN_DIR).startswith(str(STATE)), "store lives under MYAGENT_CONNECTORS_DIR")
    store.clear("b1")
    check(store.load("b1") == ([], False), "clear() forgets the binding")


async def test_polling():
    print("Polling:")
    calls = {"list": 0, "fetch": 0}

    def fake_list(s, password):
        calls["list"] += 1
        return ["uid:1", "uid:2"], 2

    fetched: list = []

    def fake_fetch(s, password, known, limit=mail.MAX_PER_POLL):
        calls["fetch"] += 1
        fetched.append(set(known))
        return [(k, raw) for k, raw in (("uid:2", RAW_PLAIN), ("uid:3", RAW_PLAIN.replace(b"q1@", b"q3@")))
                if k not in known], 3

    mail.imap_list_unseen, orig_list = fake_list, mail.imap_list_unseen
    mail.imap_fetch, orig_fetch = fake_fetch, mail.imap_fetch
    mail.smtp_send, orig_send = FakeSMTP.send, mail.smtp_send
    try:
        FakeSMTP.sent.clear()
        conn = make()
        conn._seen, conn._baselined = conn._seen_store.load(conn.binding.id)
        conn._seen_set = set(conn._seen)
        await conn._poll_once()
        check(calls == {"list": 1, "fetch": 0}, "first poll only LISTS the backlog")
        check(conn._baselined and "uid:2" in conn._seen_set, "backlog recorded as seen, baseline set")
        check(not conn.client.calls, "…and nothing is answered")

        await conn._poll_once()
        await asyncio.gather(*conn._tasks)
        check(calls["fetch"] == 1 and fetched[0] >= {"uid:1", "uid:2"},
              "second poll fetches, passing the seen keys so uid:2 is skipped")
        check(len(conn.client.calls) == 1, "only the new mail (uid:3) reaches the agent")
        check(len(FakeSMTP.sent) == 1, "one reply mail sent")

        # Restart: a new connector on the same binding id must not re-answer
        conn2 = make()
        conn2._seen, conn2._baselined = conn2._seen_store.load(conn2.binding.id)
        conn2._seen_set = set(conn2._seen)
        check(conn2._baselined and "uid:3" in conn2._seen_set, "seen ids survive a restart")
        await conn2._poll_once()
        await asyncio.gather(*conn2._tasks)
        check(not conn2.client.calls, "after restart the same mails are not answered again")

        # Same Message-ID under a new UID (moved back to INBOX) is a duplicate
        conn2._remember(["dummy"])
        await conn2._handle_raw("uid:99", RAW_PLAIN.replace(b"q1@", b"q3@"))
        check(not conn2.client.calls, "duplicate Message-ID under a new uid is ignored")
    finally:
        mail.imap_list_unseen, mail.imap_fetch, mail.smtp_send = orig_list, orig_fetch, orig_send


async def test_pipeline_and_reply():
    print("Pipeline and reply:")
    mail.smtp_send, orig_send = FakeSMTP.send, mail.smtp_send
    try:
        FakeSMTP.sent.clear()
        client = FakeClient()
        # Its own binding id: the disclosure store is per binding, and the
        # polling test above already disclosed to this address on "mailbot".
        conn = make(binding(id="mailbot2"), client=client)
        conn._baselined = True
        await conn._handle_raw("uid:10", RAW_PLAIN)
        check(len(client.calls) == 1, "allowlisted address reaches the agent")
        call = client.calls[0]
        check(call["sender_id"] == "mario@example.com" == call["sender_username"],
              "address passed as sender_id AND sender_username")
        check(call["sender_name"] == "Mario Rossi", "display name forwarded")
        check(call["source"] == "mail", "source is the channel type")
        check(call["session_id"] == conn.session_id_for("mario@example.com"),
              "session keyed on the address")
        check(len(FakeSMTP.sent) == 1, "disclosure + answer = ONE outgoing mail")
        settings, password, msg = FakeSMTP.sent[0]
        check(password == "app-password-secret", "SMTP logs in with the binding token")
        body = msg.get_body(preferencelist=("plain",)).get_content()
        check(DEFAULT_AI_DISCLOSURE in body and "Risposta a: Domanda sulle grotte" in body,
              "the mail holds the disclosure and the answer")
        check(msg["To"] == "mario@example.com" and msg["From"] == "bot@myagent.test",
              "To = sender, From = mailbox login (from_addr empty)")
        check(msg["Subject"] == "Re: Domanda sulle grotte", "subject is Re: the inbound")
        check(msg["In-Reply-To"] == "<q1@example.com>" and "<q1@example.com>" in msg["References"],
              "threaded onto the inbound message")
        check(msg["Auto-Submitted"] == "auto-replied", "flagged as an automatic reply")

        # A second message from the same person: disclosure not repeated.
        FakeSMTP.sent.clear()
        await conn._handle_raw("uid:11", RAW_PLAIN.replace(b"q1@", b"q2@").replace(
            b"Subject: Domanda sulle grotte", b"Subject: Re: Domanda sulle grotte"))
        body = FakeSMTP.sent[0][2].get_body(preferencelist=("plain",)).get_content()
        check(DEFAULT_AI_DISCLOSURE not in body, "disclosure sent once per address")
        check(FakeSMTP.sent[0][2]["Subject"] == "Re: Domanda sulle grotte",
              "an inbound 'Re:' subject is not doubled")

        # Not in the allowlist: nothing goes out, the agent is never called.
        FakeSMTP.sent.clear()
        n = len(client.calls)
        await conn._handle_raw("uid:12", RAW_PLAIN.replace(b"Mario@Example.com", b"Nobody@Else.org"))
        check(len(client.calls) == n and not FakeSMTP.sent, "unknown address: no turn, no mail")

        # Files delivered by the turn become attachments of the same mail.
        FakeSMTP.sent.clear()
        ws = Path(tempfile.mkdtemp(prefix="ws-"))
        from app import config as app_config
        orig_ws = app_config.WORKSPACE_DIR
        app_config.WORKSPACE_DIR = ws
        try:
            (ws / "report.txt").write_text("il report")
            # Resource paths are workspace-relative, as the executor records them.
            client2 = FakeClient(resources=[{"path": "report.txt", "name": "report.txt",
                                             "title": "Il report"}])
            conn2 = make(binding(id="mailbot3"), client=client2)
            conn2._baselined = True
            await conn2._handle_raw("uid:20", RAW_PLAIN)
            check(len(FakeSMTP.sent) == 1, "answer + delivered file = ONE mail")
            msg = FakeSMTP.sent[0][2]
            atts = [a.get_filename() for a in msg.iter_attachments()]
            check(atts == ["report.txt"], f"file attached to the reply: {atts}")
        finally:
            app_config.WORKSPACE_DIR = orig_ws
            shutil.rmtree(ws, ignore_errors=True)

        # from_addr overrides the From:, and the token never leaks in a status line
        conn3 = make(binding(settings={"username": "bot@myagent.test",
                                       "host": "imap.myagent.test", "from_addr": "Assistente <ai@myagent.test>"}))
        r = conn3._build_reply("x@y.z", "ciao", [])
        check(r["From"] == "Assistente <ai@myagent.test>", "from_addr used as From:")
        check("app-password-secret" not in conn3._redact("bad app-password-secret here"),
              "token redacted from error strings")
    finally:
        mail.smtp_send = orig_send


async def test_verify():
    print("verify():")
    conn = make()
    import imaplib
    import smtplib

    # The real imap_list_unseen / smtp_check run; only the socket layer is
    # faked, so the exception MAPPING (the thing under test) is the real one.
    class RefusedIMAP:
        def __init__(self, host, port, timeout=None):
            raise ConnectionRefusedError(111, "Connection refused")

    class OkIMAP:
        def __init__(self, host, port, timeout=None):
            pass

        def login(self, u, p):
            if p != "app-password-secret":
                raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

        def select(self, folder):
            return "OK", [b"7"]

        def uid(self, cmd, *args):
            return "OK", [b"1 2 3"]

        def logout(self):
            pass

    class SMTPBase:
        def __init__(self, host, port, timeout=None):
            pass

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def noop(self):
            pass

        def quit(self):
            pass

    class RefusedSMTP(SMTPBase):
        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    class OkSMTP(SMTPBase):
        def login(self, u, p):
            pass

    orig = (mail.imaplib.IMAP4_SSL, mail.smtplib.SMTP)
    try:
        mail.imaplib.IMAP4_SSL = RefusedIMAP
        try:
            await conn.verify()
            check(False, "socket error raises")
        except Unreachable as e:
            check("imap.myagent.test:993" in str(e), f"socket error → Unreachable: {e}")
        except Exception as e:
            check(False, f"socket error → Unreachable, got {type(e).__name__}: {e}")

        mail.imaplib.IMAP4_SSL = OkIMAP
        bad = make(binding(token="wrong"))
        try:
            await bad.verify()
            check(False, "IMAP login failure raises")
        except Unreachable:
            check(False, "a refused IMAP login is NOT Unreachable (would retry forever)")
        except RuntimeError as e:
            check("login refused" in str(e) and "wrong" not in str(e),
                  f"refused IMAP login → plain error, token not in it: {e}")

        mail.smtplib.SMTP = RefusedSMTP
        try:
            await conn.verify()
            check(False, "SMTP login failure raises")
        except Unreachable:
            check(False, "a refused SMTP login is NOT Unreachable")
        except RuntimeError as e:
            check("535" in str(e) or "bad credentials" in str(e),
                  f"refused SMTP login → plain error, server message kept: {e}")

        mail.smtplib.SMTP = OkSMTP
        res = await conn.verify()
        check(res.get("account") == "bot@myagent.test" and "7 messages" in res.get("detail", ""),
              f"verify() reports the account and what it checked: {res}")

        # Per-section tests: each half is provable on its own, so a dead IMAP
        # server does not hide a working SMTP one (and vice versa).
        mail.imaplib.IMAP4_SSL = RefusedIMAP
        res = await conn.verify(check="send")
        check(res.get("check") == "send" and "messages" not in res.get("detail", "")
              and "ok" in res.get("detail", ""),
              f"check='send' skips IMAP and reports only SMTP: {res}")
        mail.imaplib.IMAP4_SSL, mail.smtplib.SMTP = OkIMAP, RefusedSMTP
        res = await conn.verify(check="receive")
        check(res.get("check") == "receive" and "7 messages" in res.get("detail", ""),
              f"check='receive' skips SMTP and reports the mailbox: {res}")
        try:
            await conn.verify(check="bogus")
            check(False, "unknown check id raises")
        except ValueError as e:
            check("bogus" in str(e) and "receive" in str(e), f"unknown check → ValueError naming the ids: {e}")
    finally:
        mail.imaplib.IMAP4_SSL, mail.smtplib.SMTP = orig

    # The manifest is what the UI renders and what verify(check=…) accepts:
    # the two must agree, and every label it names must exist in BOTH
    # dictionaries (I18n.t() would otherwise show the raw key).
    m = mail._MANIFEST
    sections = {s["id"] for s in m.get("sections") or []}
    fields = {f["key"] for f in m["settings"]}
    check(sections >= {"account", "incoming", "outgoing"}, f"manifest declares the mail sections: {sections}")
    check(all(f.get("section") in sections for f in m["settings"]),
          "every setting belongs to a declared section")
    check(all(k in fields for f in m["settings"] for k in (f.get("when") or {})),
          "every `when` clause names a real setting")
    check(any(f.get("required") for f in m["settings"]), "at least one setting is required")
    test_ids = tuple(t["id"] for s in m["sections"] for t in s.get("tests") or [])
    check(test_ids == mail._CHECKS and set(test_ids) == {"receive", "send"},
          f"sections[].tests ids are exactly what verify() accepts: {test_ids}")
    import re
    wanted = set()
    for f in m["settings"]:
        for k in ("label", "hint"):
            if str(f.get(k, "")).startswith("connectors."):
                wanted.add(f[k])
        for o in f.get("options") or []:
            if str(o.get("label", "")).startswith("connectors."):
                wanted.add(o["label"])
    for s in m["sections"]:
        wanted.add(s["label"])
        for t in s.get("tests") or []:
            wanted.add(t["label"])
    for k in ("labels", "hints", "handle"):
        for v in (m.get(k) or {}).values():
            if str(v).startswith("connectors."):
                wanted.add(v)
    for lang in ("en", "it"):
        keys = set(re.findall(r"^\s*'([^']+)':", (ROOT / "ui/js/i18n" / f"{lang}.js").read_text(), re.M))
        missing = sorted(wanted - keys)
        check(not missing, f"every manifest i18n key exists in {lang}.js" + (f" — missing {missing}" if missing else ""))

    # No provider preset any more: defaults come from the manifest alone, and
    # a stale `preset` key left by the old form is carried along, not applied.
    check(not any(f.get("fills") or f.get("key") == "preset" for f in mail._FIELDS),
          "manifest has no preset/fills descriptor")
    conn = make(binding(settings={"preset": "gmail", "username": "x@y.z", "host": "mail.y.z"}))
    s = conn.settings
    check((s["host"], s["port"], s["ssl"], s["smtp_host"], s["smtp_port"], s["smtp_security"]) ==
          ("mail.y.z", 993, True, "mail.y.z", 587, "starttls"), "manifest defaults, SMTP host = incoming host")
    conn = make(binding(settings={"username": "x@y.z", "host": "h",
                                  "poll_seconds": 3, "protocol": "POP3", "port": "995"}))
    check(conn.settings["poll_seconds"] == mail.MIN_POLL_SECONDS and conn.settings["protocol"] == "pop3"
          and conn.settings["port"] == 995, "poll floor applied, strings coerced")


def test_address_book():
    print("Address book and allowlist:")
    check(_looks_like_handle("mario@example.com"), "an email is a raw handle for notify_user")
    check(not _looks_like_handle("Mario Rossi"), "a name is not")
    check(_looks_like_handle("@mario"), "…an @username still is")
    conn = make(binding(allowed_usernames=["Mario@Example.COM"]))
    allowed, _ = conn._authorized("mario@example.com", "mario@example.com", "ciao")
    check(allowed, "allowlist matches the address case-insensitively (normalized on save)")
    allowed, _ = conn._authorized("other@example.com", "other@example.com", "ciao")
    check(not allowed, "a different address is denied")

    contacts = JsonStore(config.CONTACTS_DIR)
    contacts.save("mario", Contact(id="mario", name="Mario Rossi",
                                   handles={"mail": "Mario@Example.com"}).model_dump())
    svc = Connectors(bindings=JsonStore(config.BINDINGS_DIR), contacts=contacts,
                     grants=GrantStore(config.GRANTS_DIR), core=None, manager=None)
    who = svc.sender_display("mail", "mario@example.com", "mario@example.com", "M. Rossi")
    check(who.startswith("Mario Rossi via Email"), f"contact resolved by mail handle: {who!r}")
    who = svc.sender_display("mail", "anna@example.com", "anna@example.com", "Anna")
    check(who == "Anna via Email", f"unknown address falls back to the display name: {who!r}")


async def test_notify():
    """notify_user's hook: subject + files as ONE mail, and how it meets a
    turn in progress."""
    print("notify (subject + attachments):")
    mail.smtp_send, orig_send = FakeSMTP.send, mail.smtp_send
    try:
        FakeSMTP.sent.clear()
        conn = make(binding(id="mailbot3"))
        # A thread is known for this address (an earlier inbound mail)…
        conn._threads["mario@example.com"] = {
            "message_id": "<q1@example.com>", "subject": "Domanda sulle grotte",
            "references": ["<q1@example.com>"]}
        files = [("report.pdf", b"%PDF-1.4 fake", "application/pdf"),
                 ("foto.jpg", b"\xff\xd8", "image/jpeg")]
        ok, lost = await conn.notify("Mario@Example.com", "prova due", subject="ciao", files=files)
        check(ok is True and lost == [], "delivered, nothing lost")
        check(len(FakeSMTP.sent) == 1, "subject + text + 2 files = ONE mail (not N+1)")
        _, _, msg = FakeSMTP.sent[0]
        check(msg["Subject"] == "ciao", f"Subject header is the subject VERBATIM: {msg['Subject']!r}")
        check(msg["To"] == "mario@example.com", "address lowercased")
        check(msg["In-Reply-To"] is None and msg["References"] is None,
              "…with an explicit subject the mail is NOT threaded onto the old question")
        body = msg.get_body(preferencelist=("plain",)).get_content()
        check(body.strip() == "prova due" and "ciao" not in body, "body is the text alone, subject not repeated")
        atts = {a.get_filename(): a.get_content_type() for a in msg.iter_attachments()}
        check(atts == {"report.pdf": "application/pdf", "foto.jpg": "image/jpeg"},
              f"both files attached with their mime: {atts}")
        check(msg["Auto-Submitted"] == "auto-replied", "still flagged as machine-written")

        # No subject, outside a turn: a reply in the known thread, files attached.
        FakeSMTP.sent.clear()
        ok, lost = await conn.notify("mario@example.com", "ecco il file", files=files[:1])
        _, _, msg = FakeSMTP.sent[0]
        check(ok and msg["Subject"] == "Re: Domanda sulle grotte"
              and msg["In-Reply-To"] == "<q1@example.com>",
              "no subject = a threaded reply, as send() would do")
        check([a.get_filename() for a in msg.iter_attachments()] == ["report.pdf"], "file attached")

        # No subject DURING an inbound turn: joins the pending reply.
        FakeSMTP.sent.clear()
        conn._pending["mario@example.com"] = mail._Pending()
        ok, lost = await conn.notify("mario@example.com", "nel frattempo", files=files[:1])
        pend = conn._pending["mario@example.com"]
        check(ok and not FakeSMTP.sent and pend.texts == ["nel frattempo"]
              and [f[0] for f in pend.files] == ["report.pdf"],
              "buffered into the turn's single reply, nothing sent yet")
        # WITH a subject during a turn: its own mail, now.
        ok, lost = await conn.notify("mario@example.com", "a parte", subject="Altro")
        check(ok and len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0][2]["Subject"] == "Altro",
              "an explicit subject during a turn goes out on its own")
        conn._pending.pop("mario@example.com")

        # Failure: the files are reported lost, not swallowed.
        def boom(settings, password, msg):
            raise OSError("smtp down")
        mail.smtp_send = boom
        ok, lost = await conn.notify("mario@example.com", "x", subject="y", files=files)
        check(ok is False and lost == ["report.pdf", "foto.jpg"], "SMTP failure: not ok, files listed as lost")
        ok, lost = await conn.notify("not-an-address", "x", subject="y")
        check(ok is False, "an address without @ is refused")
    finally:
        mail.smtp_send = orig_send


async def test_base_notify():
    """The base class' default — what Telegram and the satellite inherit."""
    print("BaseConnector.notify default (chat-like transports):")
    from myagent_connectors.channels.base import BaseConnector

    class ChatLike(BaseConnector):
        def __init__(self, can_files=True):
            super().__init__(binding(id="chat"), FakeClient(), GrantStore(config.GRANTS_DIR))
            self.sent, self.files, self.can_files = [], [], can_files
            self.ok = True

        async def send(self, chat_id, text):
            self.sent.append((chat_id, text))
            return self.ok

        async def send_file(self, chat_id, name, data, mime, title):
            if not self.can_files:
                return await super().send_file(chat_id, name, data, mime, title)
            self.files.append((chat_id, name, mime, title))
            return True

    files = [("report.pdf", b"x", "application/pdf"), ("foto.jpg", b"y", "image/jpeg")]
    c = ChatLike()
    ok, lost = await c.notify("42", "prova due", subject="ciao", files=files)
    check(ok and lost == [] and c.sent == [("42", "ciao\n\nprova due")],
          "subject becomes the first line of ONE text message")
    check([f[1] for f in c.files] == ["report.pdf", "foto.jpg"] and c.files[0][3] == "report.pdf",
          "each file goes through send_file, titled by its name")
    c = ChatLike()
    await c.notify("42", "solo testo")
    check(c.sent == [("42", "solo testo")] and not c.files, "no subject, no files: a plain send")
    c = ChatLike(can_files=False)
    ok, lost = await c.notify("42", "x", files=files)
    check(ok and lost == ["report.pdf", "foto.jpg"],
          "a transport without send_file delivers the text and returns every file name")
    c = ChatLike(); c.ok = False
    ok, lost = await c.notify("42", "x", files=files)
    check(ok is False and lost == ["report.pdf", "foto.jpg"] and not c.files,
          "text refused: nothing uploaded, all files reported")


async def test_html_body():
    """An agent that writes its report in HTML must not deliver the tags.

    Sniffed, never declared: the bar is a structural tag opening the body AND
    a closing tag later, so prose that merely mentions a tag stays text/plain
    (rendering prose as HTML would eat its punctuation)."""
    print("HTML body detection:")
    cases = [
        ('<html><body style="font:Arial"><h2>Report</h2><p>ciao</p></body></html>', True),
        ('<h3>1. Stato server</h3>\n<table border="1"><tr><td>a</td></tr></table>', True),
        ('\n  <div style="x">ciao</div>', True),
        ('Ciao, ecco il report.\n- uno\n- due', False),
        ('Il tag <div> raggruppa, si chiude con </div>.', False),
        ("<b>Nota</b>: testo normale con un grassetto.", False),
        ('<p>aperto e mai chiuso', False),
        ('', False),
    ]
    for text, want in cases:
        got = mail.looks_like_html(text)
        check(got is want, f"{'markup' if want else 'prose  '}: {text[:44]!r}")

    mail.smtp_send, orig_send = FakeSMTP.send, mail.smtp_send
    try:
        FakeSMTP.sent.clear()
        conn = make(binding(id="mailbot4"))
        html = ('<html><body><h2>Report</h2><p>Server <b>OK</b>: 24/25.</p>'
                '</body></html>')
        ok, lost = await conn.notify("mario@example.com", html, subject="Report",
                                     files=[("d.csv", b"a,b\n1,2", "text/csv")])
        _, _, msg = FakeSMTP.sent[0]
        check(ok and msg.get_content_type() == "multipart/mixed",
              f"with an attachment the mail is multipart/mixed: {msg.get_content_type()}")
        html_part = msg.get_body(preferencelist=("html",))
        check(html_part is not None and html_part.get_content().strip() == html,
              "the HTML travels as text/html, verbatim")
        plain = msg.get_body(preferencelist=("plain",))
        check(plain is not None and "<h2>" not in plain.get_content()
              and "Report" in plain.get_content(),
              "…with a rendered text/plain twin for readers that refuse HTML")
        check([a.get_filename() for a in msg.iter_attachments()] == ["d.csv"],
              "the attachment survives the alternative")

        FakeSMTP.sent.clear()
        await conn.notify("mario@example.com", "Ciao, report di oggi.", subject="Report")
        _, _, msg = FakeSMTP.sent[0]
        check(msg.get_content_type() == "text/plain",
              f"prose is still ONE text/plain part: {msg.get_content_type()}")
    finally:
        mail.smtp_send = orig_send


async def main():
    await test_base_notify()
    test_parsing()
    test_loop_guards()
    test_seen_store()
    await test_polling()
    await test_pipeline_and_reply()
    await test_notify()
    await test_html_body()
    await test_verify()
    test_address_book()


try:
    asyncio.run(main())
finally:
    shutil.rmtree(STATE, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
