#!/usr/bin/env python3
"""Blender Agent MCP — Blender-side service add-on.

This add-on turns YOUR open Blender into a live agent workspace. The moment
Blender starts (and the add-on is enabled), it opens a localhost socket
service. An external MCP server (server/mcp_server.py) connects to it so an
agent can:

  * read rich context about whatever is in the scene — VSE strips, objects,
    compositor nodes, animation data, materials, grease pencil, UI layout
  * execute bpy scripts inside THIS live Blender session (state persists
    across calls, same scene, same undo stack)
  * undo / redo (Ctrl+Z / Ctrl+Shift+Z) so a failed script can be fully
    cleaned up before retrying

The agent's workflow loop: get_context() -> run_script() -> on error undo()
-> fix script -> run again -> get_context() to verify the result.

Protocol: newline-delimited JSON over TCP 127.0.0.1:<port> (default 9877,
falls back to higher ports if busy). First message must be auth with the
token written to the info file (see INFO_PATH).

Install:
  Edit > Preferences > Add-ons > Install...  pick this file, enable it.
  Or copy it to ~/.config/blender/5.2/scripts/addons/ and enable via
  Preferences. The service starts automatically with Blender.

Env overrides (optional):
  BLENDER_AGENT_MCP_PORT   fixed port (default: 9877, then 9878..9886)
  BLENDER_AGENT_MCP_INFO   path to the info file (token + port)
"""

bl_info = {
    "name": "Blender Agent MCP",
    "author": "Saptodeep Sarkar",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Agent",
    "description": "Localhost service for agent-driven bpy scripting: context introspection, script execution, undo/redo.",
    "category": "Development",
}

import contextlib
import io
import json
import math
import os
import secrets
import socket
import sys
import time
import traceback
from pathlib import Path

import bpy  # noqa: F401  (only exists inside Blender)
import mathutils  # noqa: F401

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT_BASE = int(os.environ.get("BLENDER_AGENT_MCP_PORT", "9877"))
INFO_PATH = Path(
    os.environ.get(
        "BLENDER_AGENT_MCP_INFO",
        str(Path.home() / ".config" / "blender-agent-mcp" / "info.json"),
    )
)
MAX_STDOUT = 20000
MAX_TRACEBACK = 8000

# Module state
_srv = None          # listening socket
_timer_fn = None     # GUI poll timer
_token = ""
_port = 0
_started = False
_last_error = ""
_request_count = 0

# ---------------------------------------------------------------------------
# JSON-safe serialization
# ---------------------------------------------------------------------------


def json_safe(obj, depth=0):
    """Convert bpy/mathutils objects into plain JSON-safe Python values."""
    if depth > 8:
        return "<max-depth>"
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (list, tuple)):
        return [json_safe(x, depth + 1) for x in obj[:200]]
    if isinstance(obj, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in list(obj.items())[:200]}
    if isinstance(obj, bpy.types.bpy_prop_collection):
        return [json_safe(o, depth + 1) for o in obj[:200]]
    to_tuple = getattr(obj, "to_tuple", None)
    if callable(to_tuple):
        return list(to_tuple())
    name = getattr(obj, "name", None)
    if name is not None:
        return str(name)
    try:
        return repr(obj)[:500]
    except Exception:  # noqa: BLE001
        return "<unserializable>"


# ---------------------------------------------------------------------------
# Blender 5.x API helpers (defensive — some props renamed across versions)
# ---------------------------------------------------------------------------


def _strips(editor):
    """Return the strip collection: 5.x uses .strips, older uses .sequences."""
    if editor is None:
        return []
    for attr in ("strips", "sequences"):
        coll = getattr(editor, attr, None)
        if coll is not None:
            return coll
    return []


def _obj_sel(obj):
    try:
        return bool(obj.select_get())
    except Exception:  # noqa: BLE001
        return bool(getattr(obj, "select", False))


def _sel(strip):
    v = getattr(strip, "select", None)
    return bool(v) if v is not None else False


