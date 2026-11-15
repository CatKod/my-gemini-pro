"""Read-model tổng hợp cho dashboard — **một lần đọc, đủ mọi thứ**.

Vì sao cần module này
--------------------
GUI cũ tự lắp ghép dữ liệu từ nhiều nguồn rời rạt:
`store.list_jobs()`, `accounts.list_accounts()`, `cookie_sync.status()`,
`gemini_pool.quota_snapshot()` — mỗi cái một lần gọi, mỗi cái một kiểu trả
về, và cái quota thì **gọi mạng** (chậm, tốn rate-limit).

Module này gom tất cả thành **1 snapshot** có shape ổn định, để GUI chỉ
cần vẽ. Đây là phần "sự thật của hệ thống" — UI chỉ là view.

Shape của snapshot
------------------
```python
{
  "generated_at": 1731...,
  "jobs":    {"active": [...], "recent": [...]},
  "accounts": [...],          # account + trạng thái session + quota
  "pools":   [...],           # pool + headroom quota
  "summary": {"running": 3, "queued": 1, "failed_today": 0, ...},
}
```

Nguyên tắc
----------
1. **Không bao giờ raise.** Lỗi quota/network → `quota: {"ok": False, ...}`
   chứ không phá cả dashboard.
2. **Quota là phần đắt nhất** → có TTL cache và `refresh_quota=False` mặc
   định khi poll nhanh.
3. **Không bao giờ** trả mật khẩu plaintext ra ngoài.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from orchestrator import accounts as account_registry  # noqa: E402
from orchestrator import cookie_sync  # noqa: E402
from orchestrator import store  # noqa: E402

#: TTL cache quota (giây). Quota gọi mạng → không poll quá dày.
QUOTA_TTL = 120.0

#: Số job "gần đây" giữ trong snapshot.
RECENT_JOBS = 40

#: TTL cache trạng thái session riêng (fallback khi quota lỗi).
#:
#: Vì sao cần: `account_status` được lấy từ quota map. Nếu quota fetch lỗi
#: (mạng, rate-limit, `gemini_webapi` exception) → `session_ok = None` →
#: bảng hiện `UNKNOWN`, dù session thật vẫn sống. Ngược lại, nếu pool file
#: còn cookie **chết**, quota vẫn trả `UNAUTHENTICATED` → bảng hiện `UNAUTH`
#: dù người dùng vừa bấm "Kiểm tra" thấy OK.
#:
#: → Ta probe session **trực tiếp** (2 cookie, 1 request) cho mọi pool không
#: có quota, và cache kết quả `SESSION_TTL` giây.
SESSION_TTL = 90.0

_lock = threading.Lock()
_quota_cache: dict[str, Any] = {"at": 0.0, "data": {}}
_session_cache: dict[str, Any] = {"at": 0.0, "data": {}}


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------

def invalidate_quota_cache() -> None:
    """Xoá cache quota + session → lần `snapshot()` kế tiếp đọc mạng lại.

    Gọi sau mỗi thao tác thay đổi cookie (`sync`, `adopt`, `login`) — nếu
    không, dashboard vẫn hiện trạng thái cũ trong tối đa `QUOTA_TTL` (120s).

    Đây chính là lý do user thấy "bấm Kiểm tra thành công nhưng bảng ngoài vẫn
    UNAVAILABLE": cache quota 120s + cache của `gemini_webapi` che cookie mới.
    """
    with _lock:
        _quota_cache["at"] = 0.0
        _quota_cache["data"] = {}
        _session_cache["at"] = 0.0
        _session_cache["data"] = {}


def _session_probe(pool: str) -> dict[str, Any]:
    """Probe session trực tiếp cho 1 pool (fallback khi quota lỗi).

    Đọc 2 cookie auth từ `pools/<pool>.json` rồi gọi API thật. Cache theo
    `SESSION_TTL` để không spam mạng.

    Returns:
        dict `{"ok": bool|None, "account_status": str|None}` — `ok=None` nghĩa
        là không probe được (thiếu cookie / lỗi mạng).
    """
    pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
    if not pool_file.is_file():
        return {"ok": None, "account_status": None}
    try:
        data = json.loads(pool_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": None, "account_status": None}
    cookies = {
        c["name"]: c["value"]
        for c in (data.get("cookies") or [])
        if isinstance(c, dict) and c.get("name")
    }
    if cookie_sync.REQUIRED not in cookies:
        return {"ok": None, "account_status": None}
    try:
        res = cookie_sync._probe_session(cookies)
    except Exception:
        return {"ok": None, "account_status": None}
    return {
        "ok": res.get("ok"),
        "account_status": res.get("account_status"),
    }


def _session_map(pools: list[str]) -> dict[str, dict[str, Any]]:
    """Trạng thái session của mọi pool (có cache TTL)."""
    now = time.time()
    with _lock:
        if now - _session_cache["at"] < SESSION_TTL:
            return _session_cache["data"]

    data: dict[str, dict[str, Any]] = {}
    for p in pools:
        data[p] = _session_probe(p)

    with _lock:
        _session_cache["at"] = now
        _session_cache["data"] = data
    return data


def _quota_snapshot(force: bool = False) -> dict[str, dict[str, Any]]:
    """Quota của mọi pool (có cache TTL). Không bao giờ raise."""
    now = time.time()
    with _lock:
        if not force and now - _quota_cache["at"] < QUOTA_TTL:
            return _quota_cache["data"]

    data: dict[str, dict[str, Any]] = {}
    try:
        from gemini_pool import load_pools, quota_snapshot as _qs

        pools = [p for p in load_pools() if p.usable]
        if pools:
            data = _qs(pools)
    except Exception as e:
        data = {"__error__": {"ok": False, "error": f"{type(e).__name__}: {e}"}}

    with _lock:
        _quota_cache["at"] = now
        _quota_cache["data"] = data
    return data


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def _account_rows(quota: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Danh sách account đã merge registry + pool + quota."""
    rows: dict[str, dict[str, Any]] = {}

    # 1. Registry (nguồn sự thật).
    for info in account_registry.list_accounts():
        acc = info["account"]
        rows[acc] = {
            "account": acc,
            "email": info.get("email") or "",
            "pool": info.get("pool") or acc,
            "auth": info.get("auth") or "oauth",
            "has_password": bool(info.get("has_password")),
            "in_registry": True,
            "last_ok_at": info.get("last_ok_at"),
            "last_error": info.get("last_error"),
            "last_error_at": info.get("last_error_at"),
            "cookies": 0,
            "session_ok": None,
            "account_status": None,
            "quota": None,
            "profile_dir": None,
        }

    # 2. Pool file có cookie nhưng chưa khai báo trong registry → vẫn hiện
    #    (trạng thái legacy) để user không bỏ sót.
    try:
        pool_files = sorted(cookie_sync.POOLS_DIR.glob("*.json"))
    except Exception:
        pool_files = []
    for pf in pool_files:
        if pf.name in ("example.json", cookie_sync.ACCOUNT_POOLS_FILE):
            continue
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        acc = str(data.get("profile") or pf.stem)
        if acc in rows:
            rows[acc]["pool"] = pf.stem
        else:
            rows[acc] = {
                "account": acc,
                "email": data.get("account_email") or "",
                "pool": pf.stem,
                "auth": "oauth",
                "has_password": False,
                "in_registry": False,
                "last_ok_at": None,
                "last_error": None,
                "last_error_at": None,
                "cookies": 0,
                "session_ok": None,
                "account_status": None,
                "quota": None,
                "profile_dir": None,
            }

    # 3. Merge thông tin pool: số cookie + quota.
    #
    # Nguồn trạng thái session: **quota map** (có `account_status`).
    # Nếu quota không có entry cho pool (quota fetch lỗi / pool mới) → fallback
    # sang probe trực tiếp, để bảng không hiện `UNKNOWN` khi session thật vẫn
    # sống. Đây là nửa "kiểm tra OK nhưng bảng ngoài vẫn UNAVAILABLE".
    missing_pools = sorted(
        {
            row["pool"]
            for row in rows.values()
            if not isinstance(quota.get(row["pool"]), dict)
        }
    )
    fallback = _session_map(missing_pools) if missing_pools else {}

    for acc, row in rows.items():
        pool = row["pool"]
        pool_file = cookie_sync.POOLS_DIR / f"{pool}.json"
        if pool_file.is_file():
            try:
                pdata = json.loads(pool_file.read_text(encoding="utf-8"))
                row["cookies"] = len(pdata.get("cookies") or [])
                if not row["email"]:
                    row["email"] = pdata.get("account_email") or ""
            except (OSError, ValueError):
                pass

        q = quota.get(pool)
        if isinstance(q, dict):
            row["quota"] = q
            row["session_ok"] = q.get("account_status") in ("AVAILABLE", "ACTIVE")
            row["account_status"] = q.get("account_status")
            row["headroom"] = _headroom_pct(q)
        else:
            # Fallback: probe trực tiếp (cache 90s).
            fb = fallback.get(pool) or {}
            row["session_ok"] = fb.get("ok")
            row["account_status"] = fb.get("account_status")
            row["status_source"] = "probe"
        if "status_source" not in row:
            row["status_source"] = "quota"

        try:
            row["profile_dir"] = str(cookie_sync.account_dir(acc))
        except Exception:
            pass

    return sorted(rows.values(), key=lambda r: r["account"])


