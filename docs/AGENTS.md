# Agents, and the devices they drive

An agent is `model + system prompt + tools`, stored as a JSON file under
`~/myagent/config/agents/` and editable from the UI.

![The bundled agents in the web UI](images/agents.png)

## The ten bundled agents

First run seeds ten, each running on the model chosen in **Settings**:

| Agent | Does | Needs internet? |
|---|---|---|
| **Master** | orchestrator: routes your question to the right agent, and schedules reminders and recurring jobs for itself | no |
| **Librarian** | answers from the offline library | no |
| **HTML Designer** | builds HTML pages, reports and dashboards and delivers them to the chat | no |
| **Home Automation** | drives IoT devices over their local HTTP APIs — fill in with your own | no |
| **System Administrator** | shell and file operations on the machine; converts PDFs, images and audio to text | no |
| **Coder** | writes scripts and programs in the workspace, runs them, and fixes what fails | no |
| **Conversation** | plain chat, no tools | no |
| **Tool Manager** | writes new tools for the other agents, and tests them | no |
| **Agent Manager** | creates and updates the agents themselves | no |
| **Web Researcher** | searches and reads web pages | yes |

Three are deliberately **not delegatable** (`callable: false`), so Master cannot
route to them: Tool Manager and Agent Manager, which write code and agents — you
select those on purpose — and Master itself, which is the entry point rather
than a target.

**Master keeps what its agents told it.** A delegated agent's reply is the only
record of what it found, so the last few are kept in front of Master on every
following turn, and it can pull up the full text of any of them with the
`recall_delegation` tool. Ask "what did you find so far?" a few turns later and
you get the facts, not "I have no information".

**HTML Designer** writes the page as a file in the workspace (`file_write`),
then delivers it with `show_file`: the page appears in the chat as a card that
opens in a sandboxed viewer, and the HTML itself never travels through the
conversation. Its pages are single self-contained files — CSS, JavaScript and
graphics (inline SVG) all embedded, no CDN — so they render offline and can be
copied anywhere as one file. Ask it for a report, a dashboard, a presentation,
or to update a page it made earlier.

If your install predates an agent listed above, it shows in **Agents** as a
dimmed card — one click on *Import* adds it.

**Don't want to pick an agent yourself?** The chat's agent selector has an
**Auto** entry: every message is first classified (one short LLM call over the
agent directory) and routed to the agent best suited to answer it — a small
"via *agent*" label at the top of each answer, shown as soon as the answer
starts, says who that was. Follow-ups stick: a
message like "try again" or "more detail" goes back to the agent that just
answered. When the classifier can't decide, the chat's last-used agent answers
instead, and a notice above the answer says so. Auto respects the same opt-out as delegation: agents with
*callable* off (Tool Manager, Agent Manager) are never auto-picked — except
Master, which stays the natural target for general questions.

## Editing never loses anything

- An agent or tool you changed shows a **modified** badge with a *reset to
  original* button.
- An agent you deleted stays in the list as a dimmed card you can re-import in
  one click.
- Your edits live in `~/myagent/`, outside the install directory, so upgrading
  never overwrites them.

## The agent form

Five tabs — General, Tools, Memory, Autonomy, Advanced — with state indicators
in the tab labels, so you can see from the outside which panes have something
switched on.

Some grants are managed rather than picked: the three memory tools follow the
single *memory* switch, and `call_agent` follows *can delegate*. The autonomy
tools stay individually selectable, because `autonomy_control` is genuinely
useful with `live` off.

### Working folder

*General → Working folder* points an agent at one directory. It becomes the
default root of the search tools, so `local_search` and `local_read` look there
instead of the shared library whenever a call passes no `path` — and the agent
stops having to repeat that path in its prompt, where a small model has to copy
it correctly on every call and hand the very same one back to `local_read` or
the result id will not open.

Simplifying such a prompt afterwards, **remove the path and nothing else**.
Measured on a 4B model against a folder of service manuals: deleting the path
alone kept the search working 3 times out of 3, while a broader rewrite of the
same prompt — same folder, same question — stopped the agent calling any tool
at all and left it answering from its own memory with an invented source.

The agent also carries what its own tools returned earlier in the chat into
the next turn's prompt, so a follow-up ("is there a report too?", "show it to
me") is answered from what it FOUND rather than from what it once said. That
covers the recall; it does not replace the instruction to search again, which
is what gets fresh facts.

