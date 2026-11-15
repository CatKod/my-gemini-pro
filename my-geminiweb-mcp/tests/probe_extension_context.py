"""Probe: how to get an extension page (or its service worker) into a scriptable context.

Chrome blocks a direct CDP navigation to a chrome-extension:// URL in a fresh tab, and
an MV3 service worker only starts on an event. This script tries every route that is
known to work and reports which one succeeds.

Run:  python probe_extension_context.py
"""

from __future__ import annotations

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
DEBUG_PORT = 9734


def main():
    profile = Path(tempfile.gettempdir()) / "probe_ext_profile"
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
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    client = cdp.CDPClient(DEBUG_PORT)
    try:
        client.wait_until_ready(timeout=30)
        client.connect()
        print("[*] chrome up")

        loaded = client.call("Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
        ext_id = loaded.get("id")
        print(f"[*] loadUnpacked -> {loaded}")

        # --- route 1: Target.createTarget with the extension URL
        try:
            res = client.call(
                "Target.createTarget", {"url": f"chrome-extension://{ext_id}/popup.html"}
            )
            tid = res.get("targetId")
            print(f"[1] createTarget(extension url) -> {tid}")
            time.sleep(2)
            sid = client.attach(tid)
            client.call("Runtime.enable", {}, session_id=sid)
            out = client.call(
                "Runtime.evaluate",
                {
                    "expression": "JSON.stringify({url: location.href, chrome: typeof chrome, storage: typeof chrome!=='undefined' && !!chrome.storage})",
                    "returnByValue": True,
                },
                session_id=sid,
            )
            print(f"[1] -> {out.get('result', {}).get('value')}")
        except Exception as e:
            print(f"[1] failed: {type(e).__name__}: {e}")

        # --- route 2: Target.attachToTarget on an already-open extension page
        targets = client.call("Target.getTargets")
        ext_targets = [
            (t.get("type"), t.get("url"))
            for t in targets.get("targetInfos", [])
            if (t.get("url") or "").startswith(f"chrome-extension://{ext_id}/")
        ]
        print(f"[2] extension targets: {ext_targets}")

        # --- route 3: open the extension page from the omnibox-like flow
        # Chrome renders extension pages in a tab when the URL is typed by the user.
        # CDP's Page.navigate is treated as a programmatic navigation and is refused.
        page_tid = client.call(
            "Target.createTarget", {"url": "https://example.com/"}
        ).get("targetId")
        page_sid = client.attach(page_tid)
        client.call("Page.enable", {}, session_id=page_sid)
        nav = client.call(
            "Page.navigate",
            {"url": f"chrome-extension://{ext_id}/popup.html"},
            session_id=page_sid,
        )
        print(f"[3] Page.navigate -> {nav}")
        time.sleep(2)
        out = client.call(
            "Runtime.evaluate",
            {"expression": "JSON.stringify({url: location.href})", "returnByValue": True},
            session_id=page_sid,
        )
        print(f"[3] -> {out.get('result', {}).get('value')}")

        # --- route 4: wake the service worker with Target.setAutoAttach
        try:
            client.call(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                },
            )
            print("[4] setAutoAttach ok")
        except Exception as e:
            print(f"[4] failed: {e}")

        # --- route 5: the background page variant (manifest v2 only) vs worker
        targets = client.call("Target.getTargets")
        print("[5] all targets:")
        for t in targets.get("targetInfos", []):
            print(f"    {t.get('type'):18} {(t.get('url') or '')[:80]}")
    finally:
        client.close()
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()


if __name__ == "__main__":
    main()
