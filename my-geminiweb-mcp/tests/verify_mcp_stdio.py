"""Kiểm chứng MCP server `my-geminiweb-mcp` qua stdio bằng protocol JSON-RPC thật.

Gọi `initialize` -> `tools/call` cho `gemini_check`, `gemini_models` và `gemini_chat`.
Không mock gì cả: server thật, cookie thật, Gemini thật.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

#: Binary AOT đã publish.
EXE = str(
    Path(__file__).resolve().parent.parent
    / "my-geminiweb-mcp"
    / "bin"
    / "Release"
    / "net10.0"
    / "win-x64"
    / "publish"
    / "my-geminiweb-mcp.exe"
)


class Client:
    def __init__(self, exe):
        self.p = subprocess.Popen(
            [exe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0
        self.stderr = []
        t = threading.Thread(target=self._drain, daemon=True)
        t.start()

    def _drain(self):
        for line in self.p.stderr:
            self.stderr.append(line.rstrip())

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return msg.get("id")

    def read(self, want_id, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == want_id:
                return msg
        raise TimeoutError(f"timeout waiting id={want_id}")


def call(cl, name, args, timeout=180):
    rid = cl.send("tools/call", {"name": name, "arguments": args})
    resp = cl.read(rid, timeout)
    content = resp.get("result", {}).get("content", [])
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    return "\n".join(texts)


def main():
    cl = Client(EXE)
    init_id = cl.send("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "verify", "version": "1.0"},
    })
    print("initialize ->", json.dumps(cl.read(init_id, 60).get("result", {}))[:200])
    cl.send("notifications/initialized", {}, notify=True)

    for tool, args, timeout in [
        ("gemini_check", {}, 120),
        ("gemini_models", {}, 120),
        ("gemini_chat", {"prompt": "Reply with exactly: 42", "model": "gemini-flash-lite"}, 180),
    ]:
        t0 = time.time()
        try:
            out = call(cl, tool, args, timeout)
            print(f"\n=== {tool} ({time.time()-t0:.1f}s) ===\n{out[:1500]}")
        except Exception as e:
            print(f"\n=== {tool} FAILED ({time.time()-t0:.1f}s): {e}")

    if cl.stderr:
        print("\n--- stderr tail ---")
        for line in cl.stderr[-15:]:
            print(line)

    cl.p.kill()


if __name__ == "__main__":
    sys.exit(main())
