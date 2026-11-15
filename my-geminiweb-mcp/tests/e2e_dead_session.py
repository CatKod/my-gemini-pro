"""E2E: a dead session is detected, flagged, and routed around.

The point of this test is the silent failure mode: Google answers HTTP 200 for an
expired session and simply omits the session token. A server that only checks the
status code would report the account as healthy.

Checks:
  1. An account with a wrong __Secure-1PSIDTS is reported as failed, not healthy.
  2. It is flagged needsReharvest.
  3. It is taken out of rotation, so an unpinned chat uses the healthy account.
  4. The background keep-alive does not crash the server.

Run:  python tests/e2e_dead_session.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_common import (  # noqa: E402
    call_tool,
    fresh_home,
    initialize,
    log,
    pool_cookies,
    report,
    start_server,
    stop,
)

DEAD_PSIDTS = "sidts-BOGUS_" + "0" * 64


def main():
    home = fresh_home("mygeminiweb_e2e_dead")
    cookies = pool_cookies()

    (home / "accounts.json").write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "dead",
                        "email": "dead@example.com",
                        "psid": cookies["__Secure-1PSID"],
                        "psidts": DEAD_PSIDTS,
                    },
                    {
                        "name": "alive",
                        "email": "alive@example.com",
                        "psid": cookies["__Secure-1PSID"],
                        "psidts": cookies["__Secure-1PSIDTS"],
                    },
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    log("=" * 66)
    log("  E2E: dead session detection and failover")
    log("=" * 66)

    proc, _, _ = start_server(home)
    failures = []

    try:
        initialize(proc)

        # --- 1) the dead account must be caught
        log("\n[1] gemini_check ...")
        check = call_tool(proc, "gemini_check")
        for r in check.get("results", []):
            log(f"    {r['account']}: ok={r.get('ok')} "
                f"needsReharvest={r.get('needsReharvest')} "
                f"error={(r.get('error') or '')[:70]}")

        dead = next((r for r in check.get("results", []) if r["account"] == "dead"), None)
        if dead is None:
            failures.append("no result for the 'dead' account")
        else:
            if dead.get("ok"):
                failures.append("the dead account was reported as healthy")
            if not dead.get("needsReharvest"):
                failures.append(f"the dead account was not flagged: {dead}")

        alive = next((r for r in check.get("results", []) if r["account"] == "alive"), None)
        if alive and not alive.get("ok"):
            failures.append(f"the healthy account was reported as failed: {alive}")

        # --- 2) the accounts tool agrees
        accounts = call_tool(proc, "gemini_accounts")
        log(f"\n[2] gemini_accounts -> {json.dumps(accounts['accounts'])[:340]}")
        dead_status = next((a for a in accounts["accounts"] if a["name"] == "dead"), None)
        if dead_status and dead_status.get("healthy"):
            failures.append(f"the dead account is still healthy: {dead_status}")
        if dead_status and not dead_status.get("needsReharvest"):
            failures.append(f"the dead account is not flagged: {dead_status}")

        # --- 3) failover
        log("\n[3] gemini_chat without pinning -> must use 'alive' ...")
        chat = call_tool(proc, "gemini_chat", {"prompt": "Say OK"})
        log(f"    {json.dumps(chat)[:220]}")
        if chat.get("account") != "alive":
            failures.append(f"the chat ran on {chat.get('account')}, expected 'alive'")
        if "reply" not in chat:
            failures.append(f"the chat failed: {chat}")

        # --- 4) the keep-alive must not take the server down
        log("\n[4] letting the background keep-alive run for 20s ...")
        pool_file = home / "accounts.json"
        before = pool_file.read_text(encoding="utf-8")
        time.sleep(20)
        after = pool_file.read_text(encoding="utf-8")
        log(f"    the pool file changed: {before != after}")
        if proc.poll() is not None:
            failures.append("the server died while the keep-alive was running")
        else:
            log("    the server is still alive")

    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
