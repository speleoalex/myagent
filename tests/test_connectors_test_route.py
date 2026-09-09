#!/usr/bin/env python3
"""The test route forwards `check` only when asked — and only to verify().

`POST /api/connectors/bindings/test` (and `/{id}/test`) drive one connector's
verify(). Since the mail channel split its check into per-section tests, the
route takes a `check` id. Two contracts are pinned here, both invisible when
they break:

  * a channel with the OLD parameterless verify() must keep working from the
    generic Test button: the router passes the kwarg only when `check` is set,
    otherwise every telegram/satellite test would die with a TypeError;
  * a channel with the NEW signature must receive the id verbatim, and an id
    it does not know must come back as a 400 carrying its own message — a bug
    in the form, not "Invalid credentials".

No channel type is named: the connector factory is faked, as are the services.
State root is a temporary MYAGENT_CONNECTORS_DIR, set before the import.

Run:  server/.venv/bin/python tests/test_connectors_test_route.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(tempfile.mkdtemp(prefix="myagent-testroute-"))
os.environ["MYAGENT_CONNECTORS_DIR"] = str(STATE)

sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "connectors" / "plugin"))

from fastapi import HTTPException  # noqa: E402

from myagent_connectors.models import Binding  # noqa: E402
from myagent_connectors.routers import bindings  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


class OldStyle:
    """A connector written before `check` existed."""
    calls: list = []

    async def verify(self):
        OldStyle.calls.append("called")
        return {"bot": "oldbot", "name": "Old"}


class NewStyle:
    received: list = []

    async def verify(self, check=None):
        NewStyle.received.append(check)
        if check and check not in ("receive", "send"):
            raise ValueError(f"unknown check {check!r} (expected one of: receive, send)")
        return {"account": "x@y.z", "check": check or "", "detail": "fine"}


class FakeServices:
    core = None
    grants = None


async def main():
    print("_verify() and the `check` argument:")
    orig = (bindings.services, bindings.create_connector)
    bindings.services = lambda request: FakeServices()
    b = Binding(id="probe", type="whatever", token="t")
    try:
        bindings.create_connector = lambda binding, core, grants: OldStyle()
        res = await bindings._verify(b, None)
        check(res == {"ok": True, "bot": "oldbot", "name": "Old"} and OldStyle.calls == ["called"],
              f"old-style verify() gets no kwarg and its result is passed through: {res}")
        try:
            await bindings._verify(b, None, "send")
            check(False, "old-style verify() with a check")
        except HTTPException as e:
            check(e.status_code == 400, f"a check on a channel without tests is a 400, not a crash: {e.detail}")

        bindings.create_connector = lambda binding, core, grants: NewStyle()
        res = await bindings._verify(b, None)
        check(res.get("check") == "" and NewStyle.received == [None],
              f"no check → verify() called without kwarg: {res}")
        res = await bindings._verify(b, None, "send")
        check(res.get("check") == "send" and NewStyle.received[-1] == "send",
              f"check='send' reaches verify() verbatim: {res}")
        try:
            await bindings._verify(b, None, "bogus")
            check(False, "unknown check id raises")
        except HTTPException as e:
            check(e.status_code == 400 and "bogus" in e.detail and "Invalid credentials" not in e.detail,
                  f"unknown check → 400 with the connector's own message: {e.detail}")

        # The request models: `check` defaults to "" on both routes, so the
        # form's old payloads (no `check`) still validate.
        check(bindings.TestReq(type="x", token="t").check == "", "TestReq.check defaults to ''")
        check(bindings.CheckReq().check == "", "CheckReq.check defaults to ''")
    finally:
        bindings.services, bindings.create_connector = orig


try:
    asyncio.run(main())
finally:
    shutil.rmtree(STATE, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} FAILED:\n  - " + "\n  - ".join(failures))
    sys.exit(1)
print("\nall ok")
