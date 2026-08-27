# Filling the offline library

`~/myagent/library/` is where MyAgent keeps the knowledge it can reach with no
network: ZIM archives and your own notes. This folder holds a **curated
catalog** of archives worth having and a **downloader** that puts them on a
disk of your choosing.

Nothing here is part of the running server. It is a one-off tool you use while
you still have a connection, so that later you don't need one.

```bash
library/fetch.py --list                              # what's on offer, with sizes
library/fetch.py --preset base                       # ~1.4 GB starting set
library/fetch.py --lang it --preset base             # ...plus the Italian material
library/fetch.py --preset health --dest /media/usb/library
library/fetch.py --lang it --only wikipedia-it --budget-gb 10
library/fetch.py --preset practical --urls           # just print URLs, download elsewhere
```

Downloads resume if interrupted (re-run the same command), are verified
against the publisher's SHA-256, and leave a `sha256sum`-compatible sidecar so
you can re-check the disk years later:

```bash
cd ~/myagent/library && sha256sum -c *.sha256
```

`--dest` defaults to `$MYAGENT_LIBRARY`, else `$MYAGENT_HOME/library`, else
`~/myagent/library`. The library is
just a folder: copy it to a second disk, move it, split it across drives —
the tools rescan on every search, subfolders included.

## How agents use it

Everything in `~/myagent/library/` becomes searchable knowledge. Two kinds of
file live side by side:

| In `~/myagent/library/` | Searched how |
|---|---|
| **Wikipedia / ZIM archives** (`*.zim`) | full-text index built into the archive |
| **Your notes and documents** (`.md`, `.txt`, `.rst`) | keyword scorer, one result per section |
| **PDFs** | keyword scorer over the text layer, one result per page |
| **Word, Excel, PowerPoint** (`.docx`, `.xlsx`, `.pptx`) | keyword scorer over the text, one result per section / sheet / slide |

With an **embedding model** chosen in Settings, all of these except the ZIM
archives are also matched by MEANING — which is what makes an Italian question find an answer in
an English manual. Measured on a folder of Mitsubishi service manuals: the
query *"pressione olio motore"* returns, in second place, the English page
titled `INSTALLATION OF OIL PRESSURE SWITCH`; keyword search alone puts *tyre*
pressure from an unrelated magazine article there instead.

It is opt-in and additive: the keyword results are exactly what they were, with
the semantic ones merged in beside them, and the ZIM archives are left alone
(they already have a full-text index; embedding millions of encyclopedia
articles buys nothing). The index builds in the background the first time you
search a folder, and Settings shows its progress.

The simplest way to switch it on needs no model server and nothing registered —
install the optional in-process embedder and pick it:

```bash
server/.venv/bin/pip install fastembed     # 8 packages, no torch
# then: Settings -> Embedding model -> "In this process"
```

To check the quality yourself on your own folder (this is the manual test the
automated ones deliberately skip, because it needs the 241 MB model):

```bash
S=server/tools/library/local_search/semindex.py
server/.venv/bin/python $S --root ~/myagent/library --index  --embed-local
server/.venv/bin/python $S --root ~/myagent/library --query "your question" --embed-local
```

Searching happens in **two steps**, so an agent pays only for the text it
actually wants: `local_search` returns a compact list (`id | title | snippet`),
and `local_read` opens one of those ids at length, a page at a time. A single
tool that returned full article bodies used to blow a small model's context
inside one turn — tool results are never truncated, so that text was re-sent on
every following iteration.

The **Librarian** agent is wired to both out of the box: ask it a question and
it searches, reads the best hit and answers from what it finds — citing the
article or file — or tells you the library doesn't cover it. The **Master**
agent routes general-knowledge questions to it before touching the web.

Ask for the **whole document** ("give me the full article") and the Librarian
delivers it into the chat as a file: `local_read`'s `export` flag writes a ZIM
article as a self-contained HTML page (readable inline, images included) and a
PDF or note as the original file. The text goes to *you*, never through the
model's context.

Several archives can coexist (English + Italian, say); an agent can restrict a
search to one edition with the tools' `lang` parameter, and each agent can have
its **own** knowledge folder instead of the shared one (both tools take an
optional `path`).

### Your own documents

