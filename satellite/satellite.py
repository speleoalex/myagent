#!/usr/bin/env python3
"""MyAgent voice satellite — a small speaker/microphone client for a PC or
Raspberry Pi.

Push-to-talk: press Enter, speak, pause — the recorded audio goes to MyAgent's
connectors inbound endpoint (transcribed server-side, answered by the agent
bound to this device) and the reply is spoken back through Piper. A tiny HTTP
server (/say, /health) lets MyAgent push unsolicited messages — reminders,
alarms — that the device speaks too; that is what makes the satellite an
address-book entry agents can notify.

It also serves its OWN page at http://<device>:8899/ (`ui.html`): a text box to
type to the agent, a Listen button that opens the device's microphone, and the
settings. That page is what makes a headless install usable — as a systemd
service there is no Enter to press, and before it existed the microphone was
unreachable in exactly the setup the device is meant for.

Three ways in, one path out (`run_turn`): terminal Enter, the page's Listen
button, the page's text box.

Dependencies: sounddevice (microphone; optional — the device still speaks and
can be typed to without it) and the `piper` CLI with a voice model (TTS; without
it, texts are printed only). Everything else is the standard library.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("satellite")

BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "name": "Satellite",
    "myagent_url": "http://127.0.0.1:8888",
    "binding_id": "",
    "key": "",
    "listen_host": "0.0.0.0",
    "listen_port": 8899,
    "language": "",             # what is SPOKEN here, e.g. "it" — sent with the
                                # audio so the server transcribes in it instead
                                # of guessing. Empty = let whisper detect.
    "voice": "",                # path to a .onnx Piper voice; empty = TTS off
                                # (read from the legacy key "piper_voice" too)
    "volume": 80,               # playback volume 0-100 (ALSA mixer, see below)
    "request_timeout_s": 180,    # a local model can take minutes on a long turn
    "audio": {
        "samplerate": 16000,
        "silence_threshold": 250,   # RMS floor that counts as speech (int16).
                                    # Measured on the first real device (a USB
                                    # speaker bar, someone talking at it): noise
                                    # floor ~25, speech peaking ~1000-2000 with
                                    # most blocks well under 500. At 500 the
                                    # button opened the microphone and NOTHING
                                    # ever started the recording — a default has
                                    # to work on the hardware it ships for.
        "silence_ms": 700,          # this much silence closes the utterance
        "max_seconds": 30,
        "wait_speech_s": 10,        # give up if nothing is said after Enter
        "max_gain": 8,              # ceiling for the auto-gain below; 1 = off
    },
}

# What MyAgent is allowed to write through PUT /config. Everything else — the
# shared key, binding_id, myagent_url, listen_host/port — is PAIRING, set once
# by install.sh on the device: a remote call that can repoint a device at
# another server, or move the port it is being reached on, cuts the only wire
# it could be fixed over. Tuning is remote, identity is local.
WRITABLE = ("name", "language", "voice", "volume", "request_timeout_s")
WRITABLE_AUDIO = ("samplerate", "silence_threshold", "silence_ms",
                  "max_seconds", "wait_speech_s", "max_gain")
_NUMERIC = ("volume", "request_timeout_s")  # WRITABLE_AUDIO is all numeric


def _as_number(key: str, value):
    """Refuse a non-numeric value at the door (ValueError → 400): a stored
    ``"silence_threshold": "x"`` would not fail here, it would crash every
    later capture until config.json is fixed by hand."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    return int(n) if n.is_integer() else n


# The device's own page (typing, Listen, settings). A file rather than a string
# in here: it is read per request, so it can be edited on the device and
# reloaded with F5, and satellite.py stays readable.
UI_FILE = BASE_DIR / "ui.html"


def config_path() -> Path:
    return Path(os.environ.get("MYAGENT_SATELLITE_CONFIG") or BASE_DIR / "config.json")


