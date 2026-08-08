# Blender Agent MCP

Agent-driven bpy scripting against **your live Blender** — not a headless copy.

A Blender add-on opens a localhost service the moment Blender starts. An MCP
server connects to it so an AI agent can:

- **gain context** about whatever is in the scene — VSE strips, objects,
  compositor nodes, animation data, materials, grease pencil, UI layout
- **write and run bpy scripts** inside the same live session (state,
  selection and undo stack persist across calls)
- **undo / redo** (Ctrl+Z / Ctrl+Shift+Z) to clean up a failed script before
  retrying
- **verify** — every script run returns a before/after diff, and you can
  re-read context to confirm the change actually happened

This is a *conversational* editing loop, not remote control: the agent never
spawns or renders anything by itself. It only talks to the Blender you
already have open.

```
┌─────────────────────┐   TCP 127.0.0.1:9877   ┌──────────────────────┐
│  YOUR Blender GUI   │  newline-delimited     │  MCP stdio server    │
│  (add-on service)   │ ◄────────────────────► │  server/mcp_server.py│
│  auto-starts w/     │  auth token + JSON-RPC │  read by Hermes etc. │
│  Blender            │                        └──────────────────────┘
└─────────────────────┘
```

## The workflow loop

The intended pattern (this is what the service is built for):

1. **`context(domain="vse")`** — understand the scene first. *"15 strips,
   7 selected, no retiming keys."*
2. **`run_script(code)`** — write a bpy script, run it. A checkpoint undo
   step is pushed first.
3. On error → **`undo()`** — the failed script's partial edits are reverted,
   then fix the script and run again.
4. **`context(...)` again** — verify. The `changed` diff in the run response
   already tells you what moved; a fresh context read confirms the final
   state. Then report done.

Example — *"speed up all selected clips in the VSE"*:

```
context(domain="vse")   → 4 strips, 2 selected, all speed_factor 1.0
run_script: for each selected strip: add SPEED effect strip (input1=strip)
             with speed_factor = 2.0
             → changed: ["speeds: Speed_ClipA: 1.0 -> 2.0", ...]
context(domain="vse")   → 6 strips, 2 of them SPEED strips at 2.0  ✓
```

## Tools

| Tool | What it does |
|---|---|
| `status` | Is Blender reachable? version, port, blend file, scene snapshot |
| `context(domain)` | Structured JSON of the scene. Domains: `auto` (what the user is looking at), `scene`, `objects`, `vse`, `animation`, `compositor`, `materials`, `grease_pencil`, `ui`, `all` |
| `run_script(code, checkpoint=True)` | Execute bpy code in the live session. Returns stdout, `__result__`, error traceback, and a before/after `changed` diff |
| `undo()` | Ctrl+Z — revert the last change (incl. a failed script) |
| `redo()` | Ctrl+Shift+Z |

`run_script` namespace: `bpy`, `C`/`context`, `D`/`data`, `scene`, `math`,
`mathutils`, `radians`, `degrees`. Set `__result__` to return data (auto
JSON-serialized).

## Install

1. Copy the add-on into Blender's addons folder and enable it once:

   ```bash
   cp addon/blender_agent_addon.py ~/.config/blender/5.2/scripts/addons/
   blender --background --python-expr "import bpy; bpy.ops.preferences.addon_enable(module='blender_agent_addon'); bpy.ops.wm.save_userpref()"
   ```

   (or: Edit > Preferences > Add-ons > Install... pick the file, enable it)

2. The service starts automatically whenever Blender opens. You'll see the
   status panel at **3D Viewport > Sidebar > Agent**.

3. Register the MCP server with your agent (example for Hermes):

   ```yaml
   mcp_servers:
     blender_agent:
       command: /path/to/blender-agent-mcp/.venv/bin/python
       args: ["/path/to/blender-agent-mcp/server/mcp_server.py"]
       timeout: 300
       connect_timeout: 60
   ```

   Server deps: `pip install "mcp>=1.0,<2.0"` (see `server/requirements.txt`).
   Note: mcp 2.x removed the FastMCP API — stay on 1.x.

## How it works

- The add-on binds `127.0.0.1` on port **9877** (falls back to 9878..9886 if
  busy) and writes `~/.config/blender-agent-mcp/info.json` containing the
  port + a random auth token. The file is removed when Blender exits.
- The MCP server reads that file per call, authenticates, sends one
  newline-delimited JSON request, gets one response. Each tool call is a
  fresh connection, so closing Blender can never wedge the agent — it just
  gets a clear "Blender is not running" message.
- In GUI mode requests are processed on Blender's main thread via a
  `bpy.app.timers` poller (bpy-safe). A long script blocks the UI the same
  way running it from the Text Editor would.
- `run_script` pushes an undo checkpoint (`ed.undo_push`) before executing,
  so one `undo()` fully reverts a failed attempt.
- Headless mode: `blender --background --python addon/blender_agent_addon.py`
  runs the same service with a blocking accept loop — used for testing.

## Blender 5.x API notes (learned the hard way)

- Strip collection is `scene.sequence_editor.strips` (`.sequences` is gone).
  Create with `strips.new_image(name, path, channel, frame_start)` /
  `new_movie` / `new_sound` / `new_effect(...)` — never `bpy.ops.sequencer.*`
  (needs editor context; fails in background and when the VSE isn't the
  active area).
- **Speed**: there is no `speed_factor` on image/movie strips anymore.
  Speeding up = SPEED effect strip: `strips.new_effect("Sp", "SPEED",
  channel=2, frame_start=1, input1=strip)` then `sp.speed_factor = 2.0`.
  The new retiming system exists too but only via `bpy.ops.sequencer.
  retiming_key_*` (context-dependent).
- New strips default to **selected**.
- No `frame_end` on strips — use `frame_final_start` / `frame_final_end` /
  `frame_final_duration`.
- Compositor tree is `scene.compositing_node_group` (`scene.node_tree` and
  `scene.use_nodes` are deprecated).
- `bpy.ops.ed.undo()` / `redo()` / `undo_push()` work in background mode too.
- Background-mode caveat (verified 2026-08-08): undo needs two `undo_push()`
  calls to initialize (the add-on does this at service start), and
  `bpy.ops.ed.redo()` **segfaults Blender 5.2** once the stack has 3+ pushed
  steps — so `redo` returns a clear error in background mode. GUI sessions
  (the real use case) use native undo/redo and are unaffected.

## Testing

```bash
# logic tests (runs inside Blender, no sockets)
blender --background --python tests/test_headless.py

# full stack: socket + auth + handlers
blender --background --python addon/blender_agent_addon.py &
.venv/bin/python tests/e2e_client.py          # sends shutdown when done

# MCP protocol handshake (initialize / tools/list / tools/call)
.venv/bin/python tests/test_mcp_handshake.py
```

## Troubleshooting

- **"Blender is not running"** — open Blender (the add-on auto-starts the
  service). Check the Agent panel in the 3D viewport sidebar says RUNNING.
- **Port busy** — the add-on falls back to 9878+ automatically; the info
  file always reflects the real port.
- **Multiple Blender instances** — the last one to start owns the info file;
  only that instance is reachable.
- **Stale info file after a crash** — the server tries the port anyway; a
  dead connection produces the "not running" message. Next clean Blender
  start rewrites it.
- **Add-on missing from preferences** — re-run the enable command above.

## License

MIT