Copy Markdown or plain-text files — manuals, procedures, notes, wiki exports —
straight in; subfolders are scanned recursively, and there is no import step and
no restart.

**PDFs are searched too**, one page at a time, as long as they carry a text
layer (`pdftotext` from poppler-utils does the reading). A result looks like
`p:manuals/clutch.pdf:5` and `local_read` opens the document at that page, so
an agent can cite the page it quotes. The extracted text is cached under
`~/myagent/cache/pdftext` and re-extracted by itself when the file changes:
the first search over a fresh folder pays for it once (measured: 7.7s for 108
manuals, 0.01s afterwards).

A **scanned** PDF has no text layer, so nothing in it is searchable — OCR it
first (`ocrmypdf`, or `document_extract`, which OCRs a scan page by page).
`local_search` names the PDFs it had to skip for this reason when it finds
nothing, rather than letting them look like covered ground.

**Office documents are searched too** — `.docx`, `.xlsx` and `.pptx`, with no
extra package to install: they are ZIP archives of XML, so MyAgent unzips them
itself. A Word heading becomes the title of the section it heads, a spreadsheet
is cited by sheet (`## Sheet: Costi 2026`) and a presentation by slide
(`## Slide 7`), so an agent can tell you where it read something. Table rows
stay on one line, or a table of torque figures stops saying which value belongs
to which bolt. Dates in a spreadsheet come out as real dates rather than the
five-digit numbers Excel stores, which is what makes *"when is the
appointment"* answerable.

The older `.doc`, `.xls` and `.ppt` (pre-2007) are a different, non-ZIP format
and are **not** searched. Convert them once — `libreoffice --headless
--convert-to docx *.doc` — and drop the results in.

`document_extract` reads them too, for a file you name by path instead of
searching for — including the pictures embedded in it. The pre-2007 `.doc`,
`.xls` and `.ppt` are the one thing it refuses, and it tells you the command to
convert them.

Images and audio are still not searched: convert them with the
`document_extract` tool (ask an agent to extract the file and write the
Markdown into the library), then they behave like any other note.

## Choosing what to take

Run `--list` for the full catalog with current sizes. Three things decide most
of it:

**Flavour.** Wikipedia-style archives come in three editions: `maxi` (full text
+ images), `nopic` (full text), `mini` (lead section only). **`mini` is an
index, not a manual** — the procedure you need lives in the sections it drops.
It is a reasonable choice only when disk is truly scarce. For medicine, botany
and repair, prefer `maxi`: the diagram *is* the content.

**Full-text index.** `--list` marks archives without one as `ft: NO`. Those
ship no search index, so `local_search` falls back to matching titles. They are
still worth having — `zimgit-post-disaster` is 615 MB of exactly the right
books — but the agent finds *the document*, not the sentence inside it.

**Language.** An agent quoting a treatment protocol should be quoting it in a
language the reader is not translating under stress. See below.

## Presets

| Preset | Size | + `--lang it` | What it is |
|---|---|---|---|
| `base` | 1.4 GB | 1.7 GB | Emergency medicine, water, food, knots, appropriate technology, drugs A-Z. The floor. |
| `health` | 4.8 GB | 5.1 GB | WikEM, WikiMed, MDWiki, military medicine, veterinary. |
| `survival` | 3.1 GB | 3.1 GB | Post-disaster procedures, preparedness, outdoors. |
| `practical` | 11.2 GB | 12.9 GB | Repair, growing, building, energy, radio, Q&A archives. |
| `wikipedia` | 16.0 GB | 24.6 GB | Full-text encyclopedia and dictionaries. |
| `reference` | 9.8 GB | 11.2 GB | Public-domain books (agriculture, medicine), taxonomy, courses. |
| `geo` | 0.2 GB | 2.8 GB | Maps and travel guides. |

The whole catalog is 70 GB, or 87 GB with the Italian overlay. There is no
preset for that on purpose: pick the two or three that match what you'd
actually need to *do*.

Some entries belong to no preset — the very large ones (`armypubs`,
`gutenberg-technology`, `se-electronics`, `se-physics`, `wikisource-it`).
They are deliberate choices, not defaults:
`library/fetch.py --only armypubs`.

## Languages

`catalog.json` is the **international** catalog: language-neutral material and
the English editions, which are usually the largest and most complete. Each
language is a separate overlay file loaded on top of it:

