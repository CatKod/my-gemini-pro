"""Shared helpers for the MyGeminiWebMcp end-to-end tests.

The tests drive the real published binary over MCP stdio and the real cookie bridge
over loopback HTTP. Nothing is mocked: the server talks to gemini.google.com for real,
so a passing run means the live protocol still works.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# tests/ -> my-geminiweb-mcp/ -> repo root
MCP_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = MCP_ROOT.parent

PROJECT = MCP_ROOT / "my-geminiweb-mcp"
BIN = PROJECT / "bin" / "Release" / "net10.0" / "win-x64" / "my-geminiweb-mcp.exe"
PUBLISHED_BIN = (
    PROJECT / "bin" / "Release" / "net10.0" / "win-x64" / "publish" / "my-geminiweb-mcp.exe"
)

#: Cookie pool used as the source of a real, signed-in session.
POOL_FILE = REPO_ROOT / "my-geminiweb-orchestrator" / "pools" / "kimperiod0126.json"

PORT = 9478
ENDPOINT = f"http://127.0.0.1:{PORT}/harvest"


def next_free_port(start=9800, count=60):
    """A free TCP port, so two test runs never fight over the same one."""
    import socket

    for port in range(start, start + count):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port found")


def log(msg):
    print(msg, flush=True)


def binary():
    """The binary to test: the published AOT build when it exists."""
    return PUBLISHED_BIN if PUBLISHED_BIN.exists() else BIN


def fresh_home(name):
    """A clean GEMINIWEB_MCP_HOME under the system temp directory."""
    home = Path(os.environ.get("TEMP", "/tmp")) / name
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    return home


def start_server(home, port=None):
    """Starts the server and returns (process, token, endpoint).

    The bridge port is pinned so a test run cannot attach to — or be discovered by —
    a bridge the user is already running, which would write into the real pool.
    """
    env = dict(os.environ)
    env["GEMINIWEB_MCP_HOME"] = str(home)
    if port is None:
        port = next_free_port()
    env["GEMINIWEB_MCP_BRIDGE_PORT"] = str(port)

    proc = subprocess.Popen(
        [str(binary())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
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


def post(payload, endpoint=ENDPOINT):
    """POSTs to the bridge and returns (status, parsed body)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def post_to(endpoint, payload):
    """POSTs to a specific bridge endpoint."""
    return post(payload, endpoint)


def mcp_call(proc, method, params=None, _id=[0]):
    """Sends one MCP request over stdio and returns the matching response."""
    _id[0] += 1
    req_id = _id[0]
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()

    deadline = time.time() + 120
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("the server process died")
            continue
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log(f"  [stdout non-json] {text[:200]}")
            continue
        if data.get("id") == req_id:
            return data
    raise TimeoutError(f"no response for {method}")


def initialize(proc):
    mcp_call(
        proc,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e", "version": "1.0"},
        },
    )


def list_tools(proc):
    """Lists the MCP tools the server exposes."""
    res = mcp_call(proc, "tools/list")
    return [t["name"] for t in res.get("result", {}).get("tools", [])]


def call_tool(proc, name, args=None):
    """Calls an MCP tool and returns its parsed JSON result."""
    res = mcp_call(proc, "tools/call", {"name": name, "arguments": args or {}})
    content = res.get("result", {}).get("content", [])
    if not content:
        return {"_raw": res}
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_text": text}


def pool_cookies():
    """The real cookies of a signed-in Google account."""
    pool = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in pool["cookies"]}


def full_jar():
    """The complete cookie set, fetched the way a browser would."""
    import asyncio

    from curl_cffi.requests import AsyncSession

    base = pool_cookies()
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )

    async def go():
        s = AsyncSession(impersonate="chrome145")
        try:
            r = await s.get(
                "https://gemini.google.com/app",
                headers={
                    "Origin": "https://gemini.google.com",
                    "Referer": "https://gemini.google.com/",
                    "User-Agent": ua,
                },
                cookies=base,
            )
            jar = dict(base)
            try:
                raws = r.headers.get_list("set-cookie")
            except AttributeError:
                raws = []
            for c in raws:
                if not c:
                    continue
                name = c.split("=", 1)[0]
                jar[name] = c.split("=", 1)[1].split(";")[0]
            return jar
        finally:
            await s.close()

    return asyncio.run(go())


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def report(failures):
    """Prints the summary and returns the process exit code."""
    log("\n" + "=" * 66)
    if failures:
        log(f"  FAIL — {len(failures)} problem(s):")
        for f in failures:
            log(f"   - {f}")
        return 1
    log("  PASS")
    return 0
