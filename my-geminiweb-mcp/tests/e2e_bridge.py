"""E2E: the cookie bridge accepts a harvest and the MCP tools answer.

Checks:
  1. The bridge rejects a wrong token with HTTP 403.
  2. The bridge accepts the right token and writes the account into accounts.json.
  3. The MCP server exposes every expected tool.
  4. gemini_bridge reports the running endpoint.
  5. gemini_check and gemini_chat work against the live site.

Run:  python tests/e2e_bridge.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_common import (  # noqa: E402
    call_tool,
    initialize,
    list_tools,
    log,
    pool_cookies,
    post,
    report,
    start_server,
    stop,
    fresh_home,
)


def main():
    home = fresh_home("mygeminiweb_e2e_bridge")
    log("=" * 66)
    log("  E2E: cookie bridge + MCP tools")
    log("=" * 66)

    proc, token, endpoint = start_server(home)
    failures = []

    try:
        log(f"\n[OK] bridge endpoint = {endpoint}")

        # --- 1) a wrong token must be refused
        status, body = post({"token": "wrong-token", "psid": "x"})
        log(f"[1] wrong token -> HTTP {status} {body}")
        if status != 403:
            failures.append(f"a wrong token must give 403, got {status}")

        # --- 2) the right token stores the account
        cookies = pool_cookies()
        status, body = post(
            {
                "token": token,
                "account": "e2e-account",
                "email": "e2e@example.com",
                "psid": cookies["__Secure-1PSID"],
                "psidts": cookies.get("__Secure-1PSIDTS", ""),
                "cookies": cookies,
            }
        )
        log(f"[2] valid harvest -> HTTP {status} {body}")
        if status != 200 or not body.get("ok"):
            failures.append(f"a valid harvest was refused: {status} {body}")

        pool_file = home / "accounts.json"
        if not pool_file.exists():
            failures.append("accounts.json was not created")
        else:
            saved = json.loads(pool_file.read_text(encoding="utf-8"))
            names = [a["name"] for a in saved.get("accounts", [])]
            log(f"[2b] accounts.json has {len(names)} account(s): {names}")
            if "e2e-account" not in names:
                failures.append(f"'e2e-account' was not written to the pool: {names}")

        # --- 3) the tools
        initialize(proc)
        tool_names = list_tools(proc)
        log(f"\n[3] tools = {sorted(tool_names)}")
        for expected in (
            "gemini_chat",
            "gemini_accounts",
            "gemini_check",
            "gemini_refresh",
            "gemini_bridge",
        ):
            if expected not in tool_names:
                failures.append(f"the tool {expected} is missing")

        bridge = call_tool(proc, "gemini_bridge")
        log(f"[3b] gemini_bridge -> running={bridge.get('running')}")
        if not bridge.get("running"):
            failures.append("gemini_bridge says the bridge is not running")
        if bridge.get("token") != token:
            failures.append("gemini_bridge reports a different token than the one on stderr")

        accounts = call_tool(proc, "gemini_accounts")
        log(f"[3c] gemini_accounts -> count={accounts.get('count')}")

        # --- 4) live checks
        check = call_tool(proc, "gemini_check")
        for r in check.get("results", []):
            log(f"[4] check {r['account']}: ok={r.get('ok')} "
                f"needsReharvest={r.get('needsReharvest')}")
        if not check.get("results"):
            failures.append("gemini_check returned nothing")
        elif not check["results"][0].get("ok"):
            failures.append(f"gemini_check failed: {check['results'][0]}")

        chat = call_tool(proc, "gemini_chat", {"prompt": "Reply with exactly: OK"})
        log(f"[4b] gemini_chat -> {json.dumps(chat)[:220]}")
        if "reply" not in chat:
            failures.append(f"gemini_chat failed: {chat}")

    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
