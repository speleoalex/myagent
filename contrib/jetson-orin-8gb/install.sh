#!/bin/bash
# Install the SD arbitrage wrappers into the USER tool layer of a per-user
# MyAgent install (~/myagent/tools). Run as the user that runs MyAgent, from
# anywhere; re-run after a MyAgent update to refresh the tool.json copies.
#
# For each image tool it writes ~/myagent/tools/<tool>/run (the wrapper) and
# tool.json = the bundled one with a sentence about SD-Turbo appended to the
# description, so the model asks for sizes this board can deliver. The bundled
# tools are left untouched; removing the two folders restores them.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd -P)"
MYAGENT_HOME="${MYAGENT_HOME:-$HOME/myagent}"
BUNDLED="$MYAGENT_HOME/bin/server/tools"
USER_TOOLS="${MYAGENT_TOOLS:-$MYAGENT_HOME/tools}"
NOTE=" ON THIS MACHINE the backend is SD-Turbo and shares the memory with the chat model: the call takes about a minute, only ONE image per turn is possible and the chat is unavailable meanwhile. Leave width/height/steps unset (defaults 512x512, 4 steps) or stay within 768 px and 8 steps - larger values are clamped anyway."

[ -d "$BUNDLED" ] || { echo "bundled tools not found at $BUNDLED (is MyAgent installed per-user?)" >&2; exit 1; }

for tool in generate_image edit_image; do
    src="$(find "$BUNDLED" -maxdepth 3 -type f -path "*/$tool/tool.json" | head -n1)"
    [ -n "$src" ] || { echo "bundled $tool not found under $BUNDLED — update MyAgent first" >&2; exit 1; }
    dst="$USER_TOOLS/$tool"
    mkdir -p "$dst"
    NOTE="$NOTE" python3 - "$src" "$dst/tool.json" <<'PY'
import json, os, sys
src, dst = sys.argv[1:3]
d = json.load(open(src))
note = os.environ["NOTE"]
if note.strip() not in d["description"]:
    d["description"] = d["description"].rstrip() + note
d["timeout"] = max(int(d.get("timeout", 30)), 600)
json.dump(d, open(dst, "w"), indent=2, ensure_ascii=False)
PY
    install -m 755 "$HERE/sd-arbitrage-run" "$dst/run"
    echo "installed $dst (over $(dirname "$src"))"
done

echo
echo "Units expected: systemctl --user {llama-server,sd-server}; see systemd/sd-server.service."
echo "Check: curl -s http://127.0.0.1:8888/api/tools/generate_image | grep -o '\"modified\": *true'"
