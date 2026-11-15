"""Print the extensions a Chrome profile has loaded, including unpacked ones.

Unpacked extensions live only in the profile's Secure Preferences (path = the folder
you chose with "Load unpacked"), which is why a plain scan of the Extensions directory
misses them.

Run:  python list_profile_extensions.py [profile-dir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def scan(user_data: Path):
    for prof in sorted(user_data.iterdir()):
        if not prof.is_dir():
            continue
        if not (prof.name == "Default" or prof.name.startswith("Profile ")):
            continue
        for fname in ("Preferences", "Secure Preferences"):
            prefs = prof / fname
            if not prefs.is_file():
                continue
            try:
                data = json.loads(prefs.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            settings = (data.get("extensions") or {}).get("settings") or {}
            for extid, meta in settings.items():
                if not isinstance(meta, dict):
                    continue
                path = meta.get("path")
                if not path:
                    continue
                manifest = meta.get("manifest")
                name = manifest.get("name") if isinstance(manifest, dict) else None
                state = meta.get("state")
                print(f"{prof.name:12} {fname:18} id={extid}")
                print(f"               name={name!r} state={state} location={meta.get('location')}")
                print(f"               path={path}")
                print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        scan(Path(sys.argv[1]))
    else:
        scan(Path(r"C:\Users\kimvi\AppData\Local\Google\Chrome\User Data"))
