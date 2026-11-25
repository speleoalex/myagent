# `job_search`: LinkedIn job postings for a daily search agent

A user-layer tool, not part of the app: `install.sh` skips `contrib/`. It runs
on the Orin for the `cerca-lavoro` agent, which every morning searches new job
postings, ranks them against a criteria file and mails a report.

It reads LinkedIn's public listings (the `/jobs-guest/` pages served to
logged-out visitors), with the standard library only and no account. Indeed
refuses scripts and InfoJobs Italia has closed, so this is the one large board
reachable this way. Being an unofficial endpoint, it can change without
notice.

## Install

```bash
cp -r job_search ~/myagent/tools/
chmod +x ~/myagent/tools/job_search/run
```

No restart: tools are picked up on the next call. Grant `job_search` to the
agent.

## Actions

| `action` | What it does |
|---|---|
| `daily` | runs **every** search listed in `criteria_file` in one call; returns the outcome of each search and only the postings first seen today, each listed once with the searches that found it |
| `search` | one search: `keywords`, `location` (default `Italia`), `remote`, `days` (1-30), `max_results` |
| `detail` | full text and criteria of one posting, by `job_id` |

`daily` exists because a model asked to run twelve searches one by one ran ten,
judging two redundant, and then reported that none had failed. The list is now
executed by the tool, and the model only reads and ranks.

"New" means first seen **today**: a run retried the same day still gets the
same postings as new, and yesterday's stop showing up. The ids are kept in
`$MYAGENT_STATE_DIR/seen.json` for 120 days (in the workspace on MyAgent
versions without `MYAGENT_STATE_DIR`; that file is read once after an upgrade).

## The criteria file

`daily` reads the first `## ` section whose title names the searches
(`Ricerche ...` or `... searches`). In it, a line with `` `location` = "X" ``
(and optionally `` `remote` = true ``) opens a group, each `- ` bullet below is
one search, and `` `days` = N `` anywhere in the section sets the window:

```markdown
## Ricerche da fare ogni giorno

Tutte su LinkedIn con `job_search`, `days` = 2.

Genova (`location` = "Genova"):

- responsabile sviluppo software
- head of software

Italia full remote (`location` = "Italia", `remote` = true):

- engineering manager
- tech lead
```

The same file holds the profile and the rules for ranking, which the agent
reads itself: one file to edit to change what is searched and how it is
judged.

## Test without touching the real state

```bash
echo '{"action":"daily","criteria_file":"/path/to/CRITERI.md"}' \
  | MYAGENT_STATE_DIR=/tmp/js-test ~/myagent/tools/job_search/run
```

Twelve searches take about 100 seconds (pauses between searches and pages
keep LinkedIn from answering 429; a 429 is retried after 5, 15 and 30 s).
