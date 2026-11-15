import json
import os
import shutil
from pathlib import Path

mcp = Path(os.environ["USERPROFILE"]) / ".cursor" / "mcp.json"
exe = Path(r"D:\GitHub\my-gemini-pro\my-geminiweb-mcp\my-geminiweb-mcp\bin\Release\net10.0\win-x64\publish\my-geminiweb-mcp.exe")

if not exe.is_file():
    raise SystemExit(f"missing binary: {exe}")

backup = mcp.with_suffix(".json.bak")
shutil.copy2(mcp, backup)

data = json.loads(mcp.read_text(encoding="utf-8"))
servers = data.setdefault("mcpServers", {})
servers["my-geminiweb"] = {
    "type": "stdio",
    "command": str(exe),
    "args": [],
    "env": {},
}

mcp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"registered 'my-geminiweb' -> {exe}")
print(f"backup: {backup}")
