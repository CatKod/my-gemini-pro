"""Pool quản lý cookie Gemini (Deep Research) — multi-account, auto-routing.

Mỗi pool = 1 tài khoản Google + cookie phiên (`__Secure-1PSID` + `__Secure-1PSIDTS`).
Tên pool khớp với pool Antigravity trong `gemini_mcp.py` (`windows` / `wsl`) để
auto routing nhất quán giữa hai hệ thống.

Nguồn cookie: export từ Cookie-Editor (xem `docs/COOKIE-SETUP.md`).
File pool: `pools/<name>.json`.

    {
      "name": "windows",
      "account_email": "mmeo44244@gmail.com",
      "browser": "chrome",
      "profile": "Default",
      "cookies": [
        {"name": "__Secure-1PSID", "value": "...", "domain": ".google.com"},
        {"name": "__Secure-1PSIDTS", "value": "...", "domain": ".google.com"}
      ]
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_dir(env_name: str, name: str) -> Path:
    """Tìm thư mục dữ liệu, hỗ trợ chạy từ `.exe` (frozen).

    1. Biến môi trường tường minh (`GEMINI_POOLS_DIR` / `GEMINI_REPORTS_DIR`).
    2. Source → `<repo>/<name>`.
    3. `.exe` → `<cwd>/<name>` nếu cwd trông như repo, else
       `%LOCALAPPDATA%\\Orchestrator\\<name>`.
    """
    env = os.environ.get(env_name)
    if env:
        return Path(env)

    if not getattr(sys, "frozen", False):
        return REPO_ROOT / name

    cwd = Path.cwd()
    if (cwd / name).is_dir() or (cwd / ".git").exists():
        return cwd / name
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Orchestrator"
    return local / name


POOLS_DIR = _resolve_dir("GEMINI_POOLS_DIR", "pools")
REPORTS_DIR = _resolve_dir("GEMINI_REPORTS_DIR", "reports")

REQUIRED = "__Secure-1PSID"
OPTIONAL = "__Secure-1PSIDTS"

# Cache TTL cho snapshot quota — cùng TTL với `gemini_mcp.py` (300s).
QUOTA_CACHE_TTL = 300.0

# Model nào hỗ trợ Deep Research.
#
# Đây KHÔNG phải lựa chọn tuỳ ý: model mà account để mặc định (gemini-flash-lite
# ở account Flash-Lite, hoặc model đầu tiên trong `list_models()`) có thể từ chối
# vì không hỗ trợ Deep Research, và cả account Pro cũng vậy. Triệu chứng là
# `create_deep_research_plan()` trả về refusal ("tôi chỉ là mô hình ngôn ngữ")
# hoặc text rỗng, thay vì plan. Xem upstream issue HanaokaYuzu/Gemini-API#359.
#
# Vì danh sách model thay đổi theo account/tier, ta thử lần lượt và cache
# model đầu tiên tạo được plan vào file pool (`dr_model`).
DR_MODEL_CANDIDATES = (
    "gemini-4-hydrogen",
    "gemini-flash-lite",
    "gemini-pro",
    "gemini-carbon",
)



# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


@dataclass
class Pool:
    """Một tài khoản Gemini + client lazy."""

    name: str
    account_email: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    browser: str | None = None
    profile: str | None = None
    source_file: str | None = None

    # Runtime
    _client: Any = None
    _quota: dict[str, Any] | None = None
    _quota_at: float = 0.0
    dr_model: str | None = None

    @property
    def usable(self) -> bool:
        """Pool có cookie đủ để gọi API không."""
        return bool(self.cookies.get(REQUIRED))

    def new_client(self) -> Any:
        """Tạo `GeminiClient` mới (chưa init)."""
        from gemini_webapi import GeminiClient

        return GeminiClient(
            secure_1psid=self.cookies[REQUIRED],
            secure_1psidts=self.cookies.get(OPTIONAL),
            verbose=False,
        )


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _extract_cookies(raw: Any) -> dict[str, str]:
    """Chấp nhận 3 shape: dict{"cookies": [...]}, list [...], dict{name: value}."""
    items = raw
    if isinstance(raw, dict):
        items = raw.get("cookies", raw)
    out: dict[str, str] = {}
    if isinstance(items, dict):
        for k, v in items.items():
            if isinstance(v, str):
                out[k] = v
        return out
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("name") and isinstance(it.get("value"), str):
                out[it["name"]] = it["value"]
    return out


def load_pool(path: Path) -> Pool:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cookies = _extract_cookies(raw)

    name = raw.get("name") if isinstance(raw, dict) else None
    if not name:
        name = path.stem.replace("chrome-", "").replace("edge-", "")
        if name == "default":
            name = "windows"
        elif name in ("profile1", "profile-1"):
            name = "wsl"

    return Pool(
        name=name,
        account_email=raw.get("account_email") if isinstance(raw, dict) else None,
        cookies=cookies,
        browser=raw.get("browser") if isinstance(raw, dict) else None,
        profile=raw.get("profile") if isinstance(raw, dict) else None,
        source_file=str(path),
        dr_model=raw.get("dr_model") if isinstance(raw, dict) else None,
    )


def load_pools(pools_dir: Path | None = None) -> list[Pool]:
    """Load tất cả pool trong `pools/` (bỏ qua `example.json` và file hỏng)."""
    d = pools_dir or POOLS_DIR
    if not d.is_dir():
        return []
    pools: list[Pool] = []
    for f in sorted(d.glob("*.json")):
        if f.name == "example.json":
            continue
        try:
            pools.append(load_pool(f))
        except (OSError, ValueError):
            continue
    return pools


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


def _extract_headroom(usage_info: dict[str, Any]) -> dict[str, Any]:
    """Rút remaining % của từng window từ `usage_info` của gemini_webapi.

    `usage_info` có dạng:
        {"tier": {...}, "current_5h": {...}, "weekly": {...}}

    Mỗi window có `remaining_credits`, `usage_percentage` (đã dùng %) và `reset_at`.
    """
    out: dict[str, Any] = {}
    for window in ("current_5h", "weekly"):
        metric = usage_info.get(window)
        if not isinstance(metric, dict):
            continue
        remaining = metric.get("remaining_credits")
        used_pct = metric.get("usage_percentage")
        remaining_pct = None
        if isinstance(remaining, (int, float)) and isinstance(used_pct, (int, float)):
            remaining_pct = max(0.0, 100.0 - float(used_pct))
        out[window] = {
            "remaining_credits": remaining,
            "usage_percentage": used_pct,
            "remaining_percentage": remaining_pct,
            "reset_at": metric.get("reset_at"),
        }
    return out


async def _fetch_quota(pool: Pool, force: bool = False) -> dict[str, Any]:
    """Đọc quota của pool (cached)."""
    now = time.time()
    if (
        not force
        and pool._quota is not None
        and now - pool._quota_at < QUOTA_CACHE_TTL
    ):
        return pool._quota

    from gemini_webapi import GeminiClient

    client = GeminiClient(
        secure_1psid=pool.cookies[REQUIRED],
        secure_1psidts=pool.cookies.get(OPTIONAL),
        verbose=False,
    )
    try:
        await client.init(auto_refresh=False)
        usage_info = client.usage_info or {}
        data = {
            "ok": True,
            "account": pool.account_email,
            "tier": usage_info.get("tier"),
            "ai_credits_remaining": usage_info.get("ai_credits_remaining"),
            "windows": _extract_headroom(usage_info),
            "quotas": {
                k: v
                for k, v in (client.quotas or {}).items()
                if k not in ("usage_info", "extra")
            },
            "account_status": getattr(client.account_status, "name", None),
        }
    except Exception as e:
        data = {"ok": False, "error": f"{type(e).__name__}: {e}", "windows": {}}
    finally:
        try:
            await client.close()
        except Exception:
            pass

    pool._quota = data
    pool._quota_at = now
    return data


def quota_snapshot(pools: list[Pool], force: bool = False) -> dict[str, dict[str, Any]]:
    """Quota của mọi pool, đọc song song."""
    async def _all() -> dict[str, dict[str, Any]]:
        results = await asyncio.gather(
            *[_fetch_quota(p, force=force) for p in pools],
            return_exceptions=True,
        )
        out: dict[str, dict[str, Any]] = {}
        for pool, res in zip(pools, results):
            if isinstance(res, Exception):
                out[pool.name] = {"ok": False, "error": str(res), "windows": {}}
            else:
                out[pool.name] = res
        return out

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_all())

    # Đã ở trong event loop (MCP server) → chạy ở thread riêng.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(_all())).result()


# ---------------------------------------------------------------------------
# Pool selection
# ---------------------------------------------------------------------------


def _pool_headroom(quota: dict[str, Any]) -> float:
    """Headroom % của pool = MIN trong các window (5h + weekly).

    Chạm trần nào cũng chặn công việc, nên lấy min.
    """
    windows = (quota.get("windows") or {}) if isinstance(quota, dict) else {}
    pcts = [
        w.get("remaining_percentage")
        for w in windows.values()
        if isinstance(w, dict) and isinstance(w.get("remaining_percentage"), (int, float))
    ]
    return float(min(pcts)) if pcts else 0.0


def select_pool(
    pools: list[Pool],
    quota_map: dict[str, dict[str, Any]] | None = None,
    requested: str = "auto",
) -> tuple[Pool | None, dict[str, Any]]:
    """Chọn pool để spend.

    `requested` = "auto" → chấm điểm theo headroom, pool nào còn nhiều nhất.
    Tên pool cụ thể → dùng luôn, không đọc quota pool khác.

    Trả về `(pool, detail)`; `detail` luôn có bảng headroom để caller giải thích.
    """
    usable = [p for p in pools if p.usable]
    if not usable:
        return None, {"mode": "none", "chosen": None, "reason": "Không có pool nào có cookie 1PSID."}

    if requested and requested != "auto":
        match = next((p for p in usable if p.name == requested), None)
        if match is None:
            return None, {
                "mode": "explicit",
                "chosen": None,
                "reason": f"Không có pool tên {requested!r} (có: {[p.name for p in usable]}).",
            }
        return match, {
            "mode": "explicit",
            "chosen": match.name,
            "reason": f"Caller ghim pool={requested!r}.",
            "headroom": {},
        }

    if quota_map is None:
        quota_map = quota_snapshot(usable)

    headroom = {p.name: _pool_headroom(quota_map.get(p.name, {})) for p in usable}

    # Tất cả cạn → chọn pool tổng headroom lớn nhất (tương đương max ở trên).
    best = max(usable, key=lambda p: headroom[p.name])
    best_pct = headroom[best.name]

    if best_pct <= 0:
        return best, {
            "mode": "auto",
            "chosen": best.name,
            "reason": (
                "Không pool nào còn headroom đo được; chọn pool "
                f"{best.name!r} và báo lại nếu lỗi quota."
            ),
            "headroom": headroom,
        }

    others = ", ".join(
        f"{n}={headroom[n]:.1f}%" for n in headroom if n != best.name
    )
    return best, {
        "mode": "auto",
        "chosen": best.name,
        "reason": f"Pool {best.name!r} còn nhiều headroom nhất ({best_pct:.1f}%{', ' + others if others else ''}).",
        "headroom": headroom,
    }


# ---------------------------------------------------------------------------
# Report persistence
# ---------------------------------------------------------------------------


def save_report(title: str | None, markdown: str) -> Path:
    """Lưu report vào `reports/<timestamp>-<slug>.md` và trả về path."""
    import datetime
    import re

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "report").lower()).strip("-")[:50]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = REPORTS_DIR / f"{stamp}-{slug}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
