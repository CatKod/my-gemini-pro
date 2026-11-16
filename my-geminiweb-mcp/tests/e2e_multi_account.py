"""E2E: several accounts, a full cookie jar, and the refresh tool.

Checks:
  1. The bridge stores the complete cookie set, not just the two auth cookies.
  2. Two accounts land in the pool and both are listed.
  3. gemini_refresh reports per-account results.
  4. gemini_chat works against a pinned account.
  5. accounts.json stays valid JSON after every write.

Run:  python tests/e2e_multi_account.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_common import (  # noqa: E402
    call_tool,
    fresh_home,
    full_jar,
    initialize,
    log,
    post,
    report,
    start_server,
    stop,
)


def main():
    home = fresh_home("mygeminiweb_e2e_multi")
    log("=" * 66)
    log("  E2E: full cookie jar + multiple accounts")
    log("=" * 66)

    proc, token, endpoint = start_server(home)
    failures = []

    try:
        log("\n[*] fetching the full cookie jar from gemini.google.com/app ...")
        jar = full_jar()
        log(f"    the jar has {len(jar)} cookies: {sorted(jar)}")
        if len(jar) < 4:
            failures.append(f"the jar looks too small: {sorted(jar)}")

        # --- 1) two accounts
        for name in ("acc-a", "acc-b"):
            status, body = post(
                {
                    "token": token,
                    "account": name,
                    "email": f"{name}@example.com",
                    "psid": jar["__Secure-1PSID"],
                    "psidts": jar.get("__Secure-1PSIDTS", ""),
                    "cookies": jar,
                },
                endpoint,
            )
            log(f"[1] harvest {name} -> HTTP {status} {body}")
            if status != 200:
                failures.append(f"harvesting {name} failed: {status} {body}")

        # --- 2) the pool file
        pool_file = home / "accounts.json"
        saved = json.loads(pool_file.read_text(encoding="utf-8"))
        log(f"\n[2] accounts.json has {len(saved['accounts'])} account(s)")
        for a in saved["accounts"]:
            extras = a.get("cookies", {})
            log(f"    - {a['name']}: psid={len(a.get('psid',''))} chars, "
                f"psidts={len(a.get('psidts',''))} chars, extras={sorted(extras)}")
            if len(extras) < 3:
                failures.append(
                    f"{a['name']} kept only {len(extras)} extra cookies, expected at least 3")
        if len(saved["accounts"]) != 2:
            failures.append(f"expected 2 accounts, got {len(saved['accounts'])}")

        # --- 3) the tools
        initialize(proc)
        accounts = call_tool(proc, "gemini_accounts")
        log(f"\n[3] gemini_accounts -> count={accounts.get('count')}")
        if accounts.get("count") != 2:
            failures.append(f"gemini_accounts reports {accounts.get('count')}, expected 2")

        refresh = call_tool(proc, "gemini_refresh")
        log(f"[4] gemini_refresh -> {json.dumps(refresh)[:400]}")
        if not refresh.get("results"):
            failures.append("gemini_refresh returned nothing")
        for r in refresh.get("results", []):
            if r.get("needsReharvest"):
                failures.append(f"{r['account']} needs a re-harvest: {r.get('error')}")

        # --- 5) a pinned chat
        chat = call_tool(proc, "gemini_chat", {"prompt": "Say OK", "account": "acc-a"})
        log(f"[5] gemini_chat on acc-a -> {json.dumps(chat)[:220]}")
        if "reply" not in chat:
            failures.append(f"the pinned chat failed: {chat}")
        if chat.get("account") != "acc-a":
            failures.append(f"the pinned chat ran on {chat.get('account')}, expected acc-a")

        # --- 6) the pool file is still valid
        again = json.loads(pool_file.read_text(encoding="utf-8"))
        log(f"[6] accounts.json is still valid JSON with {len(again['accounts'])} account(s)")

    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
