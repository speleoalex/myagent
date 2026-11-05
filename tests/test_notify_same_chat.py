#!/usr/bin/env python3
"""notify_user to the person a channel turn is answering.

Born from a mail bot, 2026-11-05: asked a question by mail, the agent answered
by calling notify_user to the sender (the mail went out), then hung. The turn
holds the session's lock (run_channel_turn) and notify_user appends what it
sent to the recipient's session under that same lock — an asyncio.Lock, not
re-entrant — so the turn waited on itself until the 180 s connector timeout.
The person got the answer, then an error mail, and the turn was never saved,
which left the next question without its context.

Pinned here, with the REAL run_channel_turn and NamedSessionStore and a fake
executor and connector (no model, no transport, temporary MYAGENT_HOME):

  * the turn completes (a deadlock shows up as the wait_for timeout below);
  * the notification still lands in the session — after the turn's own
    record, before anything else touches it;
  * the model is told its reply reaches that person too;
  * a notification to ANOTHER chat during the turn is logged at once, as before.

Run:  server/.venv/bin/python tests/test_notify_same_chat.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(tempfile.mkdtemp(prefix="myagent-samechat-"))
os.environ["MYAGENT_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT / "server"))

from app.engine.channel_turn import run_channel_turn  # noqa: E402
from app.models import ChatMessage, ChatRequest, ChatResponse  # noqa: E402
from app.storage.channel_sessions import NamedSessionStore  # noqa: E402
from app.tools import internal  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


MARIO, ANNA = "mario@example.com", "anna@example.com"


class FakeConnector:
    """A mail-like connector: one session per address, records each notify."""

    def __init__(self):
        self.calls: list[dict] = []

    def session_id_for(self, chat_id):
        return "mailbot_" + chat_id.replace("@", "_")

    async def notify(self, chat_id, text, subject="", files=None):
        self.calls.append(dict(chat_id=chat_id, text=text, subject=subject))
        return True, []


class FakeExecutor:
    """Runs the turn the way the model did: answers by notify_user to the
    sender, copies someone else, then writes a short final reply."""

    def __init__(self, named, state):
        self.agent = SimpleNamespace(id="curatore", autonomous=None, folder=None,
                                     memory_enabled=False)
        self.stores = SimpleNamespace(memory=None)
        self.named, self.state = named, state
        self.results: list[str] = []
        self.anna_logged_during_turn = None

    async def run(self, message, prior, attachments, **kw):
        self.results.append(await internal.notify_user_handler(
            text="Nel catasto risultano 2435 grotte.", subject="Richiesta",
            binding_id="mailbot", chat_id=MARIO, executor=self,
            _named=self.named, _state=self.state))
        self.results.append(await internal.notify_user_handler(
            text="Copia per conoscenza.", binding_id="mailbot", chat_id=ANNA,
            executor=self, _named=self.named, _state=self.state))
        anna = self.named.get("mailbot_" + ANNA.replace("@", "_"))
        self.anna_logged_during_turn = any(
            m.get("notification") for m in anna.get("messages", []))
        return ChatResponse(reply="Fatto.", conversation=[
            ChatMessage(role="user", content=message),
            ChatMessage(role="assistant", content="Fatto.")])


async def wait_for_notification(named, sid, timeout=3.0):
    """The append is queued behind the turn: poll until it lands."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        s = named.get(sid)
        if any(m.get("notification") for m in s.get("messages", [])):
            return s
        await asyncio.sleep(0.02)
    return named.get(sid)


async def test_same_chat():
    print("notify_user to the sender of the turn in progress:")
    conn = FakeConnector()
    manager = SimpleNamespace(get_connector=lambda bid: conn if bid == "mailbot" else None)
    state = SimpleNamespace(connectors=SimpleNamespace(manager=manager))
    named = NamedSessionStore(HOME / "sessions")
    ex = FakeExecutor(named, state)
    sid = conn.session_id_for(MARIO)
    req = ChatRequest(agent_id="curatore", message="quante grotte ci sono a catasto?",
                      session_id=sid, source="mail")
    try:
        resp = await asyncio.wait_for(run_channel_turn(req, named, ex), timeout=5)
    except asyncio.TimeoutError:
        check(False, "the turn completes (it waited on its own session lock)")
        return
    check(resp.reply == "Fatto.", "the turn completes and returns its reply")
    check(len(conn.calls) == 2 and conn.calls[0]["chat_id"] == MARIO,
          "the notification was handed to the connector")
    first = ex.results[0]
    print("   ->", first)
    check(first.startswith("Message sent via binding 'mailbot' to: mario@example.com."),
          "reported as sent, with no '(not added to its history)'")
    check("who you are answering" in first and "do not repeat" in first,
          "the model is told its reply reaches the same person")
    check("who you are answering" not in ex.results[1],
          "…and not when the recipient is someone else")
    check(ex.anna_logged_during_turn is True,
          "another chat's session is appended at once, during the turn")

    s = await wait_for_notification(named, sid)
    roles = [(m.get("role"), bool(m.get("notification"))) for m in s["messages"]]
    check(roles == [("user", False), ("assistant", False), ("assistant", True)],
          f"session: question, reply, then the notification: {roles}")
    check(s["conversation"][-1]["content"].startswith("Richiesta\n\nNel catasto"),
          "the model's history holds what was sent (subject first)")
    check(s["conversation"][-2]["content"] == "Fatto.",
          "…after the turn's own record, which it did not overwrite")


async def main():
    await test_same_chat()


try:
    asyncio.run(main())
finally:
    shutil.rmtree(HOME, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
