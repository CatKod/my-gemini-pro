"""Nguồn sự thật duy nhất cho **đường dẫn dữ liệu** của orchestrator.

Vì sao cần module này
---------------------
Trước đây mỗi module tự tìm thư mục dữ liệu theo cách riêng:

| Module | Cách tìm | Hậu quả khi chạy `.exe` |
|---|---|---|
| `accounts._data_root()` | walk up 5 cấp từ `cwd` | `<repo>` |
| `cookie_sync._resolve_pools_dir()` | chỉ check `cwd` | `%LOCALAPPDATA%\\Orchestrator` |
| `tools/gemini_pool._resolve_dir()` | walk up 5 cấp từ `cwd` | `<repo>` |

Hệ quả: **split-brain** — GUI (chạy từ `dist/orchestrator-gui/`) ghi cookie vào
`%LOCALAPPDATA%\\Orchestrator\\pools\\<pool>.json`, còn dashboard đọc quota từ
`<repo>\\pools\\<pool>.json` (rỗng) → bảng hiện `UNAVAILABLE` dù popup báo
"thành công". Đây chính là triệu chứng user báo.

Nguyên tắc
----------
1. **Một hàm duy nhất** quyết định data root → mọi module dùng chung.
2. Thứ tự ưu tiên rõ ràng:
   1. `GEMINI_DATA_DIR` (explicit, ưu tiên cao nhất)
   2. `GEMINI_POOLS_DIR` (chỉ định riêng thư mục pool)
   3. Source (không frozen) → `<repo>`
   4. `.exe` → dò cây thư mục từ `cwd` (tối đa 6 cấp) tìm `.git` hoặc `pools/`
   5. `.exe` → `%LOCALAPPDATA%\\Orchestrator`
3. **Luôn ghi log 1 dòng** nếu phải fallback (giúp debug split-brain).
4. `mkdir` thư mục cần thiết ngay khi resolve (tránh lỗi ENOENT về sau).

Dùng
----
```python
from orchestrator import paths

paths.POOLS_DIR      # <data_root>/pools
paths.REPORTS_DIR    # <data_root>/reports
paths.ACCOUNTS_FILE  # <data_root>/accounts.json
paths.COOKIE_ARCHIVE_DIR  # <data_root>/cookie_files
```
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Tên thư mục con.
POOLS_DIRNAME = "pools"
REPORTS_DIRNAME = "reports"
COOKIE_ARCHIVE_DIRNAME = "cookie_files"
ACCOUNTS_FILENAME = "accounts.json"

#: Số cấp thư mục dò ngược từ `cwd` khi chạy `.exe`.
_MAX_PARENT_WALK = 6


def _frozen() -> bool:
    """Đang chạy từ bản `.exe` đóng gói (PyInstaller) hay không."""
    return bool(getattr(sys, "frozen", False))


def _looks_like_repo(cand: Path) -> bool:
    """Thư mục này có phải gốc repo orchestrator không."""
    return (cand / ".git").exists() or (cand / POOLS_DIRNAME).is_dir()


def _resolve_data_root() -> Path:
    """Chọn thư mục gốc dữ liệu. Xem docstring module để thứ tự ưu tiên."""
    env = os.environ.get("GEMINI_DATA_DIR")
    if env:
        return Path(env)

    # Source: `<repo>` — dùng vị trí file, không phụ thuộc cwd.
    if not _frozen():
        return Path(__file__).resolve().parent.parent

    # `.exe`: dò cây thư mục từ cwd (cho phép chạy .exe từ bất kỳ đâu trong repo).
    cwd = Path.cwd()
    for cand in [cwd, *list(cwd.parents)[:_MAX_PARENT_WALK]]:
        if _looks_like_repo(cand):
            return cand

    # Không tìm thấy → thư mục data cố định của user.
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Orchestrator"
    local.mkdir(parents=True, exist_ok=True)
    print(
        f"[orchestrator.paths] Không tìm thấy repo gốc từ cwd={cwd} "
        f"→ dùng data root: {local}",
        file=sys.stderr,
    )
    return local


DATA_ROOT = _resolve_data_root()


def _resolve_subdir(env_name: str, name: str) -> Path:
    """Thư mục con của data root (hỗ trợ override bằng biến môi trường)."""
    env = os.environ.get(env_name)
    if env:
        return Path(env)
    return DATA_ROOT / name


#: Thư mục chứa file pool (`<pool>.json`).
POOLS_DIR = _resolve_subdir("GEMINI_POOLS_DIR", POOLS_DIRNAME)

#: Thư mục lưu báo cáo deep research.
REPORTS_DIR = _resolve_subdir("GEMINI_REPORTS_DIR", REPORTS_DIRNAME)

#: Thư mục lưu bản sao JSON cookie gốc do user nhập.
COOKIE_ARCHIVE_DIR = DATA_ROOT / COOKIE_ARCHIVE_DIRNAME

#: File registry tài khoản.
ACCOUNTS_FILE = DATA_ROOT / ACCOUNTS_FILENAME

#: Thư mục state (job DB, log, credential) — dùng chung với `store`.
STATE_DIR = Path(
    os.environ.get("ORCH_STATE_DIR")
    or os.environ.get("GEMINI_STATE_DIR")
    or (Path.home() / ".cursor-agents" / "orchestrator")
)


def ensure_dirs() -> None:
    """Tạo các thư mục dữ liệu nếu chưa có (idempotent)."""
    for d in (POOLS_DIR, REPORTS_DIR, COOKIE_ARCHIVE_DIR, STATE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def describe() -> dict[str, str]:
    """Mô tả đường dẫn đang dùng (để log / debug)."""
    return {
        "frozen": str(_frozen()),
        "data_root": str(DATA_ROOT),
        "pools_dir": str(POOLS_DIR),
        "reports_dir": str(REPORTS_DIR),
        "cookie_archive_dir": str(COOKIE_ARCHIVE_DIR),
        "accounts_file": str(ACCOUNTS_FILE),
        "state_dir": str(STATE_DIR),
    }


ensure_dirs()