def _speed(strip):
    v = getattr(strip, "speed_factor", None)
    if v is None:
        return 1.0
    try:
        return round(float(v), 4)
    except Exception:  # noqa: BLE001
        return 1.0


def _gp_layers(obj):
    """Grease pencil layer info (legacy GPENCIL + new GREASE_PENCIL)."""
    try:
        data = obj.data
        if data is None:
            return []
        layers = getattr(data, "layers", None)
        if layers is None:
            return []
        out = []
        for lyr in layers[:50]:
            frames = getattr(lyr, "frames", None)
            name = getattr(lyr, "info", None) or getattr(lyr, "name", None) or "?"
            out.append({"name": name, "frames": len(frames) if frames is not None else 0})
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Context introspection (the "gain context" half of the service)
# ---------------------------------------------------------------------------


def _compositor_tree(scn):
    """The compositor node tree: 5.x uses compositing_node_group (a node
    group), older versions used scene.node_tree. Returns None when off."""
    for attr in ("compositing_node_group", "node_tree"):
        t = getattr(scn, attr, None)
        if t is not None:
            return t
    return None


def ctx_scene():
    scn = bpy.context.scene
    areas = []
    active_area = None
    if bpy.context.screen is not None:
        areas = [a.type for a in bpy.context.screen.areas]
    try:
        active_area = bpy.context.area.type if bpy.context.area is not None else None
    except Exception:  # noqa: BLE001
        active_area = None
    ed = getattr(scn, "sequence_editor", None)
    strips = _strips(ed) if ed else []
    return {
        "file": bpy.data.filepath or "<unsaved>",
        "version": bpy.app.version_string,
        "engine": scn.render.engine,
        "resolution": [scn.render.resolution_x, scn.render.resolution_y],
        "fps": round(scn.render.fps / scn.render.fps_base, 3) if scn.render.fps_base else scn.render.fps,
        "frame": scn.frame_current,
        "frame_start": scn.frame_start,
        "frame_end": scn.frame_end,
        "scene": scn.name,
        "mode": bpy.context.mode,
        "collections": sorted(c.name for c in bpy.data.collections)[:100],
        "object_count": len(bpy.data.objects),
        "selected_objects": sorted(o.name for o in scn.objects if _obj_sel(o))[:100],
        "areas": areas,
        "active_area": active_area,
        "use_compositor": _compositor_tree(scn) is not None,
        "render_output": scn.render.filepath or "",
        "render_format": scn.render.image_settings.file_format,
        "vse_strip_count": len(strips),
        "vse_selected_count": sum(1 for s in strips if _sel(s)),
    }


def ctx_objects(limit=300):
    scn = bpy.context.scene
    out = []
    for i, o in enumerate(scn.objects):
        if i >= limit:
            out.append({"note": f"truncated at {limit} objects"})
            break
        entry = {
            "name": o.name,
            "type": o.type,
            "location": [round(v, 4) for v in o.location],
            "rotation": [round(v, 4) for v in o.rotation_euler],
            "scale": [round(v, 4) for v in o.scale],
            "visible": o.visible_get(),
            "selected": _obj_sel(o),
            "parent": o.parent.name if o.parent else None,
            "collections": [c.name for c in o.users_collection],
            "modifiers": [m.type for m in o.modifiers][:20],
            "materials": [m.name for m in o.material_slots if m.material],
        }
        if o.type == "MESH":
            entry["vertices"] = len(o.data.vertices)
            entry["polygons"] = len(o.data.polygons)
        elif o.type == "CAMERA":
            entry["camera_type"] = o.data.type
            entry["focal_length"] = o.data.lens
        elif o.type == "LIGHT":
            entry["light_type"] = o.data.type
            entry["energy"] = o.data.energy
        elif o.type in ("GPENCIL", "GREASE_PENCIL"):
            entry["layers"] = _gp_layers(o)
        out.append(entry)
    return {
        "object_count": len(scn.objects),
        "selected": sorted(o.name for o in scn.objects if _obj_sel(o)),
        "active": bpy.context.active_object.name if bpy.context.active_object else None,
        "objects": out,
    }


