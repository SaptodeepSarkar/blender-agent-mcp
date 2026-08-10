"""Headless regression test for the MCP choke: a response LARGER than the
kernel send buffer must be fully delivered, not dropped.

Root cause (v1.0.6 and earlier): the GUI timer accepted the connection with
conn.setblocking(False), then _send() did a one-shot sendall(). The moment the
kernel send buffer filled (large context/exec responses, slow client, small
SO_SNDBUF), sendall() raised BlockingIOError — which _send() swallowed — and
the response was silently DROPPED. The client sat in readline() waiting for a
newline that never arrived until its 300s tool timeout ("mcp is choked").

Fix: responses are queued in st['out'] and flushed across ticks with
conn.send(), honoring backpressure. Nothing is dropped.

This test forces the failure deterministically on ANY system by capping the
server socket's SO_SNDBUF to 8KB before the 2MB response is written.

Run inside Blender (background is fine):
    blender --background --python tests/test_send_flow_headless.py
"""
import json
import socket
import sys
import time

# CRITICAL: Blender pre-imports the INSTALLED add-on copy (from
# ~/.config/blender/5.2/scripts/addons/) into sys.modules at startup. A plain
# `import blender_agent_addon` would silently load that STALE copy. Force-load
# the repo copy under test from its file path instead.
import importlib.util

_REPO_ADDON = "/home/saptodeepsarkar/BlenderAnimations/blender-agent-mcp/addon/blender_agent_addon.py"
_spec = importlib.util.spec_from_file_location("blender_agent_addon_repo", _REPO_ADDON)
mod = importlib.util.module_from_spec(_spec)
sys.modules["blender_agent_addon_repo"] = mod
_spec.loader.exec_module(mod)

PORT = 18988
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (("  " + str(detail)[:200]) if not ok else ""), flush=True)


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
poll, _hb = mod._make_timer()
mod._timer_fn = poll

BIG = 2 * 1024 * 1024  # 2 MB response — far beyond any small send buffer


def fake_process(msg):
    return {"id": msg.get("id"), "ok": True, "pad": "x" * BIG}


mod.process_request = fake_process


def tick(n=1):
    for _ in range(n):
        poll()


def recv_until(sock, rid, timeout=20.0):
    """Read newline-delimited JSON until id `rid`. Interleaves poll() ticks.
    The client stalls 0.3s before reading (slow consumer), then reads
    continuously with a short timeout — mirroring the real mcp_server.py
    client, which reads eagerly. The server send must survive the stall and
    deliver everything under send-buffer backpressure."""
    buf = b""
    deadline = time.time() + timeout
    sock.settimeout(0.02)
    stall = time.time() + 0.3
    while time.time() < deadline:
        if time.time() >= stall:
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
        tick()
    return None, f"timeout (received {len(buf)} bytes, no newline)"


# --- TEST 1: auth must still work (small response) ---
c1 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
c1.sendall((json.dumps({"id": 1, "type": "auth", "token": "testtoken"}) + "\n").encode())
msg, err = recv_until(c1, 1)
check("auth (small response)", msg is not None and msg.get("ok") is True, err)

# --- TEST 2: big response with send-buffer backpressure — the choke ---
# Cap the SERVER-side send buffer on the accepted connection so the 2MB
# response cannot fit in one send(): old code dropped it, new code queues.
# The accepted conn lives in _tick's closure state dict `st`.
tick_fn = poll.__closure__[0].cell_contents
st = None
for cell in tick_fn.__closure__:
    v = cell.cell_contents
    if isinstance(v, dict) and v.get("conn") is not None:
        st = v
        break
if st is not None and st["conn"] is not None:
    try:
        st["conn"].setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
        print("[test] capped server SO_SNDBUF=8192 on accepted conn", flush=True)
    except OSError as exc:
        print(f"[test] could not cap SO_SNDBUF: {exc}", flush=True)
else:
    print("[test] WARNING: could not find accepted conn to cap SO_SNDBUF", flush=True)

c1.sendall((json.dumps({"id": 2, "type": "exec", "code": "pass"}) + "\n").encode())
msg, err = recv_until(c1, 2)
size = len(json.dumps(msg)) if msg else 0
dbg = poll._dbg if hasattr(poll, "_dbg") else {}
print(f"[test] server dbg after big response: sent={dbg.get('sent')} err={dbg.get('err')} "
      f"flush_pending={dbg.get('flush_pending')}", flush=True)
check("BIG response delivered under send-buffer backpressure",
      msg is not None and msg.get("ok") is True, f"{err} (got {size} bytes)")
check("big response not truncated (pad length)",
      msg is not None and len(msg.get("pad", "")) == BIG,
      f"pad={len(msg.get('pad', '')) if msg else 0}")
c1.close()

# --- TEST 3: bridge still serves the NEXT connection after a big response ---
c2 = socket.create_connection(("127.0.0.1", PORT), timeout=5)
c2.sendall((json.dumps({"id": 3, "type": "auth", "token": "testtoken"}) + "\n").encode())
msg, err = recv_until(c2, 3)
check("bridge alive after big response", msg is not None and msg.get("ok") is True, err)
c2.sendall((json.dumps({"id": 4, "type": "ping"}) + "\n").encode())
msg, err = recv_until(c2, 4)
check("ping after big response", msg is not None and msg.get("ok") is True, err)
c2.close()

srv.close()
mod._srv = None
mod._started = False

allok = all(ok for _, ok in results)
print("RESULT:", "ALL PASS" if allok else "FAILURES", flush=True)
sys.exit(0 if allok else 1)
