#!/usr/bin/env python3
"""A notification another agent pushed through a bot is not the bot's own word.

Any agent with notify_user may write through any bot; the message lands in that
bot's conversation with the recipient, and the reply is answered by the BOT's
agent. Unsigned, the answering agent replays the guest's message as an assistant
turn of its own: "cerca-lavoro" writes about a job offer through master's bot,
the person replies "tell me more", and master is asked about an offer it never
looked for, in its own voice.

Pinned here, with the real NamedSessionStore and _log_to_channel (temporary
MYAGENT_HOME, no model, no transport):

  * every logged notification is signed with the agent that sent it;
  * on the next turn the bot's agent gets a guest's message flagged foreign
    (the executor then quotes it instead of replaying it);
  * its OWN notifications, and its ordinary replies, are left alone;
  * the channel turn actually applies the flag (source inspection).

Run:  server/.venv/bin/python tests/test_guest_notification.py
"""

import asyncio
import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="myagent-guest-")
os.environ["MYAGENT_HOME"] = _TMP  # before importing app.config

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "server"))

from app.engine import channel_turn  # noqa: E402
from app.engine.agent_router import mark_guest_notifications  # noqa: E402
from app.models import ChatMessage  # noqa: E402
from app.storage.channel_sessions import NamedSessionStore  # noqa: E402
from app.tools.internal import _log_to_channel  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class Sender:
    """What _log_to_channel reads off an executor."""

    def __init__(self, agent_id: str):
        self.agent = type("A", (), {"id": agent_id})()


SID = "splx_471091560"
named = NamedSessionStore(Path(_TMP) / "sessions")


async def main() -> None:
    session = named.get(SID, "master")
    session["messages"] = [{"role": "user", "text": "ciao", "agent_id": "master"},
                           {"role": "assistant", "text": "Ciao Alessandro!"}]
    session["conversation"] = [{"role": "user", "content": "ciao"},
                               {"role": "assistant", "content": "Ciao Alessandro!"}]
    named.save(SID, session)

    check(await _log_to_channel(SID, "Nuovo annuncio: sistemista", Sender("cerca-lavoro"), named),
          "a guest's notification is logged")
    check(await _log_to_channel(SID, "Domani piove", Sender("master"), named),
          "…and so is the bot's own")

    session = named.get(SID)
    signed = [(m["text"], m.get("agent_id")) for m in session["messages"]
              if m.get("notification")]
    check(signed == [("Nuovo annuncio: sistemista", "cerca-lavoro"),
                     ("Domani piove", "master")],
          "each notification is signed with its sender")

    prior = [ChatMessage(**m) for m in session["conversation"]]
    mark_guest_notifications(prior, session, "master")
    flagged = [m.content for m in prior if m.foreign]
    check(flagged == ["Nuovo annuncio: sistemista"],
          "the bot's agent sees only the guest's message as foreign")

    prior = [ChatMessage(**m) for m in session["conversation"]]
    mark_guest_notifications(prior, session, "cerca-lavoro")
    check([m.content for m in prior if m.foreign] == ["Domani piove"],
          "seen from the other agent, the roles swap")
    check(not prior[1].foreign, "an ordinary reply is never flagged")


asyncio.run(main())
check("mark_guest_notifications(prior, session, req.agent_id)"
      in inspect.getsource(channel_turn.run_channel_turn),
      "the channel turn applies it")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
