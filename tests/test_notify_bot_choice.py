#!/usr/bin/env python3
"""Which bot sends a notification, when several could.

Orin, 2026-11-20..25: two Telegram bots bound to the same agent, no default bot
on it. Every scheduled notify_user was refused as ambiguous ("several bots could
reach Sylvia — pass binding_id or channel to choose"), about 130 times in five
days, although each person had a conversation with exactly one of the two. The
model could not follow the advice either: binding_id is not in the tool's schema,
so it put the bot's id in `channel` and was refused again ("unknown channel
'master-it'").

Pinned here, with the real Connectors resolver over temporary stores and a fake
manager / session clock (no transport, no model):

  * the caller's own bots come first;
  * among several, the one whose conversation with the recipient moved last;
  * with nothing to tell them apart the error names `channel`, never binding_id;
  * `channel` takes a kind of channel OR one bot, by id or name, and a bot named
    there beats the preference;
  * a kind of channel the person is not on names the ones they are on;
  * the agent form's preview (reach_preview) says the same thing a send does.

Run:  server/.venv/bin/python tests/test_notify_bot_choice.py
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(tempfile.mkdtemp(prefix="myagent-botchoice-"))
os.environ["MYAGENT_HOME"] = str(STATE / "home")
os.environ["MYAGENT_CONNECTORS_DIR"] = str(STATE / "connectors")

sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "connectors" / "plugin"))

from myagent_connectors.models import Binding, Contact  # noqa: E402
from myagent_connectors.services import Connectors  # noqa: E402
from app.storage.store import JsonStore  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class FakeConnector:
    def __init__(self, binding: Binding):
        self.binding = binding

    def session_id_for(self, chat_id) -> str:
        return f"{self.binding.effective_prefix()}_{chat_id}"


class FakeManager:
    def __init__(self, bindings: JsonStore):
        self.bindings = bindings

    def get_connector(self, binding_id: str):
        data = self.bindings.get(binding_id)
        return FakeConnector(Binding(**data)) if data else None


class FakeCore:
    """session id -> updated_at: the only thing the resolver asks of the core."""

    def __init__(self):
        self.talked: dict[str, str] = {}

    def last_activity(self, session_id: str) -> str:
        return self.talked.get(session_id, "")


bindings = JsonStore(STATE / "bindings")
contacts = JsonStore(STATE / "contacts")
core = FakeCore()
svc = Connectors(bindings=bindings, contacts=contacts, grants=None, core=core,
                 manager=FakeManager(bindings))


def bot(id: str, agent: str, type: str = "telegram", **extra) -> None:
    bindings.save(id, Binding(id=id, type=type, agent_id=agent, **extra).model_dump())


# The Orin, verbatim in shape.
bot("master-it", "master", session_prefix="splx", name="Master IT")
bot("myagent-notebook-bot", "master")
bot("sala", "master", type="satellite")
ALE, SYLVIA = "471091560", "1489486090"
contacts.save("ale", Contact(id="ale", name="Alessandro Vernassa",
                             handles={"telegram": ALE}).model_dump())
contacts.save("sylvia", Contact(id="sylvia", name="Sylvia Mondinelli",
                                handles={"telegram": SYLVIA}).model_dump())


def resolve(to: str, channel: str = "", binding_id: str = "", agent: str = "master"):
    found, error, note = svc.resolve_recipients(to, channel, binding_id, agent)
    return (found[0].binding_id if found else ""), error, note


print("nothing to tell the bots apart")
sent, error, _ = resolve("Sylvia")
check(not sent and "several bots could reach Sylvia Mondinelli" in error,
      "still refused when no bot has a conversation with her")
check("pass one of them as channel: master-it, myagent-notebook-bot" in error,
      "the error names `channel` and lists plain bot ids")
check("binding_id" not in error, "…and never binding_id, which the model does not have")

print("the conversation decides")
core.talked[f"splx_{SYLVIA}"] = "2026-11-19T21:30:00"
check(resolve("Sylvia")[0] == "master-it", "the one bot she talks to is chosen")
check(resolve("Sylvia Mondinelli")[1] == "", "…with no error")
check(not resolve("Alessandro")[0], "per person: Alessandro is still ambiguous")
core.talked[f"splx_{ALE}"] = "2026-11-19T09:00:00"
core.talked[f"myagent-notebook-bot_{ALE}"] = "2026-11-24T18:00:00"
check(resolve("Alessandro")[0] == "myagent-notebook-bot",
      "two conversations: the one that moved last")

print("`channel` names a kind of channel or one bot")
check(resolve("Alessandro", "Telegram")[0] == "myagent-notebook-bot",
      "a kind of channel leaves the choice to the conversation")
check(resolve("Alessandro", "master-it")[0] == "master-it",
      "a bot id in `channel` is taken (it was 'unknown channel')")
check(resolve("Alessandro", "master it")[0] == "master-it", "…and so is its name")
check(resolve("Alessandro", "master-it", "myagent-notebook-bot")[0] == "master-it",
      "the bot the model named beats the configured one")
_, error, _ = resolve("Alessandro", "whatsapp")
check("unknown channel 'whatsapp'" in error and "master-it" in error
      and "Telegram" in error, "an unknown channel lists kinds AND bots")

contacts.save("sala", Contact(id="sala", name="Sala",
                              handles={"satellite": "sala"}).model_dump())
sent, error, _ = resolve("Sala", "Telegram")
check(not sent and "Sala has no Telegram handle, but can be reached on: Satellite" in error,
      "the wrong kind of channel says where the person IS reachable")
check(resolve("Sala", "Satellite")[0] == "sala", "…and following it gets through")
contacts.delete("sala")
contacts.save("mailonly", Contact(id="mailonly", name="Solo Mail",
                                  handles={"mail": "solo@example.com"}).model_dump())
_, error, _ = resolve("Solo Mail", "Telegram")
check("Add one in the address book" in error and "reached on" not in error,
      "no bot reaches them anywhere: the advice is still to add a handle")
contacts.delete("mailonly")


print("a bot is the voice of its agent")
bot("lavoro-bot", "cerca-lavoro")
check(resolve("Sylvia", agent="cerca-lavoro")[0] == "lavoro-bot",
      "the caller's only own bot, even with no conversation yet")
core.talked[f"lavoro-bot_{ALE}"] = "2026-11-25T08:00:00"
check(resolve("Alessandro")[0] == "myagent-notebook-bot",
      "another agent's bot is not chosen, however recent")
check(resolve("Alessandro", agent="guest")[0] == "lavoro-bot",
      "an agent with no bot of its own borrows the most recent conversation")

print("the preview is the send")
rows = {r["contact"]: r for r in svc.reach_preview("master")}
check(rows["Alessandro Vernassa"]["bot_id"] == "myagent-notebook-bot"
      and rows["Alessandro Vernassa"]["answered_by"] == "master",
      "per contact: the bot and the agent that answers the reply")
rows = {r["contact"]: r for r in svc.reach_preview("guest")}
check(rows["Alessandro Vernassa"]["answered_by"] == "cerca-lavoro",
      "a guest sees that somebody else answers")
contacts.save("new", Contact(id="new", name="Nuovo Arrivato",
                             handles={"telegram": "555"}).model_dump())
row = {r["contact"]: r for r in svc.reach_preview("guest")}["Nuovo Arrivato"]
check(not row["bot_id"] and row["undecided"] == ["lavoro-bot", "Master IT", "myagent-notebook-bot"],
      "an unresolved contact lists the bots nothing could tell apart")
contacts.delete("new")

print("no contact to pick the bot from")
sent, error, _ = resolve("all", "Telegram")
check(not sent and "pass one of them as channel" in error
      and "lavoro-bot" not in error,
      "a broadcast is narrowed to the caller's bots, then asks which")
check(resolve("all", "master-it")[0] == "master-it", "…and `channel` answers it")
core.talked["splx_-100123"] = "2026-11-25T10:00:00"
check(resolve("-100123", "Telegram")[0] == "master-it",
      "a raw group id goes through the bot that is in that group")

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all passed")