def load_config() -> dict:
    """config.json next to this file (path override: MYAGENT_SATELLITE_CONFIG),
    merged over the defaults so a partial file is fine."""
    path = config_path()
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        user = json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("no config file at %s — using defaults (run install.sh, "
                    "or copy config.example.json)", path)
        return cfg
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    # The voice field was "piper_voice" before the device could be configured
    # remotely. Folded in on READ, like the core's own legacy-key migrations:
    # nothing rewrites the file until something else saves it, so a device that
    # is only ever edited by hand keeps working untouched.
    legacy = cfg.pop("piper_voice", "")
    if legacy and not cfg.get("voice"):
        cfg["voice"] = legacy
    return cfg


def update_config(cfg: dict, patch: dict) -> list[str]:
    """Apply a patch to the live config and persist it. Returns the field names
    actually changed (an empty list means the patch said nothing new).

    ``cfg`` is mutated IN PLACE, and that is the mechanism, not a shortcut:
    ``Microphone`` holds a reference to ``cfg["audio"]`` and reads the
    thresholds at every capture, so an in-place update retunes the microphone
    without a restart — which is the whole point when the value being tuned is
    a silence threshold you can only find by trial. Rebinding the dict would
    leave the mic on the old numbers and look like the save had failed.
    """
    changed = []
    for key in WRITABLE:
        if key not in patch:
            continue
        value = (_as_number(key, patch[key]) if key in _NUMERIC
                 else str(patch[key] or ""))
        if value != cfg.get(key):
            cfg[key] = value
            changed.append(key)
    for key in WRITABLE_AUDIO:
        if key in (patch.get("audio") or {}):
            value = _as_number(f"audio.{key}", patch["audio"][key])
            if value != cfg["audio"].get(key):
                cfg["audio"][key] = value
                changed.append(f"audio.{key}")
    if changed:
        save_config(cfg)
    return changed


def save_config(cfg: dict) -> None:
    """Write config.json atomically, 0600 — it holds the shared key."""
    path = config_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ------------------------------------------------------------------ volume
# Volume is a MIXER setting, not a per-stream one: piper streams raw audio into
# aplay, which has no gain of its own. amixer is the dependency-free way to move
# it — and the control is DISCOVERED, because it is called Master on the Pi's
# analogue jack, PCM or Speaker on most USB cards. A device with no mixer at all
# reports that instead of pretending: the page then hides the slider rather than
# offering one that does nothing.
_MIXER_PREFS = ("Master", "PCM", "Speaker", "Headphone", "Digital", "HDMI")
_mixer_control: str | None = None      # None = not looked up, "" = none found


def mixer_control() -> str:
    global _mixer_control
    if _mixer_control is None:
        _mixer_control = ""
        try:
            out = subprocess.run(["amixer", "scontrols"], timeout=5,
                                 capture_output=True, text=True).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        found = re.findall(r"Simple mixer control '([^']+)'", out)
        # Prefer a known playback control; a capture-only card would otherwise
        # hand us 'Capture' and every volume change would move the microphone.
        for name in _MIXER_PREFS:
            if name in found:
                _mixer_control = name
                break
        else:
            _mixer_control = next((f for f in found if f != "Capture"), "")
    return _mixer_control


