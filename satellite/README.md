# MyAgent voice satellite

A small speaker/microphone client for a PC or Raspberry Pi. Speak to it and the
audio goes to your MyAgent server (transcribed there, no ML on the device), the
bound agent answers, and the reply is spoken back with
[Piper](https://github.com/rhasspy/piper). The device also exposes a tiny HTTP
endpoint (`/say`) so agents can *send* it messages: add it to the address book
and `notify_user` can make the kitchen speaker announce a reminder.

It serves **its own page** at `http://<device>:8899/` — type to the agent, press
**Talk** to open the microphone, tune the settings. No app, no terminal: that
page is how a headless device is actually used.

![The satellite's page: a Talk button, the conversation, a text box](../docs/images/satellite.png)

```text
[device]  mic → WAV ──────────▶  POST /api/connectors/inbound/<binding>   [myagent]
          speaker ◀─ reply ────  (same HTTP response)
          POST /say ◀──────────  notify_user ("tell Cucina …")

[browser] ─ Talk / type ──────▶  http://<device>:8899/   (the device's own page)
```

Requires the **connectors plugin** on the MyAgent side (`connectors/install.sh`),
which ships the `satellite` channel type.

## Install (on the device)

```bash
scp -r satellite/ user@device:myagent-satellite
ssh user@device
cd myagent-satellite
bash install.sh --name Cucina            # --voice it_IT-paola-medium by default
```

The installer creates a local venv, downloads the Piper voice, generates
`config.json` with a random shared key, installs a systemd **user** unit, and
prints exactly what to paste into MyAgent. System packages you may need:

```bash
sudo apt install libportaudio2 alsa-utils   # mic capture + playback
```

## Pairing with MyAgent

1. Edit `config.json`: set `myagent_url` to your server.
2. In the MyAgent UI, **Connectors → New bot**: type *Satellite*, id =
   `binding_id` from the config, *Device URL* = `http://<device-ip>:8899`,
   token = the shared key, and pick the agent that answers.
3. Optional: create an address-book contact whose *satellite* handle is the
   binding id — agents can then notify the device by name.

One key, both directions: the device presents it to MyAgent's inbound
endpoint, MyAgent presents it to the device's `/say`.

## The device's page

```text
http://<device-ip>:8899/
```

Two screens, one action each — it is designed for a small touch panel with no
keyboard in the room (800x480 on a Pi), not for a desktop window:

- **Talk** — the big button — opens the microphone ON THE DEVICE (not the
  browser's), records until you stop speaking, and speaks the answer. This is
  the replacement for pressing Enter in a terminal, and it works from a phone.
- The **text box** sends a typed message through the same path — useful in a
  quiet room, or to answer something the device just announced.
- **Reset** clears the conversation. It sends the connector's built-in
  `/reset`, the same command a Telegram user types, so the history the agent
  answers from is really gone and not just wiped off the screen. It asks twice:
  a panel at elbow height gets touched by accident.
- The **gear** opens the settings on their own screen: **volume** (with a
  *Test* button, because volume is tuned by ear), spoken language, voice, voice
  install and the microphone thresholds — the same values as the MyAgent form,
  since both write this device's `config.json`.

The page is shown in the device's own `language`, not the browser's: on the
kiosk screen there is no browser locale anyone can set.

The page itself is public (it is only markup); every action carries the shared
key, which you paste once and the browser keeps. `install.sh` prints a
`?key=…` link that fills it in for you — the page then strips it from the
address bar. Do not hand that link to anyone you would not give the key to: it
also opens MyAgent's inbound endpoint for this binding.

One capture at a time: if the terminal loop is recording, the button answers
"already listening", and the other way round.

## Configuration

`config.json` next to `satellite.py` is the only configuration, and it is a
plain file you can edit with any editor (`MYAGENT_SATELLITE_CONFIG` moves it). It is
read at startup, so restart after editing by hand.

| key | what it does |
| --- | --- |
| `name` | how the device introduces itself, and the sender the agent sees |
| `language` | what is SPOKEN here (`it`, `en`, …). Sent with the audio so the server transcribes in that language instead of guessing; empty = auto-detect |
| `voice` | path to the Piper `.onnx` that speaks the replies; empty = replies are printed only |
| `volume` | playback volume 0-100, applied to the ALSA mixer at startup and whenever it changes. A device with no mixer reports that and the slider disappears — set the volume on the speaker instead. Setting it also lifts back any card playback control PulseAudio has parked at zero (see *A silent speaker* below) |
| `myagent_url`, `binding_id`, `key` | pairing: which server, which binding, the shared key |
| `listen_host`, `listen_port` | where `/say`, `/health` and `/config` answer |
| `request_timeout_s` | how long to wait for the agent's reply (a local model can take minutes) |
| `audio.silence_threshold` | RMS level that counts as speech. **The one value that needs tuning against the room** — too low and the mic never stops, too high and it never starts. For scale: a USB speaker bar with someone talking at it reads ~25 idle and peaks around 1000-2000 on speech |
| `audio.silence_ms` | how much silence closes an utterance |
| `audio.max_seconds`, `audio.wait_speech_s` | caps: recording length, and how long to wait for speech after Enter |
| `audio.max_gain` | ceiling for the auto-gain applied to a captured utterance before it is sent (`1` = off). A quiet microphone does not make whisper fail, it makes it *hallucinate* — it answers near-silence with stock filler like "Sottotitoli e revisione a cura di QTSS", which then reaches the agent as if a person had said it |

### From MyAgent, without ssh

The tuning fields are also editable from the binding's page in MyAgent
(**Connectors → the device → *Device settings***), which reads and writes this
same file — a second door onto it, not a replacement. Language, voice and the
audio thresholds apply **immediately**, without a restart. Installing a voice
from that box downloads it here into `voices/` and starts using it.

What MyAgent may NOT write is the pairing: `key`, `binding_id`, `myagent_url`,
`listen_host`, `listen_port`. Those are set on the device, once, by
`install.sh` — a remote call able to repoint the device at another server, or
move the port it is reached on, would cut the only wire it could be fixed over.

### Installing another voice by hand

```bash
V=it_IT-paola-medium; L=${V%%-*}; L2=${L%%_*}; S=${V#*-}
curl -fsSLO --output-dir voices \
  "https://huggingface.co/rhasspy/piper-voices/resolve/main/$L2/$L/${S%%-*}/${S#*-}/$V.onnx"{,.json}
# then point "voice" at voices/$V.onnx (or pick it in MyAgent)
```

## Run

```bash
.venv/bin/python satellite.py                        # push-to-talk + speaker
systemctl --user enable --now myagent-satellite      # page + speaker + microphone
sudo loginctl enable-linger $USER                    # keep it up after logout (headless Pi)
```

Without a terminal there is no Enter to press, but the microphone is **not**
lost: use **Talk** on the device's page. That is why running it as a service is
the normal setup, headless Pi included.

### Kiosk: the page on the device's own screen

A device WITH a display should show the agent, not a desktop:

```bash
bash kiosk.sh --install     # add it to the desktop session's autostart
bash kiosk.sh               # try it now (needs a running X session)
bash kiosk.sh --uninstall   # remove the autostart entry
```

`kiosk.sh` waits for `/health`, stops the screen from blanking and opens
Chromium fullscreen on `http://127.0.0.1:<port>/?key=…`, so the page comes up
already unlocked with nothing to type. The key is read from `config.json` at
launch — never copied into the script — and the browser keeps its own profile
(`~/.config/myagent-kiosk`), so clearing it does not touch your normal
browsing. It ends up in that browser's argv, visible to `ps` on the device:
the same trust boundary as `config.json` itself, and it never leaves localhost.

Requires a desktop session (Raspberry Pi OS with desktop, autologin on) and
`chromium-browser`. Undo the screen tweak with `xset s on +dpms` if you would
rather the panel blanks.

## Check it works

```bash
curl http://<device-ip>:8899/health                      # → {"ok": true, "name": "Cucina"}
arecord -l                                               # the mic is visible to ALSA?
echo ciao | piper --model voices/*.onnx --output_raw \
  | aplay -r 22050 -f S16_LE -t raw                      # the voice plays?
```

From MyAgent, the binding's **Test** button probes `/health`; `notify_user`
to the device's contact makes it speak. From a browser, open
`http://<device-ip>:8899/` and press **Talk**.

## Troubleshooting

- **401 from MyAgent** — the `key` in `config.json` and the binding's token
  differ, or the binding id is wrong (both read the same from outside).
- **503 from MyAgent** — the binding is disabled, or connectors are stopped.
- **`transcription failed`** — first use downloads a Whisper model on the
  server (~464 MB); check the server's journal, or set a smaller
  `MYAGENT_WHISPER_MODEL`.
- **No sound** — check `aplay -l`, and that the unit's user session owns the
  audio device (a user unit does; a system unit would not).
- **A silent speaker, though the volume looks right** — compare the sink volume
  with the card's own control: `pactl list sinks | grep -m1 Volume` against
  `amixer -c <card> sget PCM`. When a card advertises a useless hardware range
  (a USB speaker bar declaring its whole `PCM Playback Volume` as 0.00–0.39 dB)
  PulseAudio takes the volume into software and parks that element at its
  minimum — which on such a card is not a whisper, it is silence. The satellite
  lifts it back on every volume change and at startup, and says so in the
  journal (`mixer 1:PCM was parked at 0% — restoring`).
- **Talk does nothing / "nothing was heard"** — `silence_threshold` is above
  what this microphone gives. Read the actual levels before guessing:

  ```bash
  .venv/bin/python - <<'EOF'
  import sounddevice as sd, struct
  r, n = 16000, 16000*30//1000
  with sd.RawInputStream(samplerate=r, channels=1, dtype="int16", blocksize=n) as s:
      v = []
      for _ in range(200):          # ~6 s: stay quiet, then talk
          b = bytes(s.read(n)[0]); m = len(b)//2
          v.append((sum(x*x for x in struct.unpack(f"<{m}h", b))/m)**0.5)
  v.sort(); print("idle ~%.0f  loud ~%.0f  peak %.0f" % (v[len(v)//2], v[int(len(v)*.99)], v[-1]))
  EOF
  ```

  Put the threshold between the two.
- **Replies are printed but not spoken** — `piper` or the voice model is
  missing; rerun `install.sh`, set `voice` in `config.json`, or install one
  from the device box in MyAgent.