def ctx_vse(limit=400):
    scn = bpy.context.scene
    ed = getattr(scn, "sequence_editor", None)
    if ed is None:
        return {
            "exists": False,
            "message": "No sequencer in this scene yet (add a strip, or create one with scene.sequence_editor_create()).",
            "strips": [],
            "selected": [],
        }
    strips = _strips(ed)
    out = []
    for i, s in enumerate(strips):
        if i >= limit:
            out.append({"note": f"truncated at {limit} strips"})
            break
        info = {
            "name": s.name,
            "type": s.type,
            "channel": s.channel,
            "frame_start": getattr(s, "frame_start", None),
            "frame_final_start": getattr(s, "frame_final_start", None),
            "frame_final_end": getattr(s, "frame_final_end", None),
            "frame_final_duration": getattr(s, "frame_final_duration", None),
            "frame_offset_start": getattr(s, "frame_offset_start", None),
            "frame_offset_end": getattr(s, "frame_offset_end", None),
            "speed_factor": _speed(s),
            "retiming_key_count": len(getattr(s, "retiming_keys", ())),
            "selected": _sel(s),
            "mute": getattr(s, "mute", False),
        }
        src = getattr(s, "filepath", None) or getattr(s, "sound", None)
        if src is not None:
            info["source"] = str(src)
        out.append(info)
    return {
        "exists": True,
        "strip_count": len(strips),
        "selected_count": sum(1 for s in strips if _sel(s)),
        "selected": sorted(s.name for s in strips if _sel(s)),
        "channels": sorted({s.channel for s in strips}),
        "frame": scn.frame_current,
        "strips": out,
    }


def ctx_animation(limit=100):
    actions = []
    for i, a in enumerate(bpy.data.actions):
        if i >= limit:
            break
        actions.append({
            "name": a.name,
            "frame_range": [round(v, 3) for v in a.frame_range],
            "fcurve_count": len(a.fcurves),
            "keyframes": sum(len(fc.keyframe_points) for fc in a.fcurves),
        })
    markers = [{"frame": m.frame, "name": m.name} for m in bpy.context.scene.timeline_markers][:200]
    return {
        "action_count": len(bpy.data.actions),
        "actions": actions,
        "marker_count": len(markers),
        "markers": markers,
    }


def ctx_compositor(limit=200):
    scn = bpy.context.scene
    nt = _compositor_tree(scn)
    if nt is None:
        return {"use_nodes": False, "message": "Compositor is off (no compositing node group)."}
    nodes = []
    for i, n in enumerate(nt.nodes):
        if i >= limit:
            break
        nodes.append({
            "name": n.name,
            "type": n.type,
            "label": n.label or "",
            "location": [round(n.location.x), round(n.location.y)],
            "mute": getattr(n, "mute", False),
        })
    return {"use_nodes": True, "node_count": len(nt.nodes), "link_count": len(nt.links), "nodes": nodes}


def ctx_materials(limit=150):
    out = []
    for i, m in enumerate(bpy.data.materials):
        if i >= limit:
            break
        node_types = []
        if m.use_nodes and m.node_tree is not None:
            node_types = sorted({n.type for n in m.node_tree.nodes})[:30]
        out.append({"name": m.name, "use_nodes": m.use_nodes, "node_types": node_types, "users": m.users})
    return {"material_count": len(bpy.data.materials), "materials": out}


def ctx_grease_pencil():
    out = []
    for o in bpy.data.objects:
        if o.type not in ("GPENCIL", "GREASE_PENCIL"):
            continue
        out.append({"name": o.name, "type": o.type, "layers": _gp_layers(o)})
    return {"grease_pencil_objects": out, "count": len(out)}