def set_volume(pct: int) -> bool:
    """Set playback volume 0-100. False = this device has no mixer to move."""
    control = mixer_control()
    if not control:
        return False
    try:
        pct = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        return False
    # -M asks for the perceptual (mapped) scale: on the Pi's bcm2835 the raw
    # scale is dB-linear, where 50% is already almost inaudible.
    for args in (["-M"], []):
        try:
            subprocess.run(["amixer", "-q", *args, "sset", control, f"{pct}%"],
                           check=True, timeout=5, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            if pct:
                _unpark_hw_playback()
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    log.warning("could not set volume on mixer control %r", control)
    return False


def _unpark_hw_playback() -> None:
    """Lift back any card playback control PulseAudio parked at zero.

    The control we just moved is usually PulseAudio's own proxy, and pulse then
    decides how to split the volume between software and the card. When a card
    advertises a useless hardware range — the kitchen's USB speaker bar declares
    its whole 'PCM Playback Volume' as 0.00 to 0.39 dB — pulse takes the volume
    into software and leaves the element at its minimum. On that bar the
    minimum is not 0.39 dB down, it is SILENCE: measured, the same TTS phrase
    goes from a peak of 2260 on the room microphone to 56, i.e. nothing. So the
    device came up mute after every restart (set_volume runs at startup) and the
    volume slider muted it again on every move — a bug that reads, from the
    kitchen, as "the satellite stopped talking to me".

    Only a control sitting at exactly zero WHILE we were asked for a non-zero
    volume is touched: that combination is never a setting anyone chose, and
    anything above zero is left alone so a card whose hardware volume really is
    the volume keeps working.
    """
    try:
        cards = re.findall(r"^\s*(\d+)\s+\[", Path("/proc/asound/cards").read_text(),
                           re.M)
    except OSError:
        return
    for card in cards:
        try:
            out = subprocess.run(["amixer", "-c", card, "scontents"], timeout=5,
                                 capture_output=True, text=True).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        # scontents gives every control in one call: name, then its channel
        # lines. A control is ours to fix if it plays and reads [0%].
        for block in out.split("Simple mixer control ")[1:]:
            name = block.partition("'")[2].partition("'")[0]
            if not name or "Playback " not in block or "Playback 0 [0%]" not in block:
                continue
            log.info("mixer %s:%s was parked at 0%% — restoring", card, name)
            for args in (["-M"], []):
                try:
                    subprocess.run(["amixer", "-q", "-c", card, *args,
                                    "sset", name, "100%"], check=True, timeout=5,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    break
                except (OSError, subprocess.SubprocessError):
                    continue


# --------------------------------------------------------------------- TTS
class Speaker:
    """Serialized text-to-speech: one queue, one worker, so a /say arriving
    mid-reply queues up instead of speaking over it."""

    # Drop symbols/emoji before TTS (a model reply may carry ⏳ or ✅); keep
    # letters of any alphabet, digits and plain punctuation.
    _UNSPEAKABLE = re.compile(r"[℀-\U0010FFFF]")

    def __init__(self, cfg: dict):
        self.voice = str(cfg.get("voice") or "")
        self.piper = shutil.which("piper") or str(Path(sys.executable).parent / "piper")
        self.sample_rate = self._voice_rate()
        self._q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()
        if not self.available:
            log.warning("TTS unavailable (piper or voice model missing): "
                        "replies will be printed, not spoken")

    @property
    def available(self) -> bool:
        return bool(self.voice) and Path(self.voice).exists() \
            and Path(self.piper).exists()

    def apply(self, cfg: dict) -> None:
        """Adopt a voice picked remotely, mid-run. Read at speak time, so the
        queued announcements that have not been spoken yet use the new one."""
        self.voice = str(cfg.get("voice") or "")
        self.sample_rate = self._voice_rate()

    def _voice_rate(self) -> int:
        """Piper ships a <voice>.onnx.json with the audio sample rate."""
        try:
            meta = json.loads(Path(self.voice + ".json").read_text())
            return int(meta["audio"]["sample_rate"])
        except Exception:
            return 22050

    def say(self, text: str, done: threading.Event | None = None) -> None:
        """Queue an utterance. `done` is set when it has been spoken (or
        skipped): the page's volume test waits on it, because "sent" and
        "heard" are seconds apart and the button is the only feedback."""
        text = self._UNSPEAKABLE.sub(" ", text or "").strip()
        if not text:
            if done is not None:
                done.set()
            return
        self._q.put((text, done))

    def _worker(self) -> None:
        while True:
            text, done = self._q.get()
            # flush: under systemd stdout is a pipe (block-buffered) and the
            # journal would otherwise show announcements minutes late.
            print(f"🔊 {text}", flush=True)
            try:
                if self.available:
                    self._speak(text)
            except Exception as e:
                log.warning("TTS failed: %s", e)
            finally:
                if done is not None:
                    done.set()

    def _speak(self, text: str) -> None:
        # piper streams raw s16le mono at the voice's rate; aplay plays it.
        piper = subprocess.Popen(
            [self.piper, "--model", self.voice, "--output_raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        aplay = subprocess.Popen(
            ["aplay", "-q", "-r", str(self.sample_rate), "-f", "S16_LE",
             "-t", "raw", "-c", "1"],
            stdin=piper.stdout, stderr=subprocess.DEVNULL)
        piper.stdout.close()  # let aplay see EOF when piper finishes
        piper.stdin.write(text.encode("utf-8"))
        piper.stdin.close()
        aplay.wait(timeout=120)
        piper.wait(timeout=5)


# ----------------------------------------------------------------- voices
VOICES_DIR = BASE_DIR / "voices"

# Piper publishes voices as <lang>/<lang_REGION>/<speaker>/<quality>/<name>.onnx
# (+ .onnx.json). install.sh calls voice_url() below for the first voice; the
# /voices/install endpoint uses it for every later one, so a device that
# shipped with Italian can be given a second language without an ssh session.
VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def installed_voices() -> list[str]:
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))


def voice_url(name: str) -> str:
    """it_IT-paola-medium -> …/it/it_IT/paola/medium/it_IT-paola-medium"""
    locale, _, rest = name.partition("-")          # it_IT | paola-medium
    speaker, _, quality = rest.partition("-")
    lang = locale.partition("_")[0]
    if not (locale and speaker and quality):
        raise ValueError(f"not a piper voice name: {name!r} "
                         f"(expected <lang>_<REGION>-<speaker>-<quality>)")
    return f"{VOICES_BASE}/{lang}/{locale}/{speaker}/{quality}/{name}"


class VoiceInstaller:
    """Downloads a Piper voice in the background.

    Background, and not inside the request, because a voice is 20-110 MB: on a
    Raspberry's wifi that is minutes, and an HTTP call held open that long is
    one the caller has already timed out on — it would look like a failure while
    the download was in fact fine. So the POST returns at once and the state
    machine below is published in GET /config for the UI to poll.
    """

    _NAME_OK = re.compile(r"^[A-Za-z]{2,3}_[A-Za-z]{2,4}-[A-Za-z0-9]+-[a-z_]+$")

    def __init__(self):
        self._lock = threading.Lock()
        self.state: dict = {}

    def start(self, name: str, on_done=None) -> dict:
        """Returns the state to answer with. Refuses a second concurrent
        install: the disk and the link are the resources, one at a time."""
        if not self._NAME_OK.match(name or ""):
            return {"name": name, "state": "error",
                    "error": "not a piper voice name, e.g. it_IT-paola-medium"}
        with self._lock:
            if self.state.get("state") == "downloading":
                return dict(self.state, error="another voice is downloading")
            self.state = {"name": name, "state": "downloading", "error": ""}
        threading.Thread(target=self._run, args=(name, on_done),
                         daemon=True).start()
        return dict(self.state)

    def _run(self, name: str, on_done) -> None:
        onnx = VOICES_DIR / f"{name}.onnx"
        try:
            VOICES_DIR.mkdir(parents=True, exist_ok=True)
            url = voice_url(name)
            # .json first: it is tiny, and a wrong name fails here in a second
            # instead of after a 60 MB download.
            self._fetch(url + ".onnx.json", onnx.with_suffix(".onnx.json"))
            self._fetch(url + ".onnx", onnx)
        except Exception as e:
            # A partial file would satisfy Speaker.available and then make piper
            # fail on every single announcement.
            onnx.unlink(missing_ok=True)
            onnx.with_suffix(".onnx.json").unlink(missing_ok=True)
            log.warning("voice %s failed to install: %s", name, e)
            with self._lock:
                self.state = {"name": name, "state": "error", "error": str(e)[:200]}
            return
        log.info("voice %s installed", name)
        with self._lock:
            self.state = {"name": name, "state": "done", "error": ""}
        if on_done:
            try:
                on_done(name)
            except Exception as e:      # never kill the thread on a callback
                log.warning("voice %s: post-install step failed: %s", name, e)

    @staticmethod
    def _fetch(url: str, dest: Path) -> None:
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh, length=256 * 1024)
        tmp.replace(dest)


