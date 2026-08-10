#!/usr/bin/env python3
"""Minimal MCP stdio handshake test against server/mcp_server.py.

Sends initialize -> notifications/initialized -> tools/list -> tools/call.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
SERVER = ROOT / "server" / "mcp_server.py"

proc = subprocess.Popen(
    [str(PY), str(SERVER)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)


def send(obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def recv(timeout=30):
    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        chunk = proc.stdout.read1(65536)
        if chunk:
            buf += chunk
            if b"\n" in buf:
                line, _ = buf.split(b"\n", 1)
                return json.loads(line)
        else:
            time.sleep(0.05)
    raise TimeoutError("no response")


ok = True


def step(name, cond, detail=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (f"  {str(detail)[:200]}" if detail else ""))
    if not cond:
        ok = False


# 1. initialize
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05",
                 "capabilities": {}, "clientInfo": {"name": "hermes-test", "version": "0"}}})
r = recv()
step("initialize", r.get("id") == 1 and "serverInfo" in r.get("result", {}), r)

# 2. initialized notification
send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

# 3. tools/list
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
r = recv()
tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
step("tools/list", set(tools) == {"status", "context", "run_script", "select_strips", "undo", "redo"}, tools)

# 4. tools/call status (Blender service should be up)
send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "status", "arguments": {}}})
r = recv()
txt = r.get("result", {}).get("content", [{}])[0].get("text", "")
data = json.loads(txt)
step("tools/call status", data.get("connected") is True, txt[:150])

# 5. tools/call run_script (error path)
send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
      "params": {"name": "run_script", "arguments": {"code": "1/0"}}})
r = recv()
txt = r.get("result", {}).get("content", [{}])[0].get("text", "")
data = json.loads(txt)
step("tools/call run_script error", data.get("ok") is False and "ZeroDivisionError" in data.get("error", ""), txt[:150])

proc.terminate()
proc.wait(timeout=5)
print()
print("MCP HANDSHAKE:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
