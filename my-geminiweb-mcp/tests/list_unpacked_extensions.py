import json
import os
from pathlib import Path


def scan(base: Path, label: str):
    print(f"===== {label}: {base} =====")
    if not base.is_dir():
        print("  (missing)")
        return
    for prof in sorted(base.iterdir()):
        if not prof.is_dir():
            continue
        if not (prof.name == "Default" or prof.name.startswith("Profile ")):
            continue
        for fname in ("Preferences", "Secure Preferences"):
            prefs = prof / fname
            if not prefs.is_file():
                continue
            try:
                j = json.loads(prefs.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                print(f"  {prof.name}/{fname}: unreadable ({e})")
                continue
            settings = (j.get("extensions") or {}).get("settings") or {}
            for extid, meta in settings.items():
                if not isinstance(meta, dict):
                    continue
                path = meta.get("path")
                loc = meta.get("location")
                manifest = meta.get("manifest")
                name = manifest.get("name") if isinstance(manifest, dict) else None
                if path or loc in (4, 5, 6):
                    print(f"  {prof.name:10} {fname:18} id={extid[:18]:20} loc={loc} name={name!r}")
                    if path:
                        print(f"               path={path}")
    print()


scan(Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data", "CHROME")
scan(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Edge" / "User Data", "EDGE")
