# Chatterbox Multilingual on a laptop whose GPU is already taken

Kept apart from `server/` on purpose: nothing in here is part of the app.
`install.sh` at the repo root skips `contrib/`, so a deployment never ships it
and `update.sh` never touches what this folder installs.

**What it is.** [Chatterbox](https://github.com/resemble-ai/chatterbox) by
Resemble AI: zero-shot voice cloning TTS, 23 languages, Italian included. Give
it six seconds of someone's voice and it says anything in that timbre. This is
the model tested on `ntb-vaio` in September and rejected there — that CPU
reached **RTF 54.7** (4.5 s of speech for 249 s of compute) and could not even
serve as a remote TTS box. This laptop is a different machine entirely.

**Why anything needs to be stopped.** On `ntb-hp` the GPUs are both spoken for:
`llama.service` holds 3.2 of the 3.9 GiB of the Arc A370M (Vulkan1) and
`sd-server.service` holds the iGPU (Vulkan0). So the device choice decides
whether anything has to go dark:

| `--device` | what runs where | what is stopped |
|---|---|---|
| `cpu` (default) | 16 CPU threads; llama.cpp keeps the Arc | nothing — the chat keeps answering |
| `xpu` | the Arc, whole | `llama.service`, restarted afterwards |

The `xpu` path is the same trade `contrib/jetson-orin-8gb/sd-arbitrage-run`
makes on the Orin between `llama-server` and `sd-server`, and it borrows the
same two safeguards: a `flock` so two runs cannot stop and start the unit under
each other, and a detached guardian that restarts llama if the script is killed
before its `finally` block runs.

## Install

```bash
contrib/chatterbox-tts/install.sh --prefetch     # venv + CPU wheels + model
contrib/chatterbox-tts/install.sh --xpu          # then, to add the Arc route
```

Everything lands in `~/.local/share/chatterbox-tts` (venv, HF cache, TMPDIR) and
`chatterbox-say` is symlinked into `~/bin`. The installed copy is not the
checked-in file: its shebang is rewritten to the venv's python, since torch
exists nowhere else — so after editing `chatterbox-say` here, re-run
`install.sh` (it is quick once the venv exists). To remove every trace:
`rm -rf ~/.local/share/chatterbox-tts ~/bin/chatterbox-say ~/.pkuseg`.

Two pins are worth knowing about:

- **`setuptools<81`** is not cosmetic. `resemble-perth`, the watermarker
  chatterbox calls on *every* `generate()`, still imports `pkg_resources`, which
  setuptools 81 dropped. Without it the first synthesis crashes on the Vaio and
  here alike.
- **`torch==2.6.0`** is what the package pins, and the CPU index has exactly
  that. The XPU index starts at 2.7.1, so `--xpu` installs `2.7.1+xpu` *over*
  the pin deliberately. If a future chatterbox release breaks under it, the CPU
  route is still there.

## Use

```bash
chatterbox-say -o /tmp/prova.wav 'Ciao, sono il satellite della cucina.'
chatterbox-say -v ~/voce-mia.wav -o /tmp/clone.wav --play 'Prova di clonazione.'
chatterbox-say -d xpu -o /tmp/veloce.wav 'Questa usa la Arc.'    # stops llama
chatterbox-say --batch frasi.txt --outdir ~/repertorio/          # model loaded once
chatterbox-say -v voce.wav --textdir testi/ --outdir audio/ --limit 1   # try the first
chatterbox-say -v voce.wav --textdir testi/ --outdir audio/ --skip-existing
chatterbox-say -v voce.wav --pron pronuncia.tsv -f testo.txt     # respell names
```

`--batch` takes one phrase per line, optionally `name<TAB>text`, and loads the
model once for the whole list. That matters: the load is paid per invocation,
not per phrase, so pre-generating a repertoire of twenty fixed sentences costs
one load instead of twenty. `--textdir` does the same for a folder of `.txt`
files, one WAV each with the same name; `--limit N` takes only the first N
(to judge one before committing hours), `--skip-existing` leaves finished WAVs
alone, so the same command resumes an interrupted batch.

Long texts are split at sentence ends into chunks of at most `--max-chars`
(280) and joined with `--gap` seconds (0.35) of silence. That is not optional:
the model stops at 1000 speech tokens per call, about 40 s of audio, and
truncates silently past it. The voice is conditioned once per run, not once
per chunk.

`--pron FILE` fixes proper names the model stresses wrongly without touching
the texts: one `word<TAB>respelling` per line, whole word, case sensitive.
The tokenizer reads graphemes, so an accent moves the stress — `Martel` came
out *Màrtel*, `Martel<TAB>Martèl` gives *Martèl*.

## What this is actually good for

The conclusion from the Vaio still stands in shape, only the numbers moved:
Chatterbox is **not** a drop-in replacement for Piper on the satellites, because
a satellite answers with text it has never seen and must speak it now. What it
is good for is the **fixed repertoire** — the greeting, the alarm, "non ho
capito", the error lines — synthesised once in a cloned voice and played back
from WAV at zero cost. Piper keeps the dynamic half.

## Measured here (ntb-hp, 2026-10-23)

i7-1360P, 16 threads, CPU build (`torch 2.6.0+cpu`), `llama.service` left
running on the Arc the whole time — nothing stopped.

| | |
|---|---|
| model load (local HF cache) | 7 s |
| trial | 105 chars of Italian, cloned voice, `--exaggeration 0.45 --cfg 0.3` |
| audio produced | 7.0 s |
| synthesis time | 51 s |
| **RTF** | **7.4** |

For comparison the ntb-vaio did RTF 54.7 on the same model: this laptop is
7x faster on CPU alone. Projected for the Costacciaro talk (16 files,
14,313 chars, roughly 16 min of speech): about 2 hours in one `--textdir`
run, model loaded once. Slow but unattended, and the chat stays up — so the
CPU route is the default and `--xpu` is not needed for a batch job.

The first whole file confirmed it: `01_titolo.txt`, 595 chars in 4 chunks,
gave 35.7 s of audio in 251 s — RTF 7.0, every chunk ending on its own well
under the token cap (the longest used 346 of 1000).

Three things worth knowing:

- `forcing EOS token, long_tail=True` from the alignment analyzer is the
  normal end-of-utterance stop, not an error. If a clip ends clipped, that
  is the place to look.
- the output follows the reference's loudness, and a loud reference makes it
  overshoot full scale. The launcher now scales each take to a -1 dBFS peak
  before writing the 16-bit WAV; the Costacciaro batch predates that and has
  a few hundred clipped samples per file, in runs of 7 samples (0.3 ms) at
  most. Normalise loudness afterwards (two-pass `loudnorm`, -18 LUFS) so the
  files of one talk match.
- the multilingual tokenizer pulls `spacy-pkuseg`'s Chinese model on first
  load, into `~/.pkuseg/` — **outside** `CHATTERBOX_HOME`. Removing the
  prefix does not remove it.
