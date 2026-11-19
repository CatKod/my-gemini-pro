"""Verify that `gemini_chat` always reports which model answered.

Google only echoes a model id on some replies, so `servedModel` used to come back
empty for models like 4 Hydrogen. This checks that the field is always present
and that it says whether the name was read from the payload or is the model that
was requested.

Usage: verify_served_model.py [account] [model ...]
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
    wanted = sys.argv[2:]

    cl = Client(EXE)
    init_id = cl.send("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "verify-served", "version": "1.0"}})
    cl.read(init_id, 60)
    cl.send("notifications/initialized", {}, notify=True)

    # The model names move — Google renames the lineup without notice, which is
    # the whole reason the server discovers them at runtime. Hardcoding a name
    # here would make this test fail for the wrong reason, so the list comes
    # from the server itself.
    models = wanted
    if not models:
        res = cl.call("gemini_models", {"account": account}, 180)
        selectable = [m for m in res.get("models", []) if m.get("selectable", True)]
        models = [m["name"] for m in selectable]
        print(f"models discovered for {account}: {models}")

    failures = []
    for model in models:
        print(f"\n=== gemini_chat model={model!r} ===")
        t0 = time.time()
        r = cl.call("gemini_chat",
                    {"prompt": "Say the number 7 only", "account": account,
                     "model": model}, 400)
        dt = time.time() - t0
        served = r.get("servedModel")
        confirmed = r.get("servedModelConfirmed")
        reply = str(r.get("reply"))[:60]
        notice = r.get("quotaNotice")
        error = r.get("error")
        print(f"({dt:.1f}s) served={served!r} confirmed={confirmed!r} "
              f"notice={notice!r} reply={reply!r} error={str(error)[:80]!r}")

        if error:
            failures.append(f"{model}: error {error!r}")
            continue
        if not served:
            failures.append(f"{model}: servedModel missing")
        if confirmed is None:
            failures.append(f"{model}: servedModelConfirmed missing")
        if notice:
            failures.append(f"{model}: Google downgraded the answer: {notice!r}")

    if cl.stderr:
        print("\n--- stderr tail ---")
        for line in cl.stderr[-8:]:
            print(line)
    cl.p.kill()

    print("\n=== result ===")
    if failures:
        for f in failures:
            print("FAIL", f)
        return 1
    print("PASS: every reply reported a model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
