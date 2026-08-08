#!/usr/bin/env python3
"""E2E driver: exercises the full stack (socket + auth + handlers) against a
background Blender running the add-on as a service.

Start the service first:
    blender --background --python addon/blender_agent_addon.py
Then run (with the project venv, which has the mcp package):
    .venv/bin/python tests/e2e_client.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from mcp_server import Bridge  # noqa: E402

b = Bridge()
ok = True


def step(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f"  {str(detail)[:300]}" if detail else ""))
    if not cond:
        ok = False


r = b.call({"type": "ping"}, timeout=15)
step("ping", r.get("ok") is True and bool(r.get("info", {}).get("version")), r)

r = b.call({"type": "context", "domain": "vse"}, timeout=30)
step("context vse", r.get("ok") is True and "strip_count" in r.get("context", {}), r)

r = b.call({"type": "exec", "code": "import math\nx = 1/0"}, timeout=30)
step("error script reported", r.get("ok") is False and "ZeroDivisionError" in r.get("error", ""), r.get("error"))

r = b.call({"type": "exec",
            "code": "import bpy\nbpy.ops.mesh.primitive_cube_add(size=2)\n__result__ = bpy.context.object.name"},
           timeout=30)
step("fix script ok", r.get("ok") is True and isinstance(r.get("result"), str), r)
step("diff shows change", any("objects:" in c for c in r.get("changed", [])), r.get("changed"))

r = b.call({"type": "undo"}, timeout=30)
step("undo works", r.get("ok") is True and r.get("result") == "undone", r)

r = b.call({"type": "context", "domain": "objects"}, timeout=30)
step("verify context", r.get("ok") is True and r["context"]["object_count"] >= 1, r)

r = b.call({"type": "shutdown"}, timeout=15)
step("shutdown", r.get("ok") is True, r)

print()
print("E2E RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