def ctx_ui():
    screen = bpy.context.screen
    if screen is None:
        return {"screen": None, "message": "No screen (background mode)."}
    areas = []
    for a in screen.areas:
        areas.append({"type": a.type, "x": a.x, "y": a.y, "width": a.width, "height": a.height})
    return {"screen": screen.name, "area_count": len(areas), "areas": areas, "mode": bpy.context.mode}


def ctx_auto():
    """Pick the domain based on what the user is currently looking at."""
    try:
        area = bpy.context.area.type if bpy.context.area is not None else None
    except Exception:  # noqa: BLE001
        area = None
    if area == "SEQUENCE_EDITOR":
        return {"_source": "vse", **ctx_scene(), **ctx_vse()}
    if area == "NODE_EDITOR":
        return {"_source": "compositor", **ctx_scene(), **ctx_compositor()}
    if area == "GRAPH_EDITOR":
        return {"_source": "animation", **ctx_scene(), **ctx_animation()}
    if area == "IMAGE_EDITOR":
        return {"_source": "image", **ctx_scene(), "image_editor": True}
    return {"_source": "scene", **ctx_scene()}


def ctx_all():
    return {
        **ctx_scene(),
        "objects": ctx_objects(limit=200),
        "vse": ctx_vse(limit=300),
        "animation": ctx_animation(limit=50),
        "compositor": ctx_compositor(limit=100),
        "materials": ctx_materials(limit=80),
        "grease_pencil": ctx_grease_pencil(),
        "ui": ctx_ui(),
    }


CONTEXT_FN = {
    "scene": ctx_scene,
    "objects": ctx_objects,
    "vse": ctx_vse,
    "animation": ctx_animation,
    "compositor": ctx_compositor,
    "materials": ctx_materials,
    "grease_pencil": ctx_grease_pencil,
    "ui": ctx_ui,
    "auto": ctx_auto,
    "all": ctx_all,
}

# ---------------------------------------------------------------------------
# Change detection (before/after) — lets the agent verify what a script did
# ---------------------------------------------------------------------------


def _digest():
    scn = bpy.context.scene
    ed = getattr(scn, "sequence_editor", None)
    strips = _strips(ed) if ed else []
    speeds = {}
    retiming = {}
    for s in strips[:400]:
        speeds[s.name] = _speed(s)
        retiming[s.name] = len(getattr(s, "retiming_keys", ()))
    return {
        "objects": len(bpy.data.objects),
        "selected_objects": sorted(o.name for o in scn.objects if _obj_sel(o))[:100],
        "strips": len(strips),
        "selected_strips": sorted(s.name for s in strips if _sel(s))[:100],
        "speeds": speeds,
        "retiming": retiming,
        "frame": scn.frame_current,
    }


def _diff(before, after):
    changes = []
    for k in ("objects", "strips", "frame"):
        if before.get(k) != after.get(k):
            changes.append(f"{k}: {before.get(k)} -> {after.get(k)}")
    for k in ("selected_objects", "selected_strips"):
        b, a = set(before.get(k, [])), set(after.get(k, []))
        if b != a:
            changes.append(f"{k}: {sorted(b)} -> {sorted(a)}")
    b_speeds, a_speeds = before.get("speeds", {}), after.get("speeds", {})
    changed_speeds = []
    for n in sorted(set(b_speeds) | set(a_speeds))[:100]:
        bv, av = b_speeds.get(n), a_speeds.get(n)
        if bv != av:
            changed_speeds.append(f"{n}: {bv} -> {av}")
    if changed_speeds:
        changes.append("speeds: " + "; ".join(changed_speeds[:30]))
    b_ret, a_ret = before.get("retiming", {}), after.get("retiming", {})
    changed_ret = []
    for n in sorted(set(b_ret) | set(a_ret))[:100]:
        bv, av = b_ret.get(n), a_ret.get(n)
        if bv != av:
            changed_ret.append(f"{n}: {bv} -> {av}")
    if changed_ret:
        changes.append("retiming: " + "; ".join(changed_ret[:30]))
    return changes


# ---------------------------------------------------------------------------
# Code execution (the "write a bpy script" half of the service)
# ---------------------------------------------------------------------------


