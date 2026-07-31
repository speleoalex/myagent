#!/usr/bin/env python3
"""MyAgent voice satellite — a small speaker/microphone client for a PC or
Raspberry Pi.

Push-to-talk: press Enter, speak, pause — the recorded audio goes to MyAgent's
connectors inbound endpoint (transcribed server-side, answered by the agent
bound to this device) and the reply is spoken back through Piper. A tiny HTTP
server (/say, /health) lets MyAgent push unsolicited messages — reminders,
alarms — that the device speaks too; that is what makes the satellite an
address-book entry agents can notify.

Runs in two modes, decided by the environment:
- interactive terminal → full push-to-talk loop + /say server
- no TTY (systemd service) or no microphone → speaker-only: /say still works,
  so the device keeps announcing what the agents send it.

Dependencies: sounddevice (microphone; optional — speaker-only without it) and
the `piper` CLI with a voice model (TTS; without it, texts are printed only).
Everything else is the standard library.
"""
from __future__ import annotations

import base64
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
    "piper_voice": "",          # path to a .onnx voice; empty = print-only
    "request_timeout_s": 180,    # a local model can take minutes on a long turn
    "audio": {
        "samplerate": 16000,
        "silence_threshold": 500,   # RMS floor that counts as speech (int16)
        "silence_ms": 700,          # this much silence closes the utterance
        "max_seconds": 30,
        "wait_speech_s": 10,        # give up if nothing is said after Enter
    },
}


def load_config() -> dict:
    """config.json next to this file (path override: MYAGENT_SAT_CONFIG),
    merged over the defaults so a partial file is fine."""
    path = Path(os.environ.get("MYAGENT_SAT_CONFIG") or BASE_DIR / "config.json")
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
    return cfg


# --------------------------------------------------------------------- TTS
class Speaker:
    """Serialized text-to-speech: one queue, one worker, so a /say arriving
    mid-reply queues up instead of speaking over it."""

    # Drop symbols/emoji before TTS (a model reply may carry ⏳ or ✅); keep
    # letters of any alphabet, digits and plain punctuation.
    _UNSPEAKABLE = re.compile(r"[℀-\U0010FFFF]")

    def __init__(self, cfg: dict):
        self.voice = str(cfg.get("piper_voice") or "")
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

    def _voice_rate(self) -> int:
        """Piper ships a <voice>.onnx.json with the audio sample rate."""
        try:
            meta = json.loads(Path(self.voice + ".json").read_text())
            return int(meta["audio"]["sample_rate"])
        except Exception:
            return 22050

    def say(self, text: str) -> None:
        text = self._UNSPEAKABLE.sub(" ", text or "").strip()
        if text:
            self._q.put(text)

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            # flush: under systemd stdout is a pipe (block-buffered) and the
            # journal would otherwise show announcements minutes late.
            print(f"🔊 {text}", flush=True)
            if not self.available:
                continue
            try:
                self._speak(text)
            except Exception as e:
                log.warning("TTS failed: %s", e)

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


# ------------------------------------------------------------- microphone
class Microphone:
    """Push-to-talk capture with RMS endpointing, no numpy: sounddevice's
    RawInputStream hands over int16 bytes and 30 ms blocks are small enough
    to sum in pure Python."""

    BLOCK_MS = 30

    def __init__(self, cfg: dict):
        self.cfg = cfg["audio"]
        try:
            import sounddevice  # noqa: F401 — probe only
            self._sd = sounddevice
            self.available = True
        except Exception as e:
            self._sd = None
            self.available = False
            log.warning("microphone unavailable (%s): speaker-only mode", e)

    @staticmethod
    def _rms(block: bytes) -> float:
        n = len(block) // 2
        if not n:
            return 0.0
        samples = struct.unpack(f"<{n}h", block)
        return (sum(s * s for s in samples) / n) ** 0.5

    def record(self) -> bytes | None:
        """One utterance as WAV bytes: wait for speech, stop on silence.
        None when nothing was said."""
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
            w.writeframes(b"".join(chunks))
        return buf.getvalue()


# ----------------------------------------------------------- myagent client
def ask_myagent(cfg: dict, wav: bytes) -> dict:
    """POST the utterance to the connectors inbound endpoint; returns
    {"reply", "text"} or raises with a readable message."""
    url = (cfg["myagent_url"].rstrip("/")
           + f"/api/connectors/inbound/{cfg['binding_id']}")
    body = json.dumps({
        "audio_b64": base64.b64encode(wav).decode("ascii"),
        "filename": "speech.wav",
        "sender_name": cfg["name"],
    }).encode("utf-8")
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


# -------------------------------------------------------------- /say server
def make_server(cfg: dict, speaker: Speaker) -> ThreadingHTTPServer:
    key = cfg["key"]
    name = cfg["name"]

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # Health is unauthenticated on purpose: it only reveals the
            # device's display name and makes `curl /health` a usable check.
            if self.path == "/health":
                self._json(200, {"ok": True, "name": name})
            else:
                self._json(404, {"detail": "not found"})

        def do_POST(self):
            if self.path != "/say":
                return self._json(404, {"detail": "not found"})
            auth = self.headers.get("Authorization", "")
            candidate = auth[7:] if auth.lower().startswith("bearer ") else ""
            import hmac
            if not key or not hmac.compare_digest(candidate, key):
                return self._json(401, {"detail": "invalid key"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                text = str(payload.get("text") or "").strip()
            except Exception:
                return self._json(400, {"detail": "bad JSON"})
            if not text:
                return self._json(400, {"detail": "empty text"})
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
        wav = mic.record()
        if wav is None:
            print("(heard nothing)")
            continue
        print(f"→ sending {len(wav) // 1024} KiB…")
        try:
            res = ask_myagent(cfg, wav)
        except RuntimeError as e:
            print(f"⚠ {e}")
            continue
        if res.get("text"):
            print(f"🗣 {res['text']}")
        speaker.say(res.get("reply") or "")


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    if not cfg["key"]:
        log.warning("no shared key configured: /say will refuse everything "
                    "and myagent will refuse us")
    speaker = Speaker(cfg)
    server = make_server(cfg, speaker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("listening on http://%s:%s (/say, /health)",
             cfg["listen_host"], cfg["listen_port"])

    mic = Microphone(cfg)
    if mic.available and sys.stdin.isatty() and cfg["binding_id"]:
        try:
            push_to_talk_loop(cfg, mic, speaker)
        except KeyboardInterrupt:
            pass
    else:
        # Speaker-only: as a service there is no TTY for push-to-talk, and
        # without a binding id there is nobody to send speech to — but /say
        # keeps working, which is the half notify_user needs.
        why = ("no microphone" if not mic.available else
               "no TTY" if not sys.stdin.isatty() else "no binding_id")
        log.info("speaker-only mode (%s): /say stays available", why)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    server.shutdown()


if __name__ == "__main__":
    main()
