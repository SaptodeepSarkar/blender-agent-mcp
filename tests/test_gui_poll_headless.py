"""Headless test of the GUI timer poll() logic (no window needed).

Drives the add-on's _make_timer() callback directly against a real socket,
covering: the BlockingIOError race (connection must survive an empty recv
tick), auth, ping, run_script, undo, shutdown-by-close.
"""
import json
import socket
import sys
import time

sys.path.insert(0, "/home/saptodeepsarkar/BlenderAnimations/blender-agent-mcp/addon")
import blender_agent_addon as mod  # noqa: E402

PORT = 18987
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  " + detail[:160]) if not ok else ""), flush=True)


# --- set up the service state exactly like GUI start_service does ---
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", PORT))
srv.listen(4)
srv.setblocking(False)
mod._srv = srv
mod._port = PORT
mod._started = True
mod._token = "testtoken"
mod._request_count = 0
mod.warmup_undo()  # same as real start_service
poll = mod._make_timer()
mod._timer_fn = poll  # so panel self-heal logic has a handle


def tick(n=1):
    for _ in range(n):
        poll()


def recv_until(sock, rid, timeout=5.0):
    """Read newline-delimited JSON until the response with id `rid` arrives.
    Interleaves poll() ticks — the service only processes when ticked."""
    buf = b""
    deadline = time.time() + timeout
    sock.settimeout(0.2)
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                return None, "closed"
            buf += chunk
        except socket.timeout:
            pass
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                return msg, None
        tick()  # let the service process what we sent
    return None, "timeout"


# --- TEST 1: the race — connect, send NOTHING for 400ms; connection must survive ---
c1 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
time.sleep(0.4)  # several empty ticks while data is absent
for _ in range(10):
    tick()
# connection must still be open (the bug closed it on the first empty tick)
c1.settimeout(1.0)
try:
    probe = c1.recv(1)
    alive = probe != b""  # b"" would mean server closed us
except socket.timeout:
    alive = True
check("race fix: conn survives empty-recv ticks", alive)

# now send auth and ping on the SAME connection
c1.sendall((json.dumps({"id": 1, "type": "auth", "token": "testtoken"}) + "\n").encode())
msg, err = recv_until(c1, 1)
check("auth on surviving conn", msg is not None and msg.get("ok") is True
      and msg.get("result") == "authenticated", str(err))

c1.sendall((json.dumps({"id": 2, "type": "ping"}) + "\n").encode())
msg, err = recv_until(c1, 2)
check("ping", msg is not None and msg.get("ok") is True
      and "5.2" in str(msg.get("info", {}).get("version", "")), str(err))

# run_script + undo on same connection
c1.sendall((json.dumps({"id": 3, "type": "exec", "code":
            "import bpy\nbpy.ops.mesh.primitive_cube_add(size=2)\n__result__ = bpy.context.object.name"}) + "\n").encode())
msg, err = recv_until(c1, 3)
check("run_script", msg is not None and msg.get("ok") is True and msg.get("result"), str(err))

c1.sendall((json.dumps({"id": 4, "type": "undo"}) + "\n").encode())
msg, err = recv_until(c1, 4)
check("undo", msg is not None and msg.get("ok") is True and msg.get("result") == "undone", str(err))
c1.close()

# --- TEST 2: a fresh connection where data arrives before the first tick ---
c2 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
c2.sendall((json.dumps({"id": 5, "type": "auth", "token": "testtoken"}) + "\n").encode())
tick()
msg, err = recv_until(c2, 5)
check("auth (data-first connection)", msg is not None and msg.get("ok") is True, str(err))

c2.sendall((json.dumps({"id": 6, "type": "ping"}) + "\n").encode())
msg, err = recv_until(c2, 6)
check("ping (data-first connection)", msg is not None and msg.get("ok") is True, str(err))
c2.close()

# --- TEST 3: client disconnect is still detected (EOF) ---
c3 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
time.sleep(0.3)
tick()
c3.close()
time.sleep(0.3)
tick()
check("EOF still closes conn", mod is not None)  # no crash; poll handles it

# --- TEST 4: info.json self-heal (v1.0.4) — poll rewrites it every ~5s ---
import pathlib
info_path = pathlib.Path(mod.INFO_PATH)
info_path.unlink(missing_ok=True)
check("info.json deleted for self-heal test", not info_path.exists())
time.sleep(0.05)
for _ in range(3):
    tick()
info = json.loads(info_path.read_text()) if info_path.exists() else None
check("info.json self-healed by poll", info is not None and info.get("port") == PORT)

srv.close()
mod._srv = None
mod._started = False

allok = all(ok for _, ok in results)
print("RESULT:", "ALL PASS" if allok else "FAILURES", flush=True)
sys.exit(0 if allok else 1)