def run_code(code, mode="exec"):
    ns = {
        "bpy": bpy,
        "C": bpy.context,
        "D": bpy.data,
        "context": bpy.context,
        "data": bpy.data,
        "scene": bpy.context.scene,
        "math": math,
        "mathutils": mathutils,
        "radians": math.radians,
        "degrees": math.degrees,
        "__result__": None,
    }
    out, err = io.StringIO(), io.StringIO()
    t0 = time.monotonic()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            if mode == "eval":
                ns["__result__"] = eval(compile(code, "<agent-eval>", "eval"), ns)  # noqa: S307
            else:
                exec(compile(code, "<agent-exec>", "exec"), ns)  # noqa: S102
            ok, error = True, None
        except Exception:  # noqa: BLE001
            ok, error = False, traceback.format_exc()
    dur_ms = round((time.monotonic() - t0) * 1000)
    return {
        "ok": ok,
        "error": (error[-MAX_TRACEBACK:] if error else None),
        "stdout": out.getvalue()[-MAX_STDOUT:],
        "result": json_safe(ns.get("__result__")),
        "duration_ms": dur_ms,
    }


def warmup_undo():
    """Background mode disables undo at startup; two undo_push() calls
    initialize the undo system so ed.undo()/redo() work afterwards.
    GUI mode is already initialized — never touch its undo stack."""
    if not bpy.app.background:
        return
    for _ in range(2):
        try:
            bpy.ops.ed.undo_push(message="agent warmup")
        except Exception:  # noqa: BLE001
            break


def push_checkpoint():
    """Push an undo step so a failed script can be fully reverted with one undo()."""
    try:
        bpy.ops.ed.undo_push(message="agent checkpoint")
        return True, None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def do_undo():
    try:
        bpy.ops.ed.undo()
        return {"ok": True, "result": "undone"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"undo failed: {exc}"}


def do_redo():
    """GUI: native redo. Background: disabled — bpy.ops.ed.redo() segfaults
    Blender 5.2 when the undo stack has 3+ pushes (verified 2026-08-08)."""
    if bpy.app.background:
        return {"ok": False,
                "error": "redo is disabled in background mode (Blender 5.2 "
                         "crashes on bpy.ops.ed.redo()); use the GUI session "
                         "for redo, or re-run the script instead"}
    try:
        bpy.ops.ed.redo()
        return {"ok": True, "result": "redone"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"redo failed: {exc}"}


# ---------------------------------------------------------------------------
# Request handler (pure — shared by GUI timer and background blocking loop)
# ---------------------------------------------------------------------------


def process_request(msg):
    mtype = msg.get("type", "")
    rid = msg.get("id")
    if mtype == "ping":
        return {"id": rid, "ok": True, "result": "pong", "info": json_safe(ctx_scene())}
    if mtype == "context":
        domain = str(msg.get("domain", "auto")).lower()
        if domain not in CONTEXT_FN:
            return {"id": rid, "ok": False, "error": f"unknown domain {domain!r}; use {sorted(CONTEXT_FN)}"}
        try:
            data = json_safe(CONTEXT_FN[domain]())
        except Exception:  # noqa: BLE001
            return {"id": rid, "ok": False, "error": traceback.format_exc()[-MAX_TRACEBACK:]}
        return {"id": rid, "ok": True, "domain": domain, "context": data}
    if mtype in ("exec", "eval"):
        code = msg.get("code", "")
        before = _digest()
        cp_ok, cp_err = False, None
        if mtype == "exec" and msg.get("checkpoint", True):
            cp_ok, cp_err = push_checkpoint()
        resp = run_code(code, mode=mtype)
        after = _digest()
        resp.update({
            "id": rid,
            "before": before,
            "after": after,
            "changed": _diff(before, after),
            "undo_checkpoint": cp_ok,
            "undo_checkpoint_error": cp_err,
        })
        return resp
    if mtype == "undo":
        return {"id": rid, **do_undo()}
    if mtype == "redo":
        return {"id": rid, **do_redo()}
    if mtype == "shutdown":
        return {"id": rid, "ok": True, "result": "bye"}
    return {"id": rid, "ok": False, "error": f"unknown type {mtype!r}"}


