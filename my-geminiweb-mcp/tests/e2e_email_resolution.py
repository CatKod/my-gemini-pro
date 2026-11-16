"""E2E: a harvest without an email must heal the right pool account.

The browser extension usually cannot report the Google account email: the Gemini page
title does not carry it. The server therefore resolves the email itself from the
harvested session and uses it to pick the pool entry to heal.

The test drives the real published binary and the real bridge against the live site.
The pool is seeded with a stale session id under the real account's email; after the
harvest, the entry must hold the fresh session id — and a repeated harvest must not
create a duplicate.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_common import (  # noqa: E402
    fresh_home,
    log,
    pool_cookies,
    post_to,
    report,
    start_server,
    stop,
)


def main():
    home = fresh_home("geminiweb_email_resolution")

    # The real cookie set of a signed-in account, plus its email — the email is what
    # the pool keys the account by.
    cookies = pool_cookies()
    psid = cookies["__Secure-1PSID"]
    psidts = cookies.get("__Secure-1PSIDTS", "")
    email = "kimperiod0126@gmail.com"

    # Seed the pool BEFORE the server starts: the server reads the pool file once, at
    # startup, and holds it in memory.
    pool = {
        "accounts": [
            {
                "name": "kimperiod0126",
                "email": email,
                "psid": "g.stale-session-id",
                "psidts": "sidts-stale",
                "cookies": {"NID": "stale-nid"},
            }
        ]
    }
    (home / "accounts.json").write_text(json.dumps(pool, indent=2), encoding="utf-8")

    proc, token, endpoint = start_server(home)

    failures = []
    try:
        # The harvest carries no account name and no email: exactly what the extension
        # sends when it cannot read the email off the page.
        payload = {
            "token": token,
            "account": "",
            "email": "",
            "psid": psid,
            "psidts": psidts,
            "cookies": {k: v for k, v in cookies.items() if k != "__Secure-1PSID"},
        }

        status, body = post_to(endpoint, payload)
        log(f"  [harvest] HTTP {status} -> {json.dumps(body)[:200]}")

        if status != 200 or not body.get("ok"):
            failures.append(f"harvest failed: HTTP {status} {body}")

        resolved = body.get("email") or ""
        if resolved != email:
            failures.append(f"email was not resolved from the session (got {resolved!r})")
        else:
            log(f"  [harvest] resolved email: {resolved}")

        if body.get("account") != "kimperiod0126":
            failures.append(
                f"the harvest healed the wrong account (got {body.get('account')!r})"
            )

        accounts = json.loads((home / "accounts.json").read_text(encoding="utf-8"))["accounts"]
        names = [a["name"] for a in accounts]
        log(f"  [pool] accounts: {names}")

        if len(accounts) != 1:
            failures.append(f"expected exactly 1 account after the harvest, found {names}")
        else:
            healed = accounts[0]
            if healed["psid"] != psid:
                failures.append("the harvested session id was not stored")
            if healed.get("email") != email:
                failures.append("the resolved email was not stored on the account")
            if healed["psidts"] != psidts:
                failures.append("the harvested session timestamp was not stored")
            if not healed.get("cookies", {}).get("NID"):
                failures.append("the extra cookies were not stored")

        # A second harvest with the same session must land on the same account.
        status, body2 = post_to(endpoint, payload)
        if body2.get("account") != body.get("account"):
            failures.append(f"a repeated harvest created a duplicate account: {body2.get('account')}")

        accounts = json.loads((home / "accounts.json").read_text(encoding="utf-8"))["accounts"]
        if len(accounts) != 1:
            failures.append(f"expected 1 account after two harvests, found {len(accounts)}")
    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
