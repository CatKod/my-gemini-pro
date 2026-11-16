"""Run the user's real Chrome profile (a copy) with a debug port, then drive the
extension in it against the running bridge.

Chrome 136+ refuses --remote-debugging-port on the default user-data directory, so
the profile is copied to a scratch directory first. The cookie values stay encrypted
with app-bound encryption, but that key is bound to the browser — not to the profile
directory — so the copy still decrypts.

Run:  python e2e_extension_real.py [profile-name]
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
sys.path.insert(0, str(REPO_ROOT / "my-geminiweb-orchestrator"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import cdp  # noqa: E402

from e2e_common import fresh_home, log, report, start_server, stop  # noqa: E402

EXTENSION_DIR = REPO_ROOT / "my-geminiweb-mcp" / "browser_extension" / "geminiweb-mcp-bridge"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_DATA = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
DEBUG_PORT = 9745


def copy_profile(name: str, dest: Path) -> Path:
    """Copy one Chrome profile into a fresh user-data directory."""
    dest.mkdir(parents=True, exist_ok=True)
    src = CHROME_DATA / name

    # The minimum a profile needs to be usable.
    for item in ("Preferences", "Secure Preferences", "Local State"):
        s = src / item
        if s.is_file():
            shutil.copy2(s, dest / item)

    for folder in ("Network", "Local Extension Settings", "Sessions", "Local Storage"):
        s = src / folder
        if s.is_dir():
            shutil.copytree(s, dest / folder, dirs_exist_ok=True)

    # The "Default" profile lives inside the user-data directory itself.
    return dest


def main():
    profile_name = sys.argv[1] if len(sys.argv) > 1 else "Profile 6"

    home = fresh_home("mygeminiweb_e2e_real")
    log("=" * 66)
    log(f"  E2E: the real extension in a copy of '{profile_name}'")
    log("=" * 66)

    if not CHROME.is_file():
        log(f"SKIP: chrome.exe not found at {CHROME}")
        return report([])

    proc, token, endpoint = start_server(home)
    failures = []
    port = int(endpoint.rsplit(":", 1)[1].split("/")[0])

    scratch = Path(tempfile.gettempdir()) / "mygeminiweb_real_profile"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    user_data = copy_profile(profile_name, scratch / "Default")
    # Local State must sit one level above the profile directory.
    ls = CHROME_DATA / "Local State"
    if ls.is_file():
        shutil.copy2(ls, scratch / "Local State")

    chrome = subprocess.Popen(
        [
            str(CHROME),
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={scratch}",
            "--no-first-run",
            "--no-default-browser-check",
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

        loaded = client.call("Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
        ext_id = loaded.get("id")
        log(f"[*] loadUnpacked -> {loaded}")
        if not ext_id:
            failures.append("the extension did not load")
            raise SystemExit(report(failures))

        # Open the extension page: it has chrome.cookies and chrome.runtime.
        tab_tid = client.call("Target.createTarget", {"url": "about:blank"}).get("targetId")
        sid = client.attach(tab_tid)
        client.call("Page.enable", {}, session_id=sid)
        client.call("Runtime.enable", {}, session_id=sid)
        client.call(
            "Page.navigate",
            {"url": f"chrome-extension://{ext_id}/popup.html"},
            session_id=sid,
        )
        time.sleep(3)

        def evaluate(expression):
            res = client.call(
                "Runtime.evaluate",
                {"expression": expression, "awaitPromise": True, "returnByValue": True},
                session_id=sid,
            )
            if "exceptionDetails" in res:
                log(f"    !! {res['exceptionDetails'].get('exception', {}).get('description')}")
            return res.get("result", {}).get("value")

        where = evaluate("JSON.stringify({url: location.href})")
        log(f"[*] extension page: {where}")

        # 1) what cookies does the extension see?
        cookies = evaluate(
            'chrome.cookies.getAll({domain: ".google.com"})'
            ".then(cs => JSON.stringify(cs.map(c => c.name)))"
        )
        log(f"[*] cookies visible: {cookies}")

        # 2) the extension must discover the bridge and read the token
        stored = evaluate(
            'chrome.storage.local.get(["endpoint","token"]).then(c => JSON.stringify(c))'
        )
        log(f"[*] storage: {stored}")
        try:
            stored_obj = json.loads(stored) if stored else {}
        except json.JSONDecodeError:
            stored_obj = {}
        if not stored_obj.get("token"):
            failures.append(f"the extension never learned the token: {stored}")

        # 3) send them
        result = evaluate(
            'chrome.runtime.sendMessage({type:"SEND_COOKIES"}).then(r => JSON.stringify(r))'
        )
        log(f"[*] SEND_COOKIES -> {result}")

        # 4) did the account reach the pool?
        pool_file = home / "accounts.json"
        if pool_file.is_file():
            pool = json.loads(pool_file.read_text(encoding="utf-8"))
            names = [a["name"] for a in pool.get("accounts", [])]
            log(f"[*] accounts.json -> {names}")
            if not names:
                failures.append("no account reached the pool file")
        else:
            log("[*] accounts.json not written")
            failures.append("the pool file was never written")

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
