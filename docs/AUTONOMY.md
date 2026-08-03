# Autonomous agents and scheduled tasks

Any agent can run unattended. Two things are needed, and only two:

1. switch **Live** on (in the agent editor, or with the play button on its card);
2. give it at least one **task**.

An agent with no task stays idle, live or not — there is no separate heartbeat.
A routine is just a task with a recurring schedule.

## Tasks

A task is *an agent* + *what to do* + *when*. Create them on the **Tasks** page,
where the *when* is a set of presets — once, every N minutes or hours, daily,
certain days of the week, or a raw cron expression — with a preview of the next
runs.

Or just ask the agent, if it has the `manage_tasks` tool:

> *"wake up in an hour and remind me to call the accountant"*
> *"every Monday at 9, prepare my week"*
> *"what is your next task?"*

Tasks are stored in `~/myagent/config/tasks/`, one JSON file each — they are
configuration, not runtime state, because a schedule is your intent and belongs
in your backup.

A one-shot task that has run stays in the list, disabled, as a visible record
you can delete.

## What a wake looks like

When a task comes due the agent runs a normal turn in its own session, marked
with a robot icon in the chat history. It reads the task and decides what to do
— including nothing: a wake that ends with `NOOP` and no tool call leaves no
trace.

A wake deliberately gets **no chat history**. A recurring task's history is
dozens of near-identical copies of itself, and a small local model reading its
own last output will happily reproduce it — we watched an agent report the same
stale failure for three wakes after the problem was fixed. Continuity comes
from long-term memory instead; add `memory_search` to the task text if the
agent needs more.

## Useful pieces to give a live agent

- **`manage_tasks`** — it schedules, reviews and cancels its own work
  ("check the backup log every morning").
- **`notify_user`** — it reaches you through the
  [connectors plugin](../connectors/README.md): a Telegram message, or a
  sentence spoken by a voice satellite. Recipients are contacts addressed by
  **name**, and the agent's autonomy settings hold the default one.
- **Memory** (`memory_enabled`) — so it remembers what it did across wakes.
- **`POST /api/tasks`** — trigger one from a script or a webhook. A task with
  no schedule is due immediately and runs once, which makes it a clean external
  poke.
- **Scheduling for others** (`schedule_others`) — let one agent create tasks
  for, and start, the *other* agents it can reach. Off by default.

## Guard rails

Autonomy is **off by default**, per agent, and the protections are not
optional:

| Limit | Default | What it does |
|---|---|---|
| `max_wakes_per_hour` | 12 | also the real floor: no task runs more often than every 5 minutes |
| `max_consecutive_errors` | 5 | auto-pauses the agent instead of looping on a broken task |
| `wake_timeout_s` | 600 | a stuck turn is cancelled |

A failed wake does **not** advance the schedule — the task stays due and is
retried — but the failure is still recorded, so the Tasks page shows red
instead of the last success.

Unpause with `POST /api/autonomy/{id}/resume` or by re-saving the agent.
`GET /api/autonomy/status` (or the badge on the agent card) shows the scheduler
state, and `POST /api/autonomy/{id}/wake` forces a turn — the main way to test
an agent's behaviour without waiting for its schedule.

A started agent restarts by itself after a reboot; the stop button (or
`live: false`) halts it within seconds and is the kill switch.
