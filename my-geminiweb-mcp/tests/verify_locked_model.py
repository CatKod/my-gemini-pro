"""Verify the locked-model detection end to end over MCP stdio.

Checks that `gemini_models` marks the model the account may not select, and that
asking for that model fails fast instead of hanging until the turn budget ends.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

EXE = str(
    Path(__file__).resolve().parent.parent
    / "my-geminiweb-mcp" / "bin" / "Release" / "net10.0" / "win-x64" / "publish"
    / "my-geminiweb-mcp.exe"
)


class Client:
    def __init__(self, exe):
        self.p = subprocess.Popen(
            [exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self._id = 0
        self.stderr = []
        threading.Thread(target=self._drain, daemon=True).start()

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

    def call(self, name, args, timeout=180):
        rid = self.send("tools/call", {"name": name, "arguments": args})
        resp = self.read(rid, timeout)
        if "error" in resp:
            return {"_error": resp["error"].get("message", "")}
        content = resp.get("result", {}).get("content", [])
        text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_text": text}


def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "kimperiod0126"
    cl = Client(EXE)
    init_id = cl.send("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "verify-locked", "version": "1.0"}})
    cl.read(init_id, 60)
    cl.send("notifications/initialized", {}, notify=True)

    print("=== gemini_models ===")
    t0 = time.time()
    res = cl.call("gemini_models", {"account": account}, 180)
    print(f"({time.time()-t0:.1f}s)")
    print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])

    models = res.get("models") or []
    blocked = [m for m in models if not m.get("selectable", True)]
    print(f"\n[*] unselectable reported by server: {res.get('unselectable')}")
    print(f"[*] models flagged selectable=false: {[m['description'] for m in blocked]}")

    for m in blocked:
        for name in (m["description"], m.get("currentName") or m["description"]):
            print(f"\n=== gemini_chat with locked model {name!r} ===")
            t0 = time.time()
            r = cl.call("gemini_chat",
                       {"prompt": "hi", "account": account, "model": name}, 200)
            dt = time.time() - t0
            print(f"({dt:.1f}s) -> {json.dumps(r, ensure_ascii=False)[:600]}")
            if dt > 30:
                print("    FAIL: took too long, the request was not refused up front")
            else:
                print("    PASS: refused fast")

    print("\n=== gemini_chat with a selectable model ===")
    t0 = time.time()
    r = cl.call("gemini_chat",
                {"prompt": "Say the number 7 only", "account": account,
                 "model": "gemini-flash-lite"}, 200)
    print(f"({time.time()-t0:.1f}s) -> {json.dumps(r, ensure_ascii=False)[:600]}")

    if cl.stderr:
        print("\n--- stderr tail ---")
        for line in cl.stderr[-10:]:
            print(line)
    cl.p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