# ---------------------------------------------------------------------------
# Socket service
# ---------------------------------------------------------------------------


def _write_info():
    data = {
        "port": _port,
        "token": _token,
        "blender_version": bpy.app.version_string,
        "pid": os.getpid(),
        "started": time.time(),
    }
    try:
        INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = INFO_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(INFO_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[blender-agent-mcp] could not write info file {INFO_PATH}: {exc}", file=sys.stderr, flush=True)


def _send(conn, obj):
    try:
        conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001
        pass


def start_service():
    """Bind the socket and (in GUI mode) register the poll timer."""
    global _srv, _timer_fn, _token, _port, _started
    if _started:
        return {"ok": True, "message": "already running", "port": _port}
    _token = secrets.token_hex(16)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = PORT_BASE
    while port < PORT_BASE + 10:
        try:
            srv.bind(("127.0.0.1", port))
            break
        except OSError:
            port += 1
    else:
        srv.close()
        _last_error = f"could not bind any port in {PORT_BASE}..{PORT_BASE + 9}"
        return {"ok": False, "error": _last_error}
    srv.listen(4)
    srv.setblocking(False)
    _srv, _port, _started = srv, port, True
    warmup_undo()
    _write_info()
    if bpy.app.background:
        return {"ok": True, "port": _port, "mode": "background"}
    _timer_fn = _make_timer()
    bpy.app.timers.register(_timer_fn, first_interval=0.05)
    print(f"[blender-agent-mcp] Blender {bpy.app.version_string} service listening on 127.0.0.1:{_port}", flush=True)
    return {"ok": True, "port": _port, "mode": "gui"}


def stop_service():
    global _srv, _timer_fn, _started, _token, _port
    if _timer_fn is not None:
        with contextlib.suppress(Exception):
            bpy.app.timers.unregister(_timer_fn)
        _timer_fn = None
    if _srv is not None:
        with contextlib.suppress(Exception):
            _srv.close()
        _srv = None
    _started = False
    _token = ""
    _port = 0
    with contextlib.suppress(Exception):
        if INFO_PATH.exists():
            INFO_PATH.unlink()


def _tag_redraw():
    """Keep the sidebar panel fresh without hammering the UI."""
    with contextlib.suppress(Exception):
        screen = bpy.context.screen
        if screen is None:
            return
        for area in screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "UI":
                        region.tag_redraw()


def _make_timer():
    """GUI-mode poller: accepts one client, reads newline-delimited requests,
    executes them on the main thread (bpy-safe), replies, repeats."""
    global _request_count
    st = {"conn": None, "buf": b"", "authed": False}

    def poll():
        if not _started or _srv is None:
            return None
        if st["conn"] is None:
            try:
                conn, _ = _srv.accept()
                conn.setblocking(False)
                st.update(conn=conn, buf=b"", authed=False)
            except BlockingIOError:
                pass
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
        conn = st["conn"]
        if conn is None:
            _tag_redraw()
            return 0.5
        try:
            data = conn.recv(65536)
        except BlockingIOError:
            data = b""
        except Exception:  # noqa: BLE001
            data = b""
        if data == b"":
            with contextlib.suppress(Exception):
                conn.close()
            st["conn"] = None
            return 0.05
        st["buf"] += data
        while b"\n" in st["buf"]:
            raw, st["buf"] = st["buf"].split(b"\n", 1)
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                _send(conn, {"id": None, "ok": False, "error": "bad json"})
                continue
            if msg.get("type") == "auth":
                if msg.get("token") == _token:
                    st["authed"] = True
                    _send(conn, {"id": msg.get("id"), "ok": True, "result": "authenticated"})
                else:
                    _send(conn, {"id": msg.get("id"), "ok": False, "error": "auth failed"})
                continue
            if not st["authed"]:
                _send(conn, {"id": msg.get("id"), "ok": False, "error": "auth required"})
                continue
            global _request_count
            _request_count += 1
            try:
                _send(conn, process_request(msg))
            except Exception:  # noqa: BLE001
                global _last_error
                _last_error = traceback.format_exc()[-4000:]
                _send(conn, {"id": msg.get("id"), "ok": False, "error": _last_error})
        return 0.05

    return poll


def _serve_blocking(conn):
    """Background-mode: blocking per-connection loop (used for headless runs)."""
    authed = False
    f = conn.makefile("rb")
    while True:
        raw = f.readline()
        if not raw:
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _send(conn, {"id": None, "ok": False, "error": "bad json"})
            continue
        if msg.get("type") == "auth":
            if msg.get("token") == _token:
                authed = True
                _send(conn, {"id": msg.get("id"), "ok": True, "result": "authenticated"})
            else:
                _send(conn, {"id": msg.get("id"), "ok": False, "error": "auth failed"})
            continue
        if not authed:
            _send(conn, {"id": msg.get("id"), "ok": False, "error": "auth required"})
            continue
        if msg.get("type") == "shutdown":
            _send(conn, {"id": msg.get("id"), "ok": True, "result": "bye"})
            return True
        _send(conn, process_request(msg))
        return None


def main_bg():
    """Entry point when run as a script (blender --background --python ...)."""
    res = start_service()
    if not res.get("ok"):
        print(f"[blender-agent-mcp] {res.get('error')}", file=sys.stderr, flush=True)
        return
    srv = _srv
    if srv is None:
        print("[blender-agent-mcp] service socket unavailable", file=sys.stderr, flush=True)
        return
    print(f"[blender-agent-mcp] Blender {bpy.app.version_string} background service on 127.0.0.1:{_port}", flush=True)
    srv.setblocking(True)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except (KeyboardInterrupt, OSError):
                break
            try:
                if _serve_blocking(conn):
                    break  # shutdown requested
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
    finally:
        stop_service()
        print("[blender-agent-mcp] background service stopped", flush=True)


# ---------------------------------------------------------------------------
# UI panel (3D Viewport > Sidebar > Agent)
# ---------------------------------------------------------------------------


class AGENTMCP_OT_restart(bpy.types.Operator):
    bl_idname = "agent_mcp.restart_service"
    bl_label = "Restart Agent MCP Service"
    bl_description = "Restart the localhost service"

    def execute(self, context):  # noqa: ARG002
        stop_service()
        res = start_service()
        self.report({"INFO"}, f"Agent MCP service: {res}")
        return {"FINISHED"}


class AGENTMCP_PT_panel(bpy.types.Panel):
    bl_label = "Agent MCP"
    bl_idname = "AGENTMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Agent"

    def draw(self, context):  # noqa: ARG002
        layout = self.layout
        if _started:
            box = layout.box()
            box.label(text=f"Service: RUNNING  port {_port}", icon="CHECKMARK")
        else:
            box = layout.box()
            box.label(text="Service: STOPPED", icon="ERROR")
        layout.label(text=f"Blender {bpy.app.version_string}")
        layout.label(text=f"Requests served: {_request_count}")
        if _last_error:
            layout.label(text="Last error:")
            for line in _last_error.splitlines()[-4:]:
                layout.label(text=line[:80])
        layout.operator("agent_mcp.restart_service")


# ---------------------------------------------------------------------------
# Add-on registration
# ---------------------------------------------------------------------------


def register():
    bpy.utils.register_class(AGENTMCP_OT_restart)
    bpy.utils.register_class(AGENTMCP_PT_panel)
    start_service()


def unregister():
    stop_service()
    bpy.utils.unregister_class(AGENTMCP_PT_panel)
    bpy.utils.unregister_class(AGENTMCP_OT_restart)


if __name__ == "__main__":
    main_bg()
