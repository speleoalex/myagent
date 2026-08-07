#!/usr/bin/env python3
"""The AI disclosure reaches every authorized sender, exactly once.

EU AI Act art. 50(1) asks that a person interacting with an AI system be told
so, and art. 50(5) wants it no later than the first interaction. On a messaging
bot that is not obvious from the context: the account is flagged "bot", which a
scripted menu would be too.

What makes this worth a test rather than a code comment is that the property is
invisible when it breaks. A disclosure that stops firing looks exactly like one
that fired before the log was rotated, and the person who should have been told
is by definition not the person reading the logs. The three ways it can break
are each pinned below:

  * it must fire on the PLAIN path, not just on /start and /help — those only
    run when someone types them, and in open/allowlist mode a person can simply
    ask a question (this is the gap the feature was written to close),
  * it must NOT repeat, or the operator turns it off and it protects nobody —
    including across a restart, hence the persisted store,
  * it must NOT go to a DENIED sender, who never reaches the model.

Run:  python3 tests/test_ai_disclosure.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A temporary state root, set BEFORE the plugin is imported: config.py reads the
# environment at import time, and the invariant is that connector tests never
# touch the real ~/myagent/connectors (which holds live bot tokens).
STATE = Path(tempfile.mkdtemp(prefix="myagent-disclosure-"))
import os
os.environ["MYAGENT_CONNECTORS_DIR"] = str(STATE)

sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "connectors" / "plugin"))

from myagent_connectors import config  # noqa: E402
from myagent_connectors.channels.base import (  # noqa: E402
    DEFAULT_AI_DISCLOSURE, BaseConnector,
)
from myagent_connectors.models import Binding  # noqa: E402
from myagent_connectors.storage import GrantStore  # noqa: E402

config.ensure_dirs()

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class FakeConnector(BaseConnector):
    """Records what would be sent, and never calls a model.

    ``_authorized`` and ``_ensure_disclosed`` are the code under test; the agent
    turn below them is stubbed, so nothing here needs a running server, a
    provider, or a bot token.
    """
    type = "fake"

    def __init__(self, binding):
        super().__init__(binding, client=None, grants=GrantStore(config.GRANTS_DIR))
        self.sent: list[str] = []

    async def send(self, chat_id, text: str, **kw) -> bool:
        self.sent.append(text)
        return True

    async def _run_turn(self, chat_id, user_id, text, **kw):
        self.sent.append("<agent reply>")


async def deliver(conn, text="ciao", user_id="1", chat_id="1"):
    """The part of process_message this test owns: access check + disclosure.

    Reimplemented rather than calling process_message, which would drag in the
    core client and the busy/typing machinery. The ORDER is what matters and it
    mirrors base.py: authorize, disclose, then answer.
    """
    allowed, note = conn._authorized(user_id, None, text)
    if not allowed:
        if note:
            await conn.send(chat_id, note)
        return
    await conn._ensure_disclosed(chat_id)
    if note:
        await conn.send(chat_id, note)
    await conn._run_turn(chat_id, user_id, text)


def binding(**kw) -> Binding:
    base = dict(id="testbot", type="fake", access_mode="open", agent_id="a")
    return Binding(**(base | kw))


async def main() -> None:
    print("Default on, plain message (no /start, no /help):")
    c = FakeConnector(binding())
    await deliver(c)
    check(c.sent and c.sent[0] == DEFAULT_AI_DISCLOSURE,
          "disclosure is the FIRST thing sent")
    check(c.sent.count(DEFAULT_AI_DISCLOSURE) == 1, "sent exactly once")
    check("<agent reply>" in c.sent, "the question still reaches the agent")

    print("\nSame chat again:")
    await deliver(c)
    await deliver(c)
    check(c.sent.count(DEFAULT_AI_DISCLOSURE) == 1, "not repeated in-process")

    print("\nAfter a restart (fresh connector, same state dir):")
    c2 = FakeConnector(binding())
    await deliver(c2)
    check(DEFAULT_AI_DISCLOSURE not in c2.sent, "not repeated across a restart")

    print("\nA second chat on the same bot:")
    c3 = FakeConnector(binding())
    await deliver(c3, user_id="2", chat_id="2")
    check(c3.sent and c3.sent[0] == DEFAULT_AI_DISCLOSURE,
          "each chat is told on its own")

    print("\nSwitched off:")
    c4 = FakeConnector(binding(id="offbot", disclose_ai=False))
    await deliver(c4)
    check(DEFAULT_AI_DISCLOSURE not in c4.sent, "nothing sent when disclose_ai is off")
    check("<agent reply>" in c4.sent, "the turn is unaffected")

    print("\nCustom wording:")
    custom = "Rispondo io, un'IA. Posso sbagliare."
    c5 = FakeConnector(binding(id="itbot", ai_disclosure=custom))
    await deliver(c5)
    check(c5.sent and c5.sent[0] == custom, "custom text replaces the built-in")
    check(DEFAULT_AI_DISCLOSURE not in c5.sent, "built-in not also sent")

    print("\nDenied sender (allowlist, id not listed):")
    c6 = FakeConnector(binding(id="closedbot", access_mode="allowlist",
                               allowed_ids=["999"]))
    await deliver(c6, user_id="123")
    check(DEFAULT_AI_DISCLOSURE not in c6.sent,
          "no disclosure: they never reach the model")
    check("<agent reply>" not in c6.sent, "and no agent turn")

    print("\nPassword grant (/start <pw>) — the third entry path:")
    c7 = FakeConnector(binding(id="pwbot", access_mode="password", password="s3cret"))
    await deliver(c7, text="/start s3cret", user_id="7", chat_id="7")
    check(DEFAULT_AI_DISCLOSURE in c7.sent, "disclosed on the granting message")
    check(c7.sent.index(DEFAULT_AI_DISCLOSURE) == 0, "before the grant notice")

    print("\nUnreadable state dir does not break the message path:")
    c8 = FakeConnector(binding(id="brokenbot"))
    c8._disclosed = None  # any access now raises
    await deliver(c8, chat_id="8")
    check("<agent reply>" in c8.sent, "the question still gets answered")
    check(DEFAULT_AI_DISCLOSURE in c8.sent, "and it degrades toward disclosing")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(STATE, ignore_errors=True)

if failures:
    print(f"\nFAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("\nOK — the AI disclosure fires once per chat, on every path, "
      "and only for senders who reach the agent.")
