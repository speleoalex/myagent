# EU AI Act: who is responsible for what

> This is engineering documentation, not legal advice. It records how MyAgent is
> built and why, so that whoever uses it can work out their own position. If you
> put MyAgent in front of other people, or sell anything built on it, get a
> lawyer to look at your case.

MyAgent ships **no model**. It is a framework: you supply the model, write the
system prompts, choose the tools and decide who may talk to it. Under
Regulation (EU) 2024/1689 that matters more than any feature, because the Act
assigns obligations to **roles**, and the role you land in depends on what you
do with it — not on what this repository contains.

## Where you are on the ladder

| What you do | Your role | What applies to you |
| --- | --- | --- |
| Run it at home, for yourself | none | **Nothing.** Art. 2(10) excludes natural persons using AI in a purely personal, non-professional activity |
| Publish a fork on GitHub, no money involved | arguably none | "Placing on the market" (art. 3(9)-(10)) means supply *in the course of a commercial activity*. A hobby release is generally not that |
| Use it in your job, practice or business | **deployer** | Art. 4 (AI literacy of your staff), art. 26 if the use is high-risk, and the GDPR for everything personal that passes through it |
| Sell it, sell hardware with it, or sell support for it | **provider** | Art. 50 in full, art. 5, plus Chapter III if the intended purpose is high-risk |
| Configure it for a use in Annex III | **provider of a high-risk system** | The whole of Chapter III: risk management, data governance, technical documentation, logging, human oversight, accuracy and robustness, conformity assessment, registration |

The last row is the one to watch, and it is not hypothetical: MyAgent is a
*factory* for AI systems. Art. 25 says that whoever puts their name on a system,
substantially modifies it, or changes its intended purpose so that it becomes
high-risk, **becomes its provider**. Writing an agent that screens CVs is not
"using MyAgent" — it is building a new system and shipping it.

### The open-source exemption, and its hole

MyAgent is MIT-licensed, so art. 2(12) applies: the Regulation does not cover AI
systems released under a free and open-source licence — **unless** they are put
on the market as high-risk, or as systems falling under **art. 5 or art. 50**.

A conversational assistant that generates text and speech falls under art. 50 by
definition. So the licence removes the heavy chapter and leaves exactly the two
that touch this project. Do not treat it as a general shield.

## Intended purpose