# ------------------------------------------------------------- microphone
class Microphone:
    """Push-to-talk capture with RMS endpointing, no numpy: sounddevice's
    RawInputStream hands over int16 bytes and 30 ms blocks are small enough
    to sum in pure Python."""

    BLOCK_MS = 30

    def __init__(self, cfg: dict):
        self.cfg = cfg["audio"]
        # One capture at a time. There are now two ways to start one — Enter in
        # the terminal and the Listen button on the device's page — and two
        # RawInputStreams on one microphone is a driver error, not a queue.
        self._busy = threading.Lock()
        try:
            import sounddevice  # noqa: F401 — probe only
            self._sd = sounddevice
            self.available = True
        except Exception as e:
            self._sd = None
            self.available = False
            log.warning("microphone unavailable (%s): no capture, "
                        "the device can still speak and be typed to", e)

    @property
    def listening(self) -> bool:
        return self._busy.locked()

    @staticmethod
    def _rms(block: bytes) -> float:
        n = len(block) // 2
        if not n:
            return 0.0
        samples = struct.unpack(f"<{n}h", block)
        return (sum(s * s for s in samples) / n) ** 0.5

    def record(self) -> bytes | None:
        """One utterance as WAV bytes: wait for speech, stop on silence.
        None when nothing was said. Raises RuntimeError if already capturing —
        the caller (a button, or the terminal loop) must be told, not queued
        behind a recording whose speaker has already stopped talking."""
        if not self.available:
            raise RuntimeError("no microphone on this device")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("already listening")
        try:
            return self._record()
        finally:
            self._busy.release()

    def _record(self) -> bytes | None:
        a = self.cfg
        rate = int(a["samplerate"])
        block_frames = rate * self.BLOCK_MS // 1000
        silence_blocks = max(1, int(a["silence_ms"]) // self.BLOCK_MS)
        max_blocks = int(a["max_seconds"]) * 1000 // self.BLOCK_MS
        wait_blocks = int(a["wait_speech_s"]) * 1000 // self.BLOCK_MS
        threshold = float(a["silence_threshold"])

        chunks: list[bytes] = []
        quiet = 0
        started = False
        with self._sd.RawInputStream(samplerate=rate, channels=1,
                                     dtype="int16",
                                     blocksize=block_frames) as stream:
            for i in range(max_blocks + wait_blocks):
                block, _overflow = stream.read(block_frames)
                block = bytes(block)
                loud = self._rms(block) >= threshold
                if not started:
                    if loud:
                        started = True
                        chunks.append(block)
                    elif i >= wait_blocks:
                        return None
                    continue
                chunks.append(block)
                quiet = 0 if loud else quiet + 1
                if quiet >= silence_blocks or len(chunks) >= max_blocks:
                    break
        if not started:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(self._amplify(b"".join(chunks), a.get("max_gain", 8)))
        return buf.getvalue()

    # Peak we aim for: loud enough for whisper, with headroom so the loudest
    # syllable is raised rather than squared off.
    TARGET_PEAK = int(0.7 * 32767)

    @classmethod
    def _amplify(cls, pcm: bytes, max_gain) -> bytes:
        """Bring a quiet utterance up to a usable level before it is sent.

        The gain has to be applied HERE and not asked of the hardware: a USB
        conference bar's ALSA capture control can have a total range of a
        fraction of a dB (measured on the kitchen device: 'Mic Capture Volume'
        at 100% is +0.39 dB), so the level it gives is the level you get —
        peaks around -23 dBFS for someone talking straight at it. And whisper
        does not fail on audio that quiet, it HALLUCINATES on it: a whole
        utterance came back as "Sottotitoli e revisione a cura di QTSS", the
        stock filler it emits for near-silence, which then reaches the agent as
        if it were something a person had said.

        Capped, and skipped when the peak is already healthy — an open gain on
        a recording that caught one door slam would lift the room's noise floor
        into something the model will happily transcribe as words.
        """
        try:
            ceiling = float(max_gain)
        except (TypeError, ValueError):
            ceiling = 1.0
        n = len(pcm) // 2
        if ceiling <= 1.0 or not n:
            return pcm
        samples = struct.unpack(f"<{n}h", pcm)
        peak = max(max(samples), -min(samples))
        if peak <= 0:
            return pcm
        gain = min(ceiling, cls.TARGET_PEAK / peak)
        if gain <= 1.05:            # already loud enough; leave the bytes alone
            return pcm
        return struct.pack(f"<{n}h", *(
            32767 if v > 32767 else -32768 if v < -32768 else v
            for v in (int(s * gain) for s in samples)))


# ----------------------------------------------------------- myagent client
def ask_myagent(cfg: dict, wav: bytes | None = None, text: str = "") -> dict:
    """POST one utterance to the connectors inbound endpoint; returns
    {"reply", "text"} or raises with a readable message.

    Audio or text — the same endpoint takes both, and the text form is what the
    device's own page uses to let someone TYPE to the agent (a voice device in a
    quiet room, or a reply to something it just announced)."""
    url = (cfg["myagent_url"].rstrip("/")
           + f"/api/connectors/inbound/{cfg['binding_id']}")
    payload = {
        "sender_name": cfg["name"],
        # What is spoken here. The device is the only party that knows, and
        # whisper transcribes measurably better when told than when guessing —
        # the server plumbs it straight into document_extract's `language`.
        "language": cfg["language"],
    }
    if wav is not None:
        payload |= {"audio_b64": base64.b64encode(wav).decode("ascii"),
                    "filename": "speech.wav"}
    else:
        payload["text"] = text
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['key']}",
    })
    try:
        with urllib.request.urlopen(req, timeout=cfg["request_timeout_s"]) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"myagent answered {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach myagent at {cfg['myagent_url']}: "
                           f"{e.reason}")