def _headroom_pct(quota: dict[str, Any]) -> float | None:
    """Headroom % của pool = MIN trong các window (5h + weekly).

    Chạm trần nào cũng chặn việc song song → lấy min. Trùng logic với
    `tools/gemini_pool.py::_pool_headroom`.
    """
    windows = quota.get("windows") or {}
    pcts = [
        w.get("remaining_percentage")
        for w in windows.values()
        if isinstance(w, dict)
        and isinstance(w.get("remaining_percentage"), (int, float))
    ]
    return round(float(min(pcts)), 1) if pcts else None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Rút gọn job cho dashboard (không kéo cả result JSON nặng)."""
    started = job.get("started_at")
    ended = job.get("ended_at")
    if started and ended:
        dur = ended - started
    elif started:
        dur = time.time() - started
    else:
        dur = 0.0
    return {
        "id": job.get("id"),
        "kind": job.get("kind"),
        "state": job.get("state"),
        "title": job.get("title") or (job.get("prompt") or "")[:70],
        "pool": job.get("pool"),
        "account": job.get("account"),
        "model": job.get("model"),
        "duration": round(dur, 1),
        "created_at": job.get("created_at"),
        "error": job.get("error"),
        "has_report": bool(job.get("report_path")),
        "last_event": _last_event_of(job.get("id")),
    }


def _last_event_of(job_id: str | None) -> str | None:
    """Dòng log cuối cùng của job (để UI biết 'đang làm gì')."""
    if not job_id:
        return None
    try:
        rows = store.get_events(job_id, limit=500)
    except Exception:
        return None
    if not rows:
        return None
    return rows[-1].get("message")


def _job_rows() -> dict[str, list[dict[str, Any]]]:
    active = [_job_view(j) for j in store.list_jobs(state="active", limit=200)]
    recent = [_job_view(j) for j in store.list_jobs(state=None, limit=RECENT_JOBS)]
    return {"active": active, "recent": recent}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def snapshot(*, refresh_quota: bool = False) -> dict[str, Any]:
    """Chụp 1 snapshot toàn hệ thống. Không bao giờ raise."""
    try:
        quota = _quota_snapshot(force=refresh_quota)
    except Exception as e:
        quota = {"__error__": {"ok": False, "error": f"{type(e).__name__}: {e}"}}

    try:
        accounts = _account_rows(quota)
    except Exception as e:
        accounts = []

    try:
        jobs = _job_rows()
    except Exception as e:
        jobs = {"active": [], "recent": [], "error": f"{type(e).__name__}: {e}"}

    # Summary — chỉ số user nhìn để biết "đang chạy gì".
    running = sum(1 for j in jobs["active"] if j["state"] == store.RUNNING)
    queued = sum(1 for j in jobs["active"] if j["state"] == store.QUEUED)
    failed_recent = sum(
        1 for j in jobs["recent"] if j["state"] == store.FAILED
    )
    alive_accounts = sum(1 for a in accounts if a.get("session_ok"))
    with_cookie = sum(1 for a in accounts if a.get("cookies"))

    return {
        "generated_at": time.time(),
        "jobs": jobs,
        "accounts": accounts,
        "summary": {
            "running": running,
            "queued": queued,
            "active": len(jobs["active"]),
            "failed_recent": failed_recent,
            "accounts_total": len(accounts),
            "accounts_alive": alive_accounts,
            "accounts_with_cookie": with_cookie,
        },
        "db_path": str(store.db_path()),
    }


def pool_quota(pool: str, *, force: bool = False) -> dict[str, Any]:
    """Quota của 1 pool cụ thể (dùng cho biểu đồ nhỏ trong GUI)."""
    data = _quota_snapshot(force=force)
    q = data.get(pool)
    if not isinstance(q, dict):
        return {"ok": False, "error": f"không có dữ liệu quota cho pool {pool!r}"}
    return q
