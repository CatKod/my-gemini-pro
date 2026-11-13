"""Entry point cho bản .exe console (headless).

Bản đóng gói này KHÔNG cần GUI — chỉ cần CLI để:
    - agent chạy nền `orchestrator-cli.exe watch <job_id>` để được Cursor wake-up
    - script tự động hoá
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # Chạy từ .exe đã đóng gói: thêm thư mục cha vào sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.orch_cli import main

if __name__ == "__main__":
    raise SystemExit(main())