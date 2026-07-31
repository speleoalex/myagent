# MyAgent voice satellite

A small speaker/microphone client for a PC or Raspberry Pi. Press Enter,
speak, pause — the audio goes to your MyAgent server (transcribed there, no ML
on the device), the bound agent answers, and the reply is spoken back with
[Piper](https://github.com/rhasspy/piper). The device also exposes a tiny HTTP
endpoint (`/say`) so agents can *send* it messages: add it to the address book
and `notify_user` can make the kitchen speaker announce a reminder.

```
[device]  mic → WAV ──────────▶  POST /api/connectors/inbound/<binding>   [myagent]
          speaker ◀─ reply ────  (same HTTP response)
          POST /say ◀──────────  notify_user ("tell Cucina …")
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

## Run

```bash
.venv/bin/python satellite.py                        # push-to-talk + speaker
systemctl --user enable --now myagent-satellite      # speaker-only service
sudo loginctl enable-linger $USER                    # keep it up after logout (headless Pi)
```

Without a terminal (service mode) there is no push-to-talk: the device still
speaks whatever agents send to `/say`. Run it interactively to talk.

## Check it works

```bash
curl http://<device-ip>:8899/health                      # → {"ok": true, "name": "Cucina"}
arecord -l                                               # the mic is visible to ALSA?
echo ciao | piper --model voices/*.onnx --output_raw \
  | aplay -r 22050 -f S16_LE -t raw                      # the voice plays?
```

From MyAgent, the binding's **Test** button probes `/health`; `notify_user`
to the device's contact makes it speak.

## Troubleshooting

- **401 from MyAgent** — the `key` in `config.json` and the binding's token
  differ, or the binding id is wrong (both read the same from outside).
- **503 from MyAgent** — the binding is disabled, or connectors are stopped.
- **`transcription failed`** — first use downloads a Whisper model on the
  server (~464 MB); check the server's journal, or set a smaller
  `MYAGENT_WHISPER_MODEL`.
- **No sound** — check `aplay -l`, and that the unit's user session owns the
  audio device (a user unit does; a system unit would not).
- **Replies are printed but not spoken** — `piper` or the voice model is
  missing; rerun `install.sh` or set `piper_voice` in `config.json`.