In particular, keep whatever tells the agent **when** to search. A prompt that
only says "never invent" describes the answer, not the procedure: a small model
reads it, answers the first question correctly from a search, and then handles
the follow-ups from its own previous replies — confidently, and eventually
contradicting itself. A line as plain as *"your FIRST action is always
local_search, at every message, even for a follow-up"* is what puts the tool
call back.

The scope stops at searching. It does not change where `file_write` writes, nor
the tools' working directory, both of which stay on the workspace. And a folder
that does not exist is an error, never a silent fall back to the library — an
agent pointed at a drive you unplugged tells you so.

### Writing a description that routes

An agent's `description` is not a label: it is the **routing key**. In Auto mode
a classifier picks the agent from these one-liners alone, and when `master`
delegates it reads the same list. Everything measured on a 4B model against a
real installation:

- **Say what SUBJECT it covers, in the words the user will use.** "Esperto di
  salute" loses "quando era la visita cardiologica?" to the general library
  agent; naming the actual nouns — *referti, esami, analisi, ricette,
  prenotazioni, date delle visite* — wins it. 5/7 → 7/7.
- **Say whose material it is** when that is the distinction. Between an agent
  holding the user's medical records and one holding an encyclopedia, the word
  that separates them is *PERSONALI*, not *mediche*.
- **A negative half is often the load-bearing one.** "Non conoscenza medica
  generale" is what stops a health-records agent from being handed "che cos'è
  l'ipertensione?", the same way `master`'s prompt has to say coder is "not
  'sysadmin', which is for one-shot commands".
- **Do not write instructions to the router.** A description saying "you MUST
  use this agent for…" is a megaphone: the bundled `librarian` carried 553
  characters of it against 63 for a user's own agent, and won questions that
  were not its own. Routing rules belong in `master`'s prompt, which is where
  they already are.
- **The id does not do this work.** Renaming an agent to smuggle a keyword into
  its id changes nothing measurable — the classifier reads the description.
- **After cloning, rewrite the description first.** A clone inherits it
  verbatim, so two agents end up describing the same subject and the router
  picks between them by coin-flip.

### Cloning

Every agent card has a **Clone** button. It opens the form filled in from that
agent with the id and name blank, so you review the copy before it exists and
name it yourself. Long-term memory, scheduled tasks and autonomous state are
keyed by agent id and stay with the original, and the clone is always created
with *live* off — otherwise it would immediately start waking up to run
somebody else's schedule.

## Local devices & home automation

Agents reach the devices on your LAN through the `http_request` tool, which
speaks the local HTTP APIs that home-automation gear already exposes — Home
Assistant, Shelly, Tasmota, ESPHome, Philips Hue, ESP32 sketches, a Raspberry
Pi you wrote yourself. This all happens inside your network: no vendor cloud,
no internet.

The bundled **Home Automation** agent is a template. Open it in **Agents → Home
Automation** and list your devices in the *My devices* section of its system
prompt, one line each with the exact URL to call:

```text
- Living room light (Shelly): ON -> GET http://192.168.1.50/relay/0?turn=on
- Living room light (Shelly): OFF -> GET http://192.168.1.50/relay/0?turn=off
- Home Assistant: POST http://192.168.1.10:8123/api/services/light/turn_on
  header {"Authorization": "Bearer TOKEN"}  body {"entity_id": "light.kitchen"}
- Kitchen temperature: GET http://192.168.1.62/sensor/temp
```

Then just say *"turn on the living room light"*.

Why a prompt and not a device registry: your devices are already described
somewhere (their own docs, your notes), the model reads prose perfectly well,
and a registry would be one more schema to migrate. Keep the lines literal —
the exact URL, the exact header — because the model copies them.

For protocols beyond HTTP (MQTT, Zigbee, serial, GPIO) write a tool: a folder
with a `tool.json` and a `run` script that shells out to `mosquitto_pub`, a
Python library, or whatever your hardware speaks. See [TOOLS.md](TOOLS.md).

## Writing your own agents

Create one from **Agents → New**, or let the AI do it: the **Agent Manager**
creates and updates agents (through `manage_agents`), and the **Tool Manager**
writes and tests their tools (through `manage_tools`). Both are deliberately
not reachable by delegation — you select them on purpose.

A good agent is narrow. Every tool description ends up in the model's prompt,
so an agent with thirty tools spends its context explaining itself instead of
working; that matters most with the small local models this project targets.
Prefer several focused agents and let Master route between them.
