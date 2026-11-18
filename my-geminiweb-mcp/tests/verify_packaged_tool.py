"""Check that the packed dotnet tool starts and answers MCP requests.

The tool is installed from the local .nupkg files, so this proves the package
set (tool package + RID package) is complete — not just that the project builds.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

TOOL_DIR = Path(r"C:\Users\kimvi\AppData\Local\Temp\tooltest")


def main():
    proc = subprocess.Popen(
        ["dotnet", "my-geminiweb-mcp"], cwd=str(TOOL_DIR),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1)

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def read(timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "pack-test", "version": "1.0"}}})
    resp = read()
    if resp is None:
        print("FAIL: no initialize response")
        proc.kill()
        return 1

    info = resp.get("result", {}).get("serverInfo", {})
    print(f"initialize OK: {info}")

    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    resp = read()
    tools = [t["name"] for t in (resp or {}).get("result", {}).get("tools", [])]
    print(f"tools: {tools}")

    ok = "gemini_chat" in tools and "gemini_models" in tools
    proc.kill()
    print("PASS" if ok else "FAIL: expected tools missing")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