```bash
library/fetch.py --lang it --preset base     # catalog.json + catalog.it.json
```

The merge rule is one line: **a new id is added, a reused id replaces.** So
`catalog.it.json` *adds* Italian Wikipedia and Italian WikiMed next to their
English counterparts — different corpora, and you want both — while it
*replaces* `ifixit`, because the Italian iFixit is the same guides translated
and a second copy would be 3.6 GB of nothing.

Overlays can also add whole entries of their own (`scoutwiki-it`, `maps-italy`)
and slot them into the existing presets. They only need their own `presets`
block for something genuinely language-specific.

**To add a language** (`fr`, `es`, `de`, …), copy `catalog.it.json` to
`catalog.<lang>.json` and swap the archive names — Kiwix publishes most of
these in several languages, and `fetch.py --list` will pick the new file up
with no code change. Pull requests welcome.

Without `--lang`, nothing language-specific is downloaded: the script only
prints a one-line hint if it sees a catalog matching your `$LANG`. Guessing
would silently change what a scripted run fetches.

## Adding your own sources

`catalog.json` is data, not code — edit it, or keep a copy elsewhere and pass
`--catalog`. Two forms of entry:

```jsonc
{ "id": "my-wiki", "kiwix": "wikivoyage_fr_all", "flavour": "nopic",
  "lang": "fr", "topic": "geo", "presets": ["mine"], "note": "why I want it" }

{ "id": "my-manual", "url": "https://example.org/pump-manual.pdf",
  "topic": "practical", "presets": ["mine"], "note": "the actual pump I own" }
```

A `kiwix` entry names an archive and lets the script resolve the current
build; a `url` entry is downloaded verbatim, so any HTTP source works. New
preset names need a line under `"presets"` in the same file. Put anything
language-specific in the matching overlay rather than here, so the
international catalog stays usable by everyone.

**Why the catalog holds no Kiwix URLs:** those archives are date-stamped
(`wikipedia_it_all_nopic_2026-05.zim`) and Kiwix publishes no `latest`
symlink, so any URL written down here starts rotting immediately. The script
resolves the current filename, size and checksum from Kiwix's own catalog at
download time. Curation is stable; URLs are not.

## Worth having, not on Kiwix

These have no ZIM edition. Their deep links change often enough that listing
them here would be a list of 404s, so: the source, and what to look for.

- **Hesperian Health Guides** (hesperian.org) — *Where There Is No Doctor*,
  *Where There Is No Dentist*, *A Book for Midwives*. The standard for
  medicine without a hospital, free, many languages.
- **MSF Clinical Guidelines** (medicalguidelines.msf.org) — diagnosis and
  treatment plus *Essential Drugs*, written for places with no infrastructure.
- **WHO IRIS** (iris.who.int) — *Surgical Care at the District Hospital*,
  *Pocket Book of Hospital Care for Children*, the Essential Medicines list.
- **NCHFP / USDA** (nchfp.uga.edu) — *Complete Guide to Home Canning*.
  Preserving food wrong kills people; guessing here is not an option.
- **Sphere Handbook** (spherestandards.org) — humanitarian minimum standards:
  water, sanitation, shelter, quantities per person per day.
- **FAO** (fao.org/documents) — smallholder agriculture, seed saving, storage.
- **Survivor Library** (survivorlibrary.com) — scanned pre-industrial
  technical manuals: the era when these *were* the working methods.
- **A regional flora and mycology guide.** Wikipedia will not keep you from
  eating the wrong mushroom. This one should also exist on paper.
- **Manuals for the equipment you actually own** — pump, generator, inverter,
  vehicle, radio. No generic catalog can contain these.

Drop PDFs in as they are — they are searched page by page — but make sure they
carry a text layer: a scan without one is invisible to every search, and a bad
OCR layer is worse than no file, because it looks like coverage. Word, Excel and
PowerPoint files need nothing at all.

## Beyond documents

An offline library answers questions only if the rest still runs. Keep on the
same disk: the **model weights** (a `.gguf`, without which the assistant is a
search box), a copy of MyAgent itself, and the `.sha256` sidecars. Keep one
copy **disconnected** — the failure you are preparing for is not a typo.

And print the ten procedures you would need first. Paper is the only format
that does not depend on the thing that just failed.
