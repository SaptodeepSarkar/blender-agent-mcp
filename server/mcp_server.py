#!/usr/bin/env python3
"""Blender Agent MCP — stdio MCP server (agent side).

Connects to the Blender Agent MCP add-on running inside your LIVE Blender
(GUI) and exposes four tools to the agent:

    status      - is Blender running + reachable? version, port, scene snapshot
    context     - inspect whatever is in the scene (VSE, objects, compositor,
                  animation, materials, grease pencil, UI layout, ...)
    run_script  - execute a bpy script inside the live Blender session
    undo / redo - Ctrl+Z / Ctrl+Shift+Z to clean up after failed scripts

This server does NOT spawn or control Blender. The add-on opens a localhost
socket the moment Blender starts; this server reads the token/port from the
info file the add-on writes and talks to that instance. If Blender is closed,
tools return a clear "Blender is not running" message.

Run:
    python mcp_server.py            (stdio — Hermes launches this)

Config for ~/.hermes/config.yaml (see hermes_mcp.yaml.example):
    mcp_servers:
      blender_agent:
        command: "<abs path to venv>/bin/python"
        args: ["<abs path>/server/mcp_server.py"]
        timeout: 300
        connect_timeout: 60
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

DEFAULT_INFO = Path.home() / ".config" / "blender-agent-mcp" / "info.json"
INFO_PATH = Path(os.environ.get("BLENDER_AGENT_MCP_INFO", str(DEFAULT_INFO)))

PORT_BASE = int(os.environ.get("BLENDER_AGENT_MCP_PORT", "9877"))

NOT_RUNNING = (
    "Blender is not running, or the 'Blender Agent MCP' add-on is not active "
    "(it auto-starts when Blender opens — check 3D Viewport > Sidebar > Agent, "
    "or re-enable the add-on in Edit > Preferences > Add-ons). "
    f"Info file looked for: {INFO_PATH}"
)


def not_running_message() -> str:
    """Actionable bridge-down message. If a blender-agent socket is listening
    but info.json is gone, the add-on's poll timer died (an uncaught exception
    in a Blender timer silently removes it) — tell the user exactly how to
    revive it instead of the generic 'is Blender running?' text."""
    try:
        import socket as _s

        for _port in range(PORT_BASE, PORT_BASE + 10):
            try:
                _sock = _s.create_connection(("127.0.0.1", _port), timeout=0.15)
                _sock.close()
                return (
                    "Blender's Agent MCP socket is listening on 127.0.0.1:"
                    f"{_port} but unresponsive — the add-on's poll timer died. "
                    "Fix: 3D Viewport > Sidebar > Agent > 'Restart Agent MCP "
                    "Service' (or restart Blender). "
                    f"Info file looked for: {INFO_PATH}"
                )
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return NOT_RUNNING


class Bridge:
    """Reads the add-on's info file and transacts over the localhost socket."""

    def __init__(self) -> None:
        self._cache: dict | None = None
        self._mtime = 0.0

    def _info(self, refresh: bool = False) -> dict:
        try:
            m = INFO_PATH.stat().st_mtime
        except OSError as exc:
            raise RuntimeError(not_running_message()) from exc
        if refresh or m != self._mtime:
            try:
                self._cache = json.loads(INFO_PATH.read_text())
                self._mtime = m
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Info file unreadable: {INFO_PATH} ({exc})") from exc
        return self._cache

    def _transact(self, msg: dict, timeout: float, refresh: bool = False) -> dict:
        info = self._info(refresh=refresh)
        try:
            s = socket.create_connection(("127.0.0.1", int(info["port"])), timeout=10)
        except OSError as exc:
            raise RuntimeError(not_running_message()) from exc
        try:
            s.settimeout(timeout)
            rid = int(time.time() * 1000) % 10**9

            def sendline(obj: dict) -> None:
                s.sendall((json.dumps(obj) + "\n").encode("utf-8"))

            def readline() -> dict:
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise RuntimeError("connection closed by Blender")
                    buf += chunk
                    if len(buf) > 64 * 1024 * 1024:
                        raise RuntimeError("response too large")
                return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))

            sendline({"id": rid, "type": "auth", "token": info["token"]})
            auth = readline()
            if not auth.get("ok"):
                raise RuntimeError(f"auth failed: {auth.get('error')}")
            req = dict(msg)
            req.setdefault("id", rid + 1)
            sendline(req)
            return readline()
        finally:
            s.close()

    def call(self, msg: dict, timeout: float = 120.0) -> dict:
        """One request with a single retry on stale-token (add-on restarted)."""
        try:
            return self._transact(msg, timeout=timeout)
        except RuntimeError as exc:
            if "auth failed" in str(exc):
                return self._transact(msg, timeout=timeout, refresh=True)
            raise


bridge = Bridge()


def _out(obj) -> str:
    return json.dumps(obj, indent=2)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("blender-agent")


@mcp.tool()
def status() -> str:
    """Is your live Blender reachable? Returns Blender version, service port,
    blend file path and a compact scene snapshot (engine, resolution, fps,
    frame range, object/VSE strip counts, selected items). Call this first to
    confirm the Blender Agent MCP add-on is active before doing anything else."""
    try:
        info = bridge._info()  # noqa: SLF001
    except RuntimeError as exc:
        return _out({"ok": False, "connected": False, "error": str(exc)})
    try:
        resp = bridge.call({"type": "ping"}, timeout=10)
        return _out({
            "ok": True,
            "connected": True,
            "port": info.get("port"),
            "blender_version": info.get("blender_version"),
            "scene": resp.get("info"),
        })
    except RuntimeError as exc:
        return _out({"ok": False, "connected": False, "error": str(exc),
                     "info_file": str(INFO_PATH)})