def run_turn(cfg: dict, speaker: Speaker, wav: bytes | None = None,
             text: str = "", speak: bool = True) -> dict:
    """One exchange: ask the agent, speak the answer, return both.

    The single path for all three entry points (terminal Enter, the page's
    Listen button, the page's text box), so a spoken turn and a typed one cannot
    drift apart in what they send or in what comes out of the speaker.

    ``speak=False`` is for the page's housekeeping calls (the Reset button):
    the answer to those is already on the screen of whoever pressed, and the
    connector's confirmation is a fixed English string that would be read out
    loud in a room where nobody asked anything."""
    res = ask_myagent(cfg, wav=wav, text=text)
    if speak:
        speaker.say(res.get("reply") or "")
    return res


# ------------------------------------------------------------ device server
def make_server(cfg: dict, speaker: Speaker, mic: "Microphone",
                voices: VoiceInstaller) -> ThreadingHTTPServer:
    """/say, /health, and the remote-configuration pair GET|PUT /config.

    The config endpoints exist so the tuning that can only be done by trial —
    the silence threshold against this room's noise floor, which voice, which
    language — happens in MyAgent's binding form instead of over ssh. They are a
    SECOND door onto config.json, never a replacement: the file stays the source
    of truth, hand-editable, and a device whose server is unreachable keeps
    running on what it reads there.
    """

    def _auth_ok(handler) -> bool:
        auth = handler.headers.get("Authorization", "")
        candidate = auth[7:] if auth.lower().startswith("bearer ") else ""
        key = cfg["key"]
        return bool(key) and hmac.compare_digest(candidate, key)

    def _public_config() -> dict:
        """The config as MyAgent may see it: writable fields, plus the facts the
        form needs to render. NEVER the shared key — this answer travels the
        same LAN the key protects."""
        return {
            "ok": True,
            "name": cfg["name"],
            "language": cfg["language"],
            "voice": cfg["voice"],
            "volume": cfg["volume"],
            "request_timeout_s": cfg["request_timeout_s"],
            "audio": {k: cfg["audio"].get(k) for k in WRITABLE_AUDIO},
            "device": {
                "mixer": mixer_control(),
                "binding_id": cfg["binding_id"],
                "myagent_url": cfg["myagent_url"],
                "listen_port": cfg["listen_port"],
                "config_file": str(config_path()),
                "voices": installed_voices(),
                "voices_dir": str(VOICES_DIR),
                "tts": speaker.available,
                "mic": mic.available,
                "listening": mic.listening,
                "piper": speaker.piper if Path(speaker.piper).exists() else "",
            },
            "voice_install": voices.state,
        }

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                return payload if isinstance(payload, dict) else None
            except Exception:
                return None

        def _route(self) -> str:
            """The path without its query string. `/?key=…` is the URL the
            installer prints and the kiosk opens, so a router that compares
            self.path verbatim answers 404 to its own documented link."""
            return self.path.split("?", 1)[0]

        def _html(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = self._route()
            # Health is unauthenticated on purpose: it only reveals the
            # device's display name and makes `curl /health` a usable check.
            if route == "/health":
                return self._json(200, {"ok": True, "name": cfg["name"],
                                        "language": cfg["language"]})
            # /ui.html too: the file on disk is called that, so it is the path a
            # person types after reading the folder. Cheaper to answer than to
            # explain.
            if route in ("/", "/index.html", "/ui.html"):
                # The PAGE is open, its actions are not — the same split as
                # myagent's own static UI behind the API key. Markup reveals
                # nothing; every call it makes carries the shared key, which the
                # visitor has to know (it also opens myagent's inbound route, so
                # baking it into the page would be a real downgrade).
                try:
                    return self._html(UI_FILE.read_bytes())
                except OSError as e:
                    return self._json(500, {"detail": f"no ui.html: {e}"})
            if route != "/config":
                # Name the page: a 404 from a browser is a person who guessed a
                # path, and "not found" tells them nothing about where to go.
                return self._json(404, {"detail": f"not found — the page is at "
                                                  f"http://{self.headers.get('Host', 'this-device')}/"})
            if not _auth_ok(self):
                return self._json(401, {"detail": "invalid key"})
            self._json(200, _public_config())

        def do_PUT(self):
            if self._route() != "/config":
                return self._json(404, {"detail": "not found"})
            if not _auth_ok(self):
                return self._json(401, {"detail": "invalid key"})
            payload = self._body()
            if payload is None:
                return self._json(400, {"detail": "bad JSON"})
            try:
                changed = update_config(cfg, payload)
            except ValueError as e:
                return self._json(400, {"detail": str(e)})
            except OSError as e:
                # A read-only filesystem or a bad path: the caller is a form
                # that must be able to say "not saved" rather than assume.
                return self._json(500, {"detail": f"cannot write config: {e}"})
            if "voice" in changed:
                speaker.apply(cfg)
            if "volume" in changed:
                set_volume(cfg["volume"])
            self._json(200, dict(_public_config(), changed=changed))

        def do_POST(self):
            route = self._route()
            if route not in ("/say", "/voices/install", "/ask", "/listen"):
                return self._json(404, {"detail": "not found"})
            if not _auth_ok(self):
                return self._json(401, {"detail": "invalid key"})
            payload = self._body()
            if payload is None:
                return self._json(400, {"detail": "bad JSON"})
            if route in ("/ask", "/listen"):
                # A turn, started from the page: type, or open the microphone.
                # Answering the caller with the transcript AND the reply is the
                # point — /say alone speaks into the room and leaves whoever
                # pressed the button with no idea what was heard.
                wav, text = None, ""
                if route == "/listen":
                    try:
                        wav = mic.record()
                    except RuntimeError as e:
                        # No mic, or a capture already running: both are states
                        # of the device, so 409 rather than a server error.
                        return self._json(409, {"detail": str(e)})
                    if wav is None:
                        return self._json(200, {"ok": True, "text": "",
                                                "reply": "", "heard": False})
                else:
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        return self._json(400, {"detail": "empty text"})
                try:
                    res = run_turn(cfg, speaker, wav=wav, text=text,
                                   speak=payload.get("speak") is not False)
                except RuntimeError as e:
                    # myagent unreachable or refusing: the page shows this, so
                    # it must stay the readable message ask_myagent built.
                    return self._json(502, {"detail": str(e)})
                return self._json(200, {"ok": True, "heard": True,
                                        "text": res.get("text") or "",
                                        "reply": res.get("reply") or ""})
            if route == "/voices/install":
                name = str(payload.get("name") or "").strip()

                def _adopt(installed: str) -> None:
                    """Installing a voice nobody speaks with is not what was
                    asked: unless told otherwise, the new voice becomes THE
                    voice, and it is persisted so a restart keeps it."""
                    if payload.get("use") is False:
                        return
                    update_config(cfg, {"voice": f"voices/{installed}.onnx"})
                    speaker.apply(cfg)

                state = voices.start(name, _adopt)
                code = 202 if state.get("state") == "downloading" else 409
                if state.get("state") == "error":
                    code = 400
                return self._json(code, {"ok": code == 202,
                                         "voice_install": state})
            text = str(payload.get("text") or "").strip()
            if not text:
                return self._json(400, {"detail": "empty text"})
            if payload.get("wait"):
                # The page's volume test: piper takes seconds to synthesize,
                # and holding the response until the phrase has been spoken is
                # what lets the button show a state and refuse a second click
                # (each one would queue another test phrase). Pushed messages
                # (notify_user) keep the queued default below.
                spoken = threading.Event()
                speaker.say(text, spoken)
                return self._json(200, {"ok": True, "spoken": spoken.wait(120)})
            speaker.say(text)   # queued: the response never waits on the TTS
            self._json(200, {"ok": True})

        def log_message(self, fmt, *args):  # route http.server noise to logging
            log.debug("%s " + fmt, self.client_address[0], *args)

    return ThreadingHTTPServer((cfg["listen_host"], int(cfg["listen_port"])),
                               Handler)


# --------------------------------------------------------------------- main
def push_to_talk_loop(cfg: dict, mic: Microphone, speaker: Speaker) -> None:
    print(f"— {cfg['name']} ready. Press Enter, speak, pause to send. "
          "Ctrl+C to quit.")
    while True:
        try:
            input("⏎ ")
        except EOFError:
            return
        print("● Listening…")
        try:
            wav = mic.record()
        except RuntimeError as e:
            # The page's Listen button holds the microphone (or there is none).
            print(f"⚠ {e}")
            continue
        if wav is None:
            print("(heard nothing)")
            continue
        print(f"→ sending {len(wav) // 1024} KiB…")
        try:
            res = run_turn(cfg, speaker, wav=wav)
        except RuntimeError as e:
            print(f"⚠ {e}")
            continue
        if res.get("text"):
            print(f"🗣 {res['text']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if not cfg["key"]:
        log.warning("no shared key configured: /say will refuse everything "
                    "and myagent will refuse us")
    speaker = Speaker(cfg)
    # The mixer keeps whatever the last session left it at, so the configured
    # volume has to be re-applied at every start or the file and the room
    # disagree — and the page would show a number nobody is hearing.
    if not set_volume(cfg["volume"]) and mixer_control() == "":
        log.info("no ALSA mixer found: volume is fixed outside this device")
    # Before the server: /config reports whether the mic exists, so the form
    # can explain speaker-only instead of offering thresholds that do nothing.
    mic = Microphone(cfg)
    server = make_server(cfg, speaker, mic, VoiceInstaller())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = "127.0.0.1" if cfg["listen_host"] in ("", "0.0.0.0") else cfg["listen_host"]
    log.info("page and API on http://%s:%s/  (also /say /health /config)",
             host, cfg["listen_port"])

    if mic.available and sys.stdin.isatty() and cfg["binding_id"]:
        try:
            push_to_talk_loop(cfg, mic, speaker)
        except KeyboardInterrupt:
            pass
    else:
        # No terminal push-to-talk. NOT "speaker-only" any more: the page's
        # Listen button drives the same microphone, so a systemd service — the
        # normal way this runs on a headless Pi — is fully usable from a
        # browser. Only a missing mic or binding id really takes capture away.
        why = ("no microphone" if not mic.available else
               "no TTY" if not sys.stdin.isatty() else "no binding_id")
        log.info("no terminal push-to-talk (%s) — use the page at "
                 "http://%s:%s/ to type or listen", why, host, cfg["listen_port"])
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    server.shutdown()


if __name__ == "__main__":
    main()
