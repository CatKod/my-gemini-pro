import json
import os
from pathlib import Path

root = Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
if not root.is_dir():
    print("NO CHROME PROFILE DIR")
    raise SystemExit(0)

rows = []
for prof in sorted(root.iterdir()):
    if not prof.is_dir():
        continue
    if not (prof.name == "Default" or prof.name.startswith("Profile ")):
        continue
    extdir = prof / "Extensions"
    if not extdir.is_dir():
        continue
    for extid in sorted(extdir.iterdir()):
        if not extid.is_dir():
            continue
        for ver in sorted(extid.iterdir()):
            mf = ver / "manifest.json"
            if not mf.is_file():
                continue
            try:
                j = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows.append((prof.name, extid.name, j.get("name", "?"), j.get("version", "?")))

for r in rows:
    print(f"{r[0]:12} {r[1][:12]:14} {r[2]:42} {r[3]}")

print()
hits = [r for r in rows if "gemini" in r[2].lower() or "gemini" in r[1].lower()]
print("MATCH gemini:", hits if hits else "NONE")