@mcp.tool()
def context(domain: str = "auto") -> str:
    """Inspect the live Blender scene. domain:
      'auto'   - what the user is currently looking at (VSE / compositor /
                 graph editor / 3D viewport) — best default
      'scene'  - render engine, resolution, fps, frame range, collections,
                 object/strip counts, selected objects, open editor areas
      'objects'- every object: type, transforms, visibility, selection,
                 parent, collections, modifiers, materials, mesh stats
      'vse'    - video sequence editor: strips with name/type/channel/frames/
                 speed_factor/selection/mute, selected + total counts
      'animation' - actions, f-curve counts, keyframe totals, timeline markers
      'compositor' - compositor node tree: nodes (name/type/location), links
      'materials'  - materials and their shader node types
      'grease_pencil' - 2D grease pencil objects and layers
      'ui'     - editor layout: which areas are open, active area, mode
      'all'    - everything above (bounded)
    Returns structured JSON. Use this BEFORE writing a script (understand the
    scene) and AFTER running one (verify the change actually happened)."""
    try:
        resp = bridge.call({"type": "context", "domain": domain}, timeout=60)
    except RuntimeError as exc:
        return _out({"ok": False, "error": str(exc)})
    return _out(resp)


@mcp.tool()
def run_script(code: str, checkpoint: bool = True) -> str:
    """Execute a bpy script inside the user's LIVE Blender session. All state
    persists across calls (same scene, same undo stack, same viewport).

    Predefined names: bpy, C/context, D/data, scene, math, mathutils,
    radians, degrees. Assign __result__ to return data (auto JSON-serialized).

    Returns: ok, stdout (print output), result, error (traceback on failure),
    duration_ms, plus a before/after change diff ('changed') so you can see
    exactly what the script did (object/strip counts, selection, strip speeds).
    When checkpoint=True an undo step is pushed before executing, so a failed
    script can be fully reverted with a single undo() call before retrying.

    WORKFLOW: context() -> write script -> run_script() -> if error: undo(),
    fix the script, run again -> context() to verify. Never leave a failed
    script's partial changes behind."""
    try:
        resp = bridge.call({"type": "exec", "code": code, "checkpoint": checkpoint}, timeout=300)
    except RuntimeError as exc:
        return _out({"ok": False, "error": str(exc)})
    return _out(resp)


@mcp.tool()
def select_strips(
    type: str = "",
    channel: int = 0,
    name: str = "",
    regex: str = "",
    mode: str = "set",
) -> str:
    """Select VSE strips by ANY criteria — you are not bound to the user's
    manual selection. Filters combine (AND): type ('MOVIE'/'SOUND'/'IMAGE'/
    'META'/...), channel (>0), name (substring), regex (Python re on name).
    mode: 'set' (replace selection, default) | 'add' (keep current too) |
    'toggle'. Pass no filters to select all strips. Returns the selected
    strip names + count. After this, run_script acts on YOUR selection."""
    code = _select_code(type=type, channel=channel, name=name,
                        regex=regex, mode=mode)
    try:
        resp = bridge.call({"type": "exec", "code": code,
                            "checkpoint": False}, timeout=60)
    except RuntimeError as exc:
        return _out({"ok": False, "error": str(exc)})
    return _out(resp)


def _select_code(type: str = "", channel: int = 0, name: str = "",
                 regex: str = "", mode: str = "set") -> str:
    """Build a raw bpy script that selects matching VSE strips."""
    conds = []
    if type:
        conds.append(f"s.type == {type!r}")
    if channel and channel > 0:
        conds.append(f"s.channel == {channel}")
    if name:
        conds.append(f"{name!r} in s.name")
    if regex:
        conds.append(f"_re.search({regex!r}, s.name) is not None")
    if conds:
        match = " and ".join(conds)
        pick = "sel = [s for s in strips if " + match + "]"
    else:
        pick = "sel = list(strips)"
    if mode == "set":
        sel_all = "for s in strips:\n    s.select = False"
        apply_sel = "for s in sel:\n    s.select = True"
    elif mode == "add":
        sel_all = "pass"
        apply_sel = "for s in sel:\n    s.select = True"
    else:  # toggle
        sel_all = "pass"
        apply_sel = "for s in sel:\n    s.select = not s.select"
    code = (
        "import bpy, re as _re\n"
        "ed = bpy.context.scene.sequence_editor\n"
        "strips = list(ed.strips) if ed else []\n"
        + pick + "\n"
        + sel_all + "\n"
        + apply_sel + "\n"
        "print(len(sel), 'selected:', [s.name for s in sel][:20])\n"
    )
    return code


@mcp.tool()
def undo() -> str:
    """Ctrl+Z in Blender: revert the last change. Use after a failed script to
    clean up its partial edits before retrying with a fixed script."""
    try:
        return _out(bridge.call({"type": "undo"}, timeout=30))
    except RuntimeError as exc:
        return _out({"ok": False, "error": str(exc)})


@mcp.tool()
def redo() -> str:
    """Ctrl+Shift+Z in Blender: re-apply the last undone change."""
    try:
        return _out(bridge.call({"type": "redo"}, timeout=30))
    except RuntimeError as exc:
        return _out({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    mcp.run()
