"""E2E: chat with every account in the pool, using the real pool file.

Runs the published AOT binary against the live Gemini backend with the cookies that
are currently in %LOCALAPPDATA%\\MyGeminiWebMcp\\accounts.json.

Run:  python tests/e2e_chat_all.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_common import (  # noqa: E402
    call_tool,
    fresh_home,
    initialize,
    log,
    report,
    start_server,
    stop,
)

ACCOUNTS = ["kimperiod0126", "reigin483", "meobo930", "yarho3155"]


def main():
    home = fresh_home("mygeminiweb_e2e_chat_all")
    log("=" * 66)
    log("  E2E: one chat per account")
    log("=" * 66)

    # The pool lives in the real MCP home; copy it into the test home so the test does
    # not touch the user's own pool file.
    import os
    import shutil

    real_pool = Path(os.environ["LOCALAPPDATA"]) / "MyGeminiWebMcp" / "accounts.json"
    if not real_pool.is_file():
        log(f"FAIL: no pool file at {real_pool}")
        return report([f"no pool file at {real_pool}"])
    shutil.copy2(real_pool, home / "accounts.json")
    log(f"[*] copied the pool from {real_pool}")

    proc, _token, _ = start_server(home)
    failures = []

    try:
        initialize(proc)

        accounts = call_tool(proc, "gemini_accounts")
        log(f"[*] pool has {accounts.get('count')} account(s): "
            f"{[a['name'] for a in accounts.get('accounts', [])]}")

        for name in ACCOUNTS:
            chat = call_tool(proc, "gemini_chat", {"prompt": "Reply with the single word: OK", "account": name})
            reply = (chat.get("reply") or "").strip()
            log(f"  {name:18} -> {reply[:90]!r}")
            if not reply:
                failures.append(f"{name}: empty reply ({json.dumps(chat)[:200]})")

        # The pool file must still be valid JSON.
        pool_file = home / "accounts.json"
        if pool_file.exists():
            saved = json.loads(pool_file.read_text(encoding="utf-8"))
            log(f"[*] accounts.json still valid, {len(saved['accounts'])} account(s)")
    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
