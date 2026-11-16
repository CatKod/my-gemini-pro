"""Dump a Chrome extension's chrome.storage.local contents.

Chrome keeps extension storage in a LevelDB directory. Reading it while Chrome runs
is possible but awkward; the reliable way is to ask Chrome itself, over CDP, from the
extension's own page.

This script drives a scratch Chrome profile, loads the extension, and prints whatever
the extension stored.

Usage:  python dump_extension_storage.py [extension-id]
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

from orchestrator import cdp  # noqa: E402

EXTENSION_DIR = REPO_ROOT / "my-geminiweb-mcp" / "browser_extension" / "geminiweb-mcp-bridge"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
DEBUG_PORT = 9746


def main():
    ext_id = sys.argv[1] if len(sys.argv) > 1 else ""

    scratch = Path(tempfile.gettempdir()) / "dump_ext_profile"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)

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

    client = cdp.CDPClient(DEBUG_PORT)
    try:
        client.wait_until_ready(timeout=30)
        client.connect()

        loaded = client.call("Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
        ext_id = loaded.get("id") or ext_id
        print(f"[*] extension id: {ext_id}")

        tab = client.call("Target.createTarget", {"url": "about:blank"}).get("targetId")
        sid = client.attach(tab)
        client.call("Page.enable", {}, session_id=sid)
        client.call("Runtime.enable", {}, session_id=sid)
        client.call(
            "Page.navigate",
            {"url": f"chrome-extension://{ext_id}/popup.html"},
            session_id=sid,
        )
        time.sleep(3)

        res = client.call(
            "Runtime.evaluate",
            {
                "expression": (
                    'chrome.storage.local.get(null).then(c => JSON.stringify(c, null, 2))'
                ),
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=sid,
        )
        print(res.get("result", {}).get("value"))
    finally:
        client.close()
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()


if __name__ == "__main__":
    main()
