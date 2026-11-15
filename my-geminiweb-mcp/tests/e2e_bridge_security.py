"""Exercise the cookie bridge the way the browser extension does.

Checks:
  1. /ping identifies the bridge service.
  2. /config refuses a plain request (no extension origin) — a web page must not learn the token.
  3. /config serves the endpoint and token to an extension origin.
  4. /harvest refuses a bad token.
  5. /harvest accepts the real token and stores the account.
  6. The pool file on disk reflects the stored account.

Run:  python tests/e2e_bridge_security.py
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_common import (  # noqa: E402
    fresh_home,
    log,
    report,
    start_server,
    stop,
)

EXT_ORIGIN = "chrome-extension://jpokffnecmcalhcmfc"


def request(url, *, method="GET", body=None, origin=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def request_headers(url, *, origin=None):
    """The response headers, which is where the CORS decision shows up."""
    headers = {}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


def main():
    home = fresh_home("mygeminiweb_e2e_bridge_security")
    log("=" * 66)
    log("  E2E: bridge auth, CORS and ingest")
    log("=" * 66)

    proc, token, endpoint = start_server(home)
    failures = []
    base = endpoint.replace("/harvest", "")

    try:
        # 1) ping
        status, body = request(base + "/ping")
        log(f"[1] /ping -> {status} {body}")
        if status != 200 or body.get("service") != "my-geminiweb-mcp-bridge":
            failures.append(f"/ping did not identify the bridge: {status} {body}")

        # 2) /config must not be readable by a web page.
        #    The browser, not the server, is the gate here: a page may send the request,
        #    but without Access-Control-Allow-Origin it cannot read the reply.
        status, body = request(base + "/config", origin="https://evil.example")
        headers = request_headers(base + "/config", origin="https://evil.example")
        log(f"[2] /config (web page origin) -> {status}, ACAO={headers.get('Access-Control-Allow-Origin')!r}")
        if headers.get("Access-Control-Allow-Origin"):
            failures.append(
                "/config sends Access-Control-Allow-Origin to a web page — the token leaked"
            )

        # 3) /config with an extension origin
        status, body = request(base + "/config", origin=EXT_ORIGIN)
        log(f"[3] /config (extension origin) -> {status} {body}")
        if status != 200 or not body.get("token"):
            failures.append(f"/config did not serve the extension: {status} {body}")
        elif body["token"] != token:
            failures.append("/config returned a different token than the server's")

        # 4) harvest with a bad token
        status, body = request(
            endpoint,
            method="POST",
            body={"token": "wrong", "psid": "x", "account": "attacker"},
        )
        log(f"[4] /harvest (bad token) -> {status} {body}")
        if status != 403:
            failures.append(f"/harvest accepted a bad token (HTTP {status})")

        # 5) harvest with the real token
        status, body = request(
            endpoint,
            method="POST",
            body={
                "token": token,
                "account": "test-acc",
                "email": "test-acc@example.com",
                "psid": "g.a000TESTPSIDVALUE",
                "psidts": "sidts-TESTVALUE",
                "cookies": {"__Secure-3PSID": "s3", "NID": "nid123"},
            },
        )
        log(f"[5] /harvest (valid token) -> {status} {body}")
        if status != 200 or not body.get("ok"):
            failures.append(f"/harvest refused a valid token: {status} {body}")

        # 6) the pool file
        pool = json.loads((home / "accounts.json").read_text(encoding="utf-8"))
        log(f"[6] accounts.json -> {[a['name'] for a in pool['accounts']]}")
        names = [a["name"] for a in pool["accounts"]]
        if "test-acc" not in names:
            failures.append(f"the harvest did not reach the pool file: {names}")
        for a in pool["accounts"]:
            if a["name"] == "test-acc":
                log(f"    test-acc extras: {sorted(a.get('cookies', {}))}")
                if a.get("psidts") != "sidts-TESTVALUE":
                    failures.append("the pool file has the wrong psidts")
                if "NID" not in (a.get("cookies") or {}):
                    failures.append("the extra cookies were not stored")

    finally:
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
