#!/usr/bin/env python3
"""`ask_myagent()` — every way a turn can fail must come out as RuntimeError.

Run: python3 tests/test_satellite_ask_errors.py   (stdlib only, no device and no
myagent needed: a throwaway HTTP server on 127.0.0.1 plays the server's part.)

The callers — the page's /ask and /listen, and push-to-talk — catch RuntimeError
and nothing else. Anything else escaped the page handler, which dropped the
socket without an answer, and the browser showed only "Failed to fetch". That is
how a turn of 189 s against a timeout of 180 s looked on 2026-10-22: urlopen
raises a bare TimeoutError while waiting for the reply, not a URLError.
"""

import importlib.util
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("satellite", ROOT / "satellite" / "satellite.py")
sat = importlib.util.module_from_spec(spec)
sys.modules["satellite"] = sat
spec.loader.exec_module(sat)


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = self.path.rsplit("/", 1)[-1]
        if mode == "slow":                       # longer than request_timeout_s
            time.sleep(3)
            return
        if mode == "drop":                       # closes before any status line
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if mode == "garbage":                    # 200, but not JSON
            body = b"<html>proxy error</html>"
        elif mode == "refused":                  # myagent's own error shape
            self.send_response(401)
            body = json.dumps({"detail": "bad token"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        else:
            body = json.dumps({"reply": "ciao", "text": "ciao"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}"

# A port nobody listens on: bind one, read its number, close it.
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    dead = f"http://127.0.0.1:{s.getsockname()[1]}"


def cfg(binding, base=url):
    return {"myagent_url": base, "binding_id": binding, "name": "test",
            "language": "it", "key": "k", "request_timeout_s": 1}


# (label, cfg, text the RuntimeError must contain)
FAILING = [
    ("timeout waiting for the reply", cfg("slow"), "did not answer within 1 s"),
    ("connection dropped mid-request", cfg("drop"), "lost the connection"),
    ("reply is not JSON", cfg("garbage"), "not JSON"),
    ("HTTP error with detail", cfg("refused"), "401: bad token"),
    ("nothing listening", cfg("x", dead), "cannot reach myagent"),
]

failures = []

for label, c, want in FAILING:
    try:
        got = sat.ask_myagent(c, text="che ore sono")
        failures.append(f"[{label}] returned {got!r}, expected RuntimeError")
    except RuntimeError as e:
        if want not in str(e):
            failures.append(f"[{label}] message {str(e)!r} lacks {want!r}")
    except BaseException as e:
        failures.append(f"[{label}] escaped as {type(e).__name__}: {e!r}")

got = sat.ask_myagent(cfg("ok"), text="ciao")
if got != {"reply": "ciao", "text": "ciao"}:
    failures.append(f"[happy path] got {got!r}")

server.shutdown()
if failures:
    print(f"FAIL — {len(failures)} case(s)\n")
    print("\n".join(failures))
    sys.exit(1)
print(f"OK — {len(FAILING)} failures turned into RuntimeError, 1 happy path")
