# Image tools on a Jetson Orin Nano 8 GB (or any box too small for both models)

Kept apart from `server/` on purpose: nothing in here is part of the app.
`install.sh` at the repo root skips `contrib/`, so a deployment never ships it
and `update.sh` never touches what this folder installs.

**The problem.** On the Orin Nano Super 8 GB the chat model (`llama-server`,
Qwen3-VL-4B at 16k context) and Stable Diffusion (`sd-server`, SD-Turbo) do
not fit in memory together: sd-server peaks at ~4.4 GB, and with llama-server
loaded ~1.4 GB are free. No context size or quantization closes that gap.

**The arrangement.** sd-server stays a *disabled* user unit. In the user tool
layer, `generate_image` and `edit_image` are overridden by one wrapper
(`sd-arbitrage-run`) that stops llama-server, starts sd-server, waits for it,
runs the **bundled** tool unchanged, then restores llama-server and waits for
its `/health` before answering. A detached guardian repeats the restore if the
wrapper is killed. Parameters are clamped to what SD-Turbo does well here
(≤ 768 px, ≤ 8 steps). Measured: ~34 s end to end for a 768×768 picture at 8
steps, of which ~22 s is the generation itself; the chat is dark for that long.

Because the override has the same id as the bundled tool, agents and the
`images/*` group grant see nothing different; the Tools page shows the two
tools as *modified*.

## Files

| File | Goes to | Purpose |
|---|---|---|
| `sd-arbitrage-run` | `~/myagent/tools/{generate_image,edit_image}/run` | the wrapper; finds the tool id from its folder and the bundled script under `$MYAGENT_APP_DIR/tools` |
| `install.sh` | — | copies the wrapper and derives each `tool.json` from the bundled one plus a sentence about SD-Turbo's limits |
| `systemd/sd-server.service` | `~/.config/systemd/user/` | example unit, localhost only, `Conflicts=llama-server.service`, **not enabled** |

## Install

Prerequisites on the board: MyAgent installed per-user (`./install.sh` as the
user → `~/myagent/bin`), `llama-server` and `sd-server` as `systemctl --user`
units named exactly so, stable-diffusion.cpp built with `sd-server`, and an
image model registered in MyAgent (kind *Image*, provider `a1111`, base URL
`http://127.0.0.1:7860`) and chosen in Settings → *Image generation*.

```bash
cd ~/src/myagent && ./update.sh            # bundled image tools present
contrib/jetson-orin-8gb/install.sh         # writes the two overrides
systemctl --user disable --now sd-server   # must NOT run on its own
```

Re-run `install.sh` after a MyAgent update if the bundled `tool.json` changed.
To go back to the plain tools: `rm -r ~/myagent/tools/{generate_image,edit_image}`.

## Tuning

Constants at the top of `sd-arbitrage-run`: unit names, readiness URLs
(`/sdapi/v1/options` for sd-server — its `/health` is 404), clamps and
timeouts. Keep `TOOL_TIMEOUT` below the tool.json `timeout` (600) so a stuck
backend ends with a sentence, not a kill.
