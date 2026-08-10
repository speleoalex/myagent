#!/usr/bin/env python3
"""`speakable()` — the markdown flattener in front of the satellite's TTS.

Run: python3 tests/test_satellite_speech.py   (stdlib only, no device needed:
satellite.py imports nothing but the standard library at module level, and
sounddevice is loaded lazily inside Microphone.)

The rules are regexes over model output, which is exactly the code that breaks
silently — a bad pattern does not raise, it just eats a word. So the cases below
come in two halves: markup that MUST go, and text that must survive UNTOUCHED.
The second half is the important one.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("satellite", ROOT / "satellite" / "satellite.py")
sat = importlib.util.module_from_spec(spec)
sys.modules["satellite"] = sat
spec.loader.exec_module(sat)
speakable = sat.speakable

# (label, input, expected output)
STRIPPED = [
    ("bold", "Una **ricetta** da provare", "Una ricetta da provare"),
    ("italic star", "è *molto* buono", "è molto buono"),
    ("italic underscore", "è _molto_ buono", "è molto buono"),
    ("bold+italic", "***davvero*** buono", "davvero buono"),
    ("nested", "**testo _annidato_ qui**", "testo annidato qui"),
    ("heading", "## Titolo\ntesto", "Titolo\ntesto"),
    ("inline code", "esegui `python clock.py` ora", "esegui python clock.py ora"),
    ("link", "vedi [la guida](https://esempio.it/x) qui", "vedi la guida qui"),
    ("image", "![grafico](img/a.png)", "grafico"),
    ("bullet dash", "- primo\n- secondo", "primo\nsecondo"),
    ("bullet star", "* primo\n* secondo", "primo\nsecondo"),
    ("numbered", "1. primo\n2) secondo", "primo\nsecondo"),
    ("quote", "> citazione", "citazione"),
    ("rule", "prima\n\n---\n\ndopo", "prima\n\ndopo"),
    ("fence kept inside", "Per eseguirla:\n\n```bash\npython test-clock/clock.py\n```",
     "Per eseguirla:\n\npython test-clock/clock.py"),
    ("table separator row", "| a | b |\n|---|---|\n| 1 | 2 |", "| a | b |\n| 1 | 2 |"),
    ("the reported case", "✅ Una **ricetta** da preparare", "✅ Una ricetta da preparare"),
]

# Nothing here is markup. If a rule touches any of it, that rule is wrong.
#
# `__dunder__` is the deliberate exception documented in satellite.py: it has the
# exact shape of `__bold__` and nothing can tell them apart, so the double
# underscore form is never stripped. Losing the underscores off `__init__` would
# corrupt an answer; reading them aloud in a rare `__bold__` only sounds clumsy.
PRESERVED = [
    ("snake_case filename", "apri clock_with_needles.py adesso"),
    ("two underscore words", "usa my_var e your_var insieme"),
    ("dunder", "il metodo __init__ della classe"),
    ("dunder pair", "confronta __init__ e __name__ ora"),
    ("multiplication", "sono 3 * 4 = 12 in tutto"),
    ("lone asterisk", "il campo * indica tutti i file"),
    ("shell pipe", "esegui ls | grep test per cercare"),
    ("plain prose", "Ciao! Come stai oggi?"),
    ("path with dashes", "il file test-clock/clock.py esiste"),
    ("percentage and math", "il 50% di 3-4 unità"),
    ("url alone", "vai su https://esempio.it/pagina per i dettagli"),
    ("hyphen mid sentence", "un attimo - arrivo subito"),
]

failures = []

for label, src, want in STRIPPED:
    got = speakable(src)
    if got != want:
        failures.append(f"[strip: {label}]\n  in:   {src!r}\n  want: {want!r}\n  got:  {got!r}")

for label, src in PRESERVED:
    got = speakable(src)
    if got != src:
        failures.append(f"[preserve: {label}]\n  in:  {src!r}\n  got: {got!r}  (must be unchanged)")

# A real reply, end to end: the one the kitchen device actually received.
REPLY = (
    "Ciao! 😊  \n"
    "Se hai bisogno di aiuto — sono qui per te.  \n\n"
    "Può essere:\n\n"
    "✅ Una **ricetta** da preparare  \n"
    "✅ Un **aiuto con i testi** (traduzioni, correzioni)  \n\n"
    "*(P.S.: puoi dire \"Aiutami!\")*"
)
spoken = speakable(REPLY)
if "*" in spoken or "_" in spoken:
    failures.append(f"[real reply] markup survived:\n  {spoken!r}")
for word in ("ricetta", "aiuto con i testi", "traduzioni", "Aiutami"):
    if word not in spoken:
        failures.append(f"[real reply] lost content {word!r}:\n  {spoken!r}")

if failures:
    print(f"FAIL — {len(failures)} case(s)\n")
    print("\n\n".join(failures))
    sys.exit(1)
print(f"OK — {len(STRIPPED)} stripped, {len(PRESERVED)} preserved, 1 full reply")
