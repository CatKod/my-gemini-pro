"""Drive the real browser extension against the real bridge.

Chrome is launched with a throwaway profile and `--load-extension` so the extension
under test is the one in this repository. The extension's service worker is then
triggered over CDP, which makes it read the cookies of the profile and POST them to
the running bridge.

The profile is seeded with a synthetic cookie set first, so the test does not need a
real Google session and does not touch the user's own browser data.

Run:  python tests/e2e_extension.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ORCH = REPO_ROOT / "my-geminiweb-orchestrator"
sys.path.insert(0, str(ORCH))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import cdp  # noqa: E402

from e2e_common import fresh_home, log, report, start_server, stop  # noqa: E402

EXTENSION_DIR = REPO_ROOT / "my-geminiweb-mcp" / "browser_extension" / "geminiweb-mcp-bridge"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEBUG_PORT = 9733

#: A synthetic cookie set. The extension must forward these verbatim.
SEED = {
    "__Secure-1PSID": "g.a000EXTENSIONTESTSID",
    "__Secure-1PSIDTS": "sidts-EXTENSIONTEST",
    "__Secure-3PSID": "ext-3psid",
    "__Secure-3PSIDTS": "ext-3psidts",
    "NID": "ext-nid-123",
    "COMPASS": "ext-compass",
}


def read_unpacked_extension_id(profile: Path) -> str | None:
    """The id Chrome gave the unpacked extension, read from the profile preferences."""
    prefs = profile / "Default" / "Secure Preferences"
    if not prefs.is_file():
        prefs = profile / "Default" / "Preferences"
    if not prefs.is_file():
        return None
    try:
        data = json.loads(prefs.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    settings = (data.get("extensions") or {}).get("settings") or {}
    for extid, meta in settings.items():
        if not isinstance(meta, dict):
            continue
        path = meta.get("path") or ""
        if path.rstrip("\\/").lower() == str(EXTENSION_DIR).rstrip("\\/").lower():
            return extid
    return None


def main():
    home = fresh_home("mygeminiweb_e2e_extension")
    log("=" * 66)
    log("  E2E: the real browser extension against the real bridge")
    log("=" * 66)

    if not CHROME.is_file():
        log(f"SKIP: chrome.exe not found at {CHROME}")
        return report([])

    proc, token, endpoint = start_server(home)
    port = int(endpoint.rsplit(":", 1)[1].split("/")[0])
    failures = []

    profile = Path(tempfile.gettempdir()) / "mygeminiweb_ext_profile"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)

    chrome = subprocess.Popen(
        [
            str(CHROME),
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            # The extension is loaded over CDP (Extensions.loadUnpacked) rather than with
            # --load-extension: Chrome 137+ ignores that switch, and combining the two
            # makes the loaded copy get disabled again.
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    client = None
    try:
        client = cdp.CDPClient(DEBUG_PORT)
        client.wait_until_ready(timeout=30)
        client.connect()
        log(f"[*] Chrome is up on port {DEBUG_PORT}")

        # --- seed the profile with the synthetic cookie set
        target_id = client.open_page("https://gemini.google.com/app")
        sid = client.attach(target_id)
        client.call("Network.enable", {}, session_id=sid)

        expires = int(time.time()) + 3600
        for name, value in SEED.items():
            client.set_cookie(name, value, session_id=sid, expires=expires)
        log(f"[*] seeded {len(SEED)} cookies into the throwaway profile")

        # --- load the extension: prefer the CDP command, fall back to the command line
        load_result = client.call("Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
        log(f"[*] Extensions.loadUnpacked -> {load_result}")
        ext_id = load_result.get("id") or read_unpacked_extension_id(profile)
        if ext_id is None:
            failures.append("the extension was not registered in the profile")
        else:
            log(f"[*] extension id: {ext_id}")

        # --- the extension's popup page, opened as a normal tab
        # An MV3 service worker is lazy: it only starts on an event. Opening the popup
        # page and asking it to talk to the worker wakes the worker up.
        # --- open the extension's own page
        # Chrome refuses a programmatic navigation to a chrome-extension:// URL in some
        # builds. The route that works is: create a normal tab, then Page.navigate into
        # the extension page (the tab keeps the extension's origin and its APIs).
        tab_tid = client.call("Target.createTarget", {"url": "about:blank"}).get("targetId")
        tab_sid = client.attach(tab_tid)
        client.call("Page.enable", {}, session_id=tab_sid)
        client.call("Runtime.enable", {}, session_id=tab_sid)
        client.call(
            "Page.navigate",
            {"url": f"chrome-extension://{ext_id}/popup.html"},
            session_id=tab_sid,
        )
        time.sleep(3)
        popup_sid = tab_sid
        log(f"[*] extension page opened: {tab_tid}")

        def evaluate(expression):
            res = client.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=popup_sid,
            )
            if "exceptionDetails" in res:
                log(f"    !! exception: {res['exceptionDetails']}")
            return res.get("result", {}).get("value")

        # 0) what did the popup page actually load?
        where = evaluate("JSON.stringify({url: location.href, hasChrome: typeof chrome})")
        log(f"[*] popup page: {where}")

        # 0a) can the extension reach the bridge at all?
        reach = evaluate(
            f'fetch("http://127.0.0.1:{port}/ping").then(r => r.text())'
            ".catch(e => 'ERR ' + e.message)"
        )
        log(f"[*] extension -> /ping: {reach}")
        reach2 = evaluate(
            f'fetch("http://127.0.0.1:{port}/config").then(r => r.status + " " + r.statusText)'
            ".catch(e => 'ERR ' + e.message)"
        )
        log(f"[*] extension -> /config: {reach2}")

        # 0b) are the extension's own resources reachable at all?
        probe = client.call(
            "Runtime.evaluate",
            {
                "expression": "document.documentElement.outerHTML.slice(0, 300)",
                "returnByValue": True,
            },
            session_id=popup_sid,
        )
        log(f"[*] popup DOM: {probe.get('result', {}).get('value')}")

        # Point the extension at this run's bridge before anything else happens.
        # The extension scans a range of ports, so without this it could latch onto a
        # bridge that is already running on the machine — and write into the real pool.
        evaluate("chrome.storage.local.clear().then(() => 'ok')")
        evaluate(
            f'chrome.storage.local.set({{endpoint: "http://127.0.0.1:{port}/harvest"}})'
            ".then(() => 'ok')"
        )
        # Ask the background to refresh the endpoint and token from that bridge.
        refreshed = evaluate(
            'chrome.runtime.sendMessage({type:"PING"}).then(r => JSON.stringify(r))'
        )
        log(f"[*] PING -> {refreshed}")

        stored = evaluate(
            'chrome.storage.local.get(["endpoint","token"]).then(c => JSON.stringify(c))'
        )
        log(f"[*] extension storage after load: {stored}")
        try:
            stored_obj = json.loads(stored) if stored else {}
        except json.JSONDecodeError:
            stored_obj = {}
        if not stored_obj.get("token"):
            failures.append(f"the extension never learned the token: {stored}")

        # 2) trigger the harvest the way the popup does
        value = evaluate(
            'chrome.runtime.sendMessage({type:"SEND_COOKIES"}).then(r => JSON.stringify(r))'
        )
        log(f"[*] SEND_COOKIES -> {value}")

        try:
            parsed = json.loads(value) if value else {}
        except json.JSONDecodeError:
            parsed = {}
        if not parsed.get("ok"):
            failures.append(f"the extension failed to send cookies: {value}")

        # 3) the cookies the extension actually read
        read = evaluate(
            'chrome.cookies.getAll({domain: ".google.com"})'
            ".then(cs => JSON.stringify(cs.map(c => c.name)))"
        )
        log(f"[*] cookies visible to the extension: {read}")

        # --- the account must have reached the pool file
        pool = json.loads((home / "accounts.json").read_text(encoding="utf-8"))
        log(f"[*] accounts.json -> {[a['name'] for a in pool['accounts']]}")
        if not pool.get("accounts"):
            failures.append("the extension's cookies never reached the pool file")
        else:
            for a in pool["accounts"]:
                log(f"    {a['name']}: psid={a.get('psid','')[:28]}... extras={sorted(a.get('cookies', {}))}")
                if a.get("psid") != SEED["__Secure-1PSID"]:
                    failures.append(f"wrong psid stored: {a.get('psid')}")
                if a.get("psidts") != SEED["__Secure-1PSIDTS"]:
                    failures.append(f"wrong psidts stored: {a.get('psidts')}")
                if "NID" not in (a.get("cookies") or {}):
                    failures.append("the extra cookies were not stored")

    finally:
        if client is not None:
            client.close()
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
        stop(proc)

    return report(failures)


if __name__ == "__main__":
    raise SystemExit(main())
