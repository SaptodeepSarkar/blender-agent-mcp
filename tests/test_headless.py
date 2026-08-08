#!/usr/bin/env python3
"""Headless logic tests for the Blender Agent MCP add-on.

Run inside Blender (background is fine — this tests handler logic directly,
no sockets):
    blender --background --python tests/test_headless.py

Exits 0 on all-pass, 1 on any failure.
"""
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path

import bpy

HERE = Path(__file__).resolve().parent
ADDON = HERE.parent / "addon" / "blender_agent_addon.py"

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS {name}")
    else:
        FAILS.append(name)
        print(f"FAIL {name}  {str(detail)[:400]}")


def make_png(path, w=64, h=64, rgb=(200, 60, 60)):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        return c

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# --- load the addon module -------------------------------------------------
spec = importlib.util.spec_from_file_location("blender_agent_addon", ADDON)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(f"addon loaded from {ADDON}; blender {bpy.app.version_string}")

# --- build a test scene: 3 cubes + 3 VSE strips ----------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scn = bpy.context.scene
for i in range(3):
    bpy.ops.mesh.primitive_cube_add(size=1 + i * 0.5, location=(i * 3, 0, 0))
cubes = list(scn.objects)
cubes[0].select_set(True)

png = str(HERE / "test_clip.png")
make_png(png)
ed = scn.sequence_editor_create()
strips_coll = getattr(ed, "strips", None)
if strips_coll is None:
    strips_coll = getattr(ed, "sequences", None)
check("vse strips collection found", strips_coll is not None,
      f"strip-ish attrs: {[x for x in dir(ed) if 'strip' in x.lower() or 'seq' in x.lower()]}")

created = []
if strips_coll is not None and hasattr(strips_coll, "new_image"):
    try:
        for i in range(3):
            s = strips_coll.new_image(f"Clip{i}", png, channel=1, frame_start=1 + i * 10)
            created.append(s)
        check("strips.new_image created 3 strips", len(created) == 3)
    except Exception as exc:  # noqa: BLE001
        check("strips.new_image created 3 strips", False, repr(exc))
else:
    check("strips.new_image created 3 strips", False,
          f"strip coll attrs: {[x for x in dir(strips_coll) if not x.startswith('_')]}")

# selection + speed on 5.2: new strips default to SELECTED; speed = SPEED
# effect strips (retiming keys are op-only). Test both explicitly.
sel_ok = False
try:
    created[0].select = True
    created[1].select = False
    created[2].select = True
    sel_ok = bool(created[0].select) and not bool(created[1].select) and bool(created[2].select)
except Exception as exc:  # noqa: BLE001
    print("  (select set raised:", repr(exc), ")")
check("strip.select writable", sel_ok)

speed_ok = False
speed_strip = None
try:
    speed_strip = ed.strips.new_effect("Speed1", "SPEED", channel=2, frame_start=1, input1=created[1])
    speed_strip.speed_factor = 2.0
    speed_strip.select = False  # new strips default to selected
    speed_ok = abs(float(speed_strip.speed_factor) - 2.0) < 0.001
except Exception as exc:  # noqa: BLE001
    print("  (SPEED strip raised:", repr(exc), ")")
check("SPEED effect strip speed_factor writable", speed_ok)

# --- context: vse ----------------------------------------------------------
ctx = mod.ctx_vse()
check("ctx_vse exists", ctx.get("exists") is True)
check("ctx_vse count", ctx.get("strip_count") == 4, json.dumps(ctx)[:300])
expected_sel = sorted([created[0].name, created[2].name])
check("ctx_vse selected", sorted(ctx.get("selected", [])) == expected_sel,
      f"got {ctx.get('selected')}")
b_speed = [s for s in ctx["strips"] if s["name"] == "Speed1"]
check("ctx_vse speed_factor surfaced", bool(b_speed) and abs(b_speed[0]["speed_factor"] - 2.0) < 0.001,
      json.dumps(b_speed))
