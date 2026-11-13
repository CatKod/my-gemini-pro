"""Entry point cho bản .exe GUI (Tkinter dashboard).

Bản đóng gói này chỉ cần GUI. Lõi orchestrator (job store, runner) vẫn chạy
bằng Python thường trong `pools/` — GUI chỉ là *view* của cùng 1 SQLite store.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
