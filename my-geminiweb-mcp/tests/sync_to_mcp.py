"""Push the orchestrator's live Chrome cookies into the C# MyGeminiWebMcp server.

The C# server owns a loopback cookie bridge (port 9478). This script reads the full
cookie jar from each account's dedicated Chrome profile over CDP and POSTs it to the
bridge, so the C# pool holds the same fresh cookies the Python orchestrator uses.

Usage:
    python sync_to_mcp.py                 # start the MCP server, sync, stop
    python sync_to_mcp.py --token TOKEN   # use an already-running server
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# tests/ -> my-geminiweb-mcp/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ORCH = REPO_ROOT / "my-geminiweb-orchestrator"
MCP_PROJECT = REPO_ROOT / "my-geminiweb-mcp" / "my-geminiweb-mcp"
BIN = MCP_PROJECT / "bin" / "Release" / "net10.0" / "win-x64" / "publish" / "my-geminiweb-mcp.exe"

sys.path.insert(0, str(ORCH))

ACCOUNTS = ["kimperiod0126", "reigin483", "meobo930", "yarho3155"]

#: Cookies worth carrying over. The two auth cookies are mandatory; the rest keep the
#: server-side session alive (``__Secure-3PSID`` family + ``NID``).
KEEP = [
    "__Secure-1PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSID",
    "__Secure-3PSIDTS",
    "NID",
    "COMPASS",
]


def log(msg):
    print(msg, flush=True)


def start_server():
    """Starts the AOT MCP server and returns (proc, token, endpoint)."""
    env = dict(os.environ)
    proc = subprocess.Popen(
        [str(BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    token = None
    endpoint = None
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        text = line.decode("utf-8", "replace").strip()
        log(f"  [server] {text}")
        if "cookie bridge token:" in text:
            token = text.split("cookie bridge token:")[1].strip()
        if "cookie bridge endpoint:" in text:
            endpoint = text.split("cookie bridge endpoint:")[1].strip()
        if token and endpoint:
            break
    if not token:
        raise RuntimeError("could not read the bridge token from stderr")
    return proc, token, endpoint


def post(payload, endpoint):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def harvest(account):
    """Reads the live cookie jar from the account's dedicated Chrome profile."""
    from orchestrator import cookie_sync

    udd = cookie_sync.account_dir(account)
    port = cookie_sync._find_free_port(cookie_sync._account_port_base(account))
    with cookie_sync.ChromeSession(udd, port=port, start_url="about:blank", reuse_existing=True) as s:
        live = s.cookies()

    jar = {name: live[name] for name in KEEP if live.get(name)}
    if "__Secure-1PSID" not in jar:
        raise RuntimeError(f"{account}: no __Secure-1PSID in the live jar")
    return jar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="token of an already-running MCP server")
    ap.add_argument("--endpoint", help="harvest endpoint of an already-running MCP server")
    args = ap.parse_args()

    proc = None
    endpoint = args.endpoint
    token = args.token
    try:
        if not token or not endpoint:
            log("Starting the MCP server ...")
            proc, token, endpoint = start_server()
        log(f"Bridge: {endpoint}")

        failures = []
        for account in ACCOUNTS:
            try:
                jar = harvest(account)
            except Exception as e:
                log(f"  FAIL {account}: harvest failed: {type(e).__name__}: {e}")
                failures.append(account)
                continue

            payload = {
                "token": token,
                "account": account,
                "email": f"{account}@gmail.com",
                "psid": jar["__Secure-1PSID"],
                "psidts": jar.get("__Secure-1PSIDTS", ""),
                "cookies": {k: v for k, v in jar.items() if k not in ("__Secure-1PSID", "__Secure-1PSIDTS")},
                "source": "orchestrator-cdp-sync",
            }
            status, body = post(payload, endpoint)
            ok = status == 200 and body.get("ok")
            log(f"  {'OK  ' if ok else 'FAIL'} {account}: http={status} {json.dumps(body, ensure_ascii=False)[:160]}")
            if not ok:
                failures.append(account)

        log("")
        log(f"synced {len(ACCOUNTS) - len(failures)}/{len(ACCOUNTS)} accounts")
        return 1 if failures else 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