check("ctx_vse retiming_key_count field", all("retiming_key_count" in s for s in ctx["strips"]))
for s in ctx["strips"]:
    check(f"ctx_vse fields {s['name']}",
          all(k in s for k in ("type", "channel", "frame_start",
                               "frame_final_start", "frame_final_end",
                               "frame_final_duration", "speed_factor",
                               "retiming_key_count", "selected", "mute")))

# --- context: objects ------------------------------------------------------
for o in scn.objects:
    o.select_set(False)
cubes[0].select_set(True)
ctxo = mod.ctx_objects()
check("ctx_objects count", ctxo.get("object_count") == 3, json.dumps(ctxo)[:300])
check("ctx_objects selected", ctxo.get("selected") == [cubes[0].name], str(ctxo.get("selected")))

# --- process_request handler -----------------------------------------------
r = mod.process_request({"id": 1, "type": "ping"})
check("ping ok", r.get("ok") is True and bool(r.get("info", {}).get("version")))

r = mod.process_request({"id": 2, "type": "context", "domain": "vse"})
check("handler context vse", r.get("ok") is True and r["context"]["strip_count"] == 4)

r = mod.process_request({"id": 3, "type": "context", "domain": "bogus"})
check("handler bad domain", r.get("ok") is False and "unknown domain" in r.get("error", ""))

r = mod.process_request({"id": 4, "type": "exec", "code": "raise RuntimeError('boom')"})
check("exec error captured", r.get("ok") is False and "boom" in r.get("error", ""))
check("exec error has before/after", "before" in r and "after" in r and "changed" in r)

r = mod.process_request({"id": 5, "type": "exec",
                         "code": "import bpy\n"
                                 "bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=2, location=(0, 0, 2))\n"
                                 "__result__ = bpy.context.object.name"})
check("exec success", r.get("ok") is True and isinstance(r.get("result"), str))
check("exec diff detects objects", any("objects:" in c for c in r.get("changed", [])),
      json.dumps(r.get("changed")))
check("exec result serialized", str(r.get("result", "")).startswith("Cylinder"))

r = mod.process_request({"id": 6, "type": "exec",
                         "code": "print('hello from script')\n__result__ = 42",
                         "checkpoint": False})
check("exec stdout captured", "hello from script" in r.get("stdout", ""))
check("exec result value", r.get("result") == 42)
check("exec checkpoint flag present", "undo_checkpoint" in r and "undo_checkpoint_error" in r)

r = mod.process_request({"id": 7, "type": "exec", "code": "pass"})
check("checkpoint pushed in bg", r.get("undo_checkpoint") is True
      and r.get("undo_checkpoint_error") is None)

r = mod.process_request({"id": 8, "type": "eval", "code": "[o.name for o in bpy.data.objects]"})
check("eval mode", r.get("ok") is True and isinstance(r.get("result"), list))

r = mod.process_request({"id": 9, "type": "undo"})
check("undo works (bg)", r.get("ok") is True and r.get("result") == "undone", json.dumps(r)[:200])

r = mod.process_request({"id": 10, "type": "redo"})
check("redo works (bg)", r.get("ok") is True and r.get("result") == "redone", json.dumps(r)[:200])

r = mod.process_request({"id": 11, "type": "context", "domain": "auto"})
check("ctx auto in background -> scene", r.get("ok") is True and r["context"].get("_source") == "scene")

r = mod.process_request({"id": 12, "type": "shutdown"})
check("shutdown ok", r.get("ok") is True)

# --- json_safe -------------------------------------------------------------
import mathutils  # noqa: E402
check("json_safe vector", mod.json_safe(mathutils.Vector((1, 2, 3))) == [1.0, 2.0, 3.0])
check("json_safe float nan", mod.json_safe(float("nan")) is None)

print()
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURES -> {FAILS}")
    sys.exit(1)
print("RESULT: ALL PASS")
sys.exit(0)