The authoritative statement is in the README ([EN](../README.md#what-it-is-for-and-what-it-is-not),
[IT](../README.it.md#a-cosa-serve-e-a-cosa-non-serve)). In short: a
general-purpose personal assistant that searches documents you own, runs tools
on your machine and talks to your devices.

This is not decoration. Under the Medical Device Regulation (EU) 2017/745 it is
the **manufacturer's stated intended purpose** that decides whether software is
a medical device — and Rule 11 would put software that provides information used
for diagnostic or therapeutic decisions in Class IIa or above. MyAgent's
offline library holds medical and first-aid archives, and the point of having
them is that they still answer when nothing else does. The distinction that
keeps this a documentation tool is that MyAgent **retrieves and quotes sources**;
it does not assess a patient and does not advise. Keep any wording you add on
the right side of that line.

The same logic explains why the emergency use case is described as consulting
references. Annex III(5)(d) makes *emergency call triage and dispatch* high-risk;
reading a first-aid manual is not that, and should never be presented as if it
were.

## Transparency (art. 50) — applicable since 2 August 2026

Art. 50(1) requires that a person interacting with an AI system be told so,
unless it is obvious in context. Art. 50(5) requires it **no later than the
first interaction**. Art. 50(2) requires synthetic content to be marked as
artificially generated. Here is each surface and what it does:

| Surface | Assessment | What is implemented |
| --- | --- | --- |
| **Web UI** | Obvious in context — you opened an app called MyAgent, picked an agent and typed at it. Art. 50(1)'s exception applies | nothing, deliberately |
| **Messaging bots** (Telegram) | **Not obvious.** The account is flagged "bot", which a scripted menu would be too, and the person may have been invited by someone else | a one-off notice per chat, before the first answer — `Binding.disclose_ai`, on by default, wording overridable per bot |
| **Voice satellite** | Obvious in context: the owner installed the speaker and configured it. There is no third party to inform | no notice. Real-time speech into a room also has no meaningful machine-readable channel |
| **Generated HTML** | Files leave the machine; origin must travel with them | the `html-designer` agent writes `<meta name="generator" content="MyAgent — AI-generated content">` into every page |
| **Generated speech** (Piper TTS) | Synthetic audio under art. 50(2), but not a deepfake: the voice imitates no real person, so art. 50(4) does not bite | not watermarked. Recorded here as a considered position, not an oversight |

The messaging disclosure fires from `BaseConnector._ensure_disclosed`
(`connectors/plugin/myagent_connectors/channels/base.py`), at the point where a
sender is known to be authorized — so it covers plain questions, not just
`/start` and `/help`, which only run when someone types them. It is sent once
per chat and persisted in `~/myagent/connectors/disclosed/`, because a notice
that repeats after every restart is one the operator switches off. A sender who
is *denied* gets no disclosure: they never reach the model. Pinned by
`tests/test_ai_disclosure.py`.

**If you turn it off**, you are asserting that the disclosure exists elsewhere —
in the bot's profile description, on a sign next to a kiosk — or that you are
outside the EU. That is a legitimate call, and it is yours.

## Prohibited practices (art. 5) — applicable since 2 February 2025

Nothing in MyAgent implements a prohibited practice: no emotion inference, no
biometric categorisation, no social scoring, no predictive policing. Two
capabilities could be *misused* into one, and are worth naming:

- `browse_web` plus scheduled autonomy could be pointed at untargeted scraping
  of facial images to build a recognition database — art. 5(1)(e).
- An agent given `http_request` and a contact list could be aimed at
  manipulative or exploitative messaging — art. 5(1)(a)-(b).

Neither is shipped, and neither is what the tools are for. Art. 5 binds whoever
puts the practice into service.

## If you do become a high-risk provider

Chapter III is a large amount of work and MyAgent cannot do it for you. It can
supply evidence, and several of the required artefacts already exist as a side
effect of how the system is built:

| Chapter III obligation | What MyAgent already produces |
| --- | --- |
| Art. 12 — automatic recording of events | Every turn is persisted with its full recursive trace, sub-agent calls included (`~/myagent/sessions/`, `server/app/storage/sessions.py`); the debug trace in Settings adds a full executor trace |
| Art. 14 — human oversight | Per-agent tool grants, `max_iterations` and `max_tool_calls` ceilings, duplicate-call suppression, and a stop button that interrupts a run in flight |
| Art. 13 — instructions for use | [ARCHITECTURE.md](ARCHITECTURE.md), [AGENTS.md](AGENTS.md), [AUTONOMY.md](AUTONOMY.md), [SECURITY.md](SECURITY.md), [CONFIGURATION.md](CONFIGURATION.md) |
| Art. 10 — data governance | The library is a set of files you assembled and can enumerate; answers cite the archive and entry they came from |
| Art. 15 — accuracy, robustness | Model and context window are probed rather than assumed; a fallback to a different model is announced in-band and never persisted silently |

What is **not** there, and would be yours to build: a risk management system
(art. 9), a quality management system (art. 17), the technical documentation of
Annex IV, conformity assessment, EU database registration (art. 49), post-market
monitoring (art. 72) and serious-incident reporting (art. 73).

## Personal data

Outside the AI Act, but it arrives with it. MyAgent stores conversations
(`~/myagent/sessions/`), long-term memory per agent (`~/myagent/memory/`), an
address book of the people your agents can message
(`~/myagent/connectors/contacts/`) and messaging identifiers of everyone who
writes to a bot. All of it stays on your machine, and there is no telemetry —
which disposes of international transfers and third-party processors, not of
your obligations as a controller if you use it professionally. Backups contain
all of it; see [SECURITY.md](SECURITY.md#backups-contain-everything).

## Dates

| Date | What became applicable |
| --- | --- |
| 2 Feb 2025 | Prohibited practices (art. 5), AI literacy (art. 4) |
| 2 Aug 2025 | General-purpose AI models, governance, penalties |
| **2 Aug 2026** | **General applicability, including the art. 50 transparency duties** |
| 2 Aug 2027 | High-risk systems that are products under Annex I sectoral legislation |

⚠️ The Commission's *Digital Omnibus* package, proposed on 19 November 2025,
would postpone parts of the high-risk regime and adjust art. 50. Check whether
and how it was adopted before relying on the table above.
