"""Job store cho Orchestrator — SQLite, nhiều process ghi/đọc an toàn.

Mọi job state nằm trong 1 file SQLite. Runner (process con) ghi; MCP server và
GUI (process khác) đọc. Vì vậy store phải chịu được concurrent access.

    12|orchestrator/
      ├─ store.py       ← file này
      ├─ orch_mcp.py    ← MCP server (stdio)
      ├─ orch_cli.py    ← CLI
      └─ runners/
          ├─ dr.py      ← Gemini Deep Research
          ├─ chat.py    ← Gemini normal chat / canvas
          └─ agy.py    ← Antigravity CLI

Job kinds:
    deep_research  — Gemini Deep Research (gemini_webapi)
    chat          — Gemini normal chat (gemini_webapi)
    canvas        — Gemini canvas (gemini_webapi, tạo canvas artifact)
    antigravity   — Antigravity CLI (`agy`)
    chrome_profile— tạo hồ sơ Chrome mới (không cần network)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCH_DIR = REPO_ROOT / "orchestrator"
STATE_DIR = Path(
    os.environ.get("ORCH_STATE_DIR")
    or (Path.home() / ".cursor-agents" / "orchestrator")
)
DB_PATH = STATE_DIR / "jobs.db"
LOG_DIR = STATE_DIR / "logs"

# Trạng thái job. `RUNNING` là trạng thái cuối cho job đã kết thúc việc chạy
# nhưng chưa có kết quả? Không — mọi job đều kết thúc ở DONE/FAILED/CANCELLED.
QUEUED = "QUEUED"
RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

TERMINAL_STATES = (DONE, FAILED, CANCELLED)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    state        TEXT NOT NULL,
    title        TEXT,
    pool         TEXT,
    account      TEXT,
    model        TEXT,
    prompt       TEXT,
    params       TEXT,        -- JSON, tuỳ chọn riêng theo kind
    result       TEXT,        -- JSON kết quả cuối
    error        TEXT,
    report_path  TEXT,
    external_id  TEXT,        -- cid cho DR, conversation id cho chat, ...
    created_at   REAL NOT NULL,
    started_at   REAL,
    ended_at     REAL,
    pid          INTEGER,
    heartbeat_at REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    ts       REAL NOT NULL,
    level    TEXT NOT NULL,   -- info | warn | error
    kind     TEXT,            -- free-form: thinking | tool | plan | status | result
    message  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


# ---------------------------------------------------------------------------
# Kết nối
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL: nhiều process đọc/ghi đồng thời mà không khoá nhau.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(_SCHEMA)
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("params", "result"):
        raw = d.get(key)
        if raw:
            try:
                d[key] = json.loads(raw)
            except (TypeError, ValueError):
                d[key] = None
        else:
            d[key] = None
    return d


# ---------------------------------------------------------------------------
# Job API
# ---------------------------------------------------------------------------


def create_job(
    kind: str,
    prompt: str,
    *,
    pool: str | None = None,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    title: str | None = None,
) -> str:
    """Tạo job mới ở trạng thái QUEUED, trả về job id."""
    job_id = f"j_{uuid.uuid4().hex[:12]}"
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO jobs
               (id, kind, state, title, pool, model, prompt, params,
                created_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                kind,
                QUEUED,
                title,
                pool,
                model,
                prompt,
                json.dumps(params or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
    finally:
        conn.close()
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    """Cập nhật các cột của job. Tự chuyển `result`/`params` thành JSON."""
    if not fields:
        return
    allowed = {
        "state",
        "title",
        "pool",
        "account",
        "model",
        "result",
        "error",
        "report_path",
        "external_id",
        "started_at",
        "ended_at",
        "pid",
        "heartbeat_at",
    }
    cols = {k: v for k, v in fields.items() if k in allowed}
    if "result" in cols and not isinstance(cols["result"], (str, type(None))):
        cols["result"] = json.dumps(cols["result"], ensure_ascii=False, default=str)
    if not cols:
        return

    sets = ", ".join(f"{k} = ?" for k in cols)
    conn = _connect()
    try:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*cols.values(), job_id))
    finally:
        conn.close()


def get_job(job_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_jobs(
    state: str | None = None, kind: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if state:
        if state == "active":
            clauses.append("state IN (?, ?)")
            args += [QUEUED, RUNNING]
        elif state != "all":
            clauses.append("state = ?")
            args.append(state)
    if kind:
        clauses.append("kind = ?")
        args.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def claim_job(job_id: str, pid: int | None = None) -> bool:
    """Chuyển job QUEUED -> RUNNING. False nếu đã bị claim (race)."""
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE jobs
               SET state = ?, started_at = ?, pid = ?, heartbeat_at = ?
               WHERE id = ? AND state = ?""",
            (RUNNING, time.time(), pid, time.time(), job_id, QUEUED),
        )
        return cur.rowcount == 1
    finally:
        conn.close()


def finish_job(
    job_id: str,
    state: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    report_path: str | None = None,
    external_id: str | None = None,
    title: str | None = None,
) -> None:
    update_job(
        job_id,
        state=state,
        result=result,
        error=error,
        report_path=report_path,
        external_id=external_id,
        title=title,
        ended_at=time.time(),
        heartbeat_at=time.time(),
    )


def heartbeat(job_id: str) -> None:
    update_job(job_id, heartbeat_at=time.time())


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def add_event(
    job_id: str,
    message: str,
    *,
    level: str = "info",
    kind: str | None = None,
) -> None:
    """Ghi 1 dòng log của job. Đây là nguồn 'bạn đang làm gì' cho dashboard."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO events (job_id, ts, level, kind, message) VALUES (?, ?, ?, ?, ?)",
            (job_id, time.time(), level, kind, message),
        )
    finally:
        conn.close()


def get_events(
    job_id: str, after_id: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id, job_id, ts, level, kind, message FROM events
               WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?""",
            (job_id, after_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def last_event_id(job_id: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(id) AS m FROM events WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row["m"] or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Khác
# ---------------------------------------------------------------------------


def db_path() -> Path:
    return DB_PATH


def log_path(job_id: str) -> Path:
    """Đường dẫn file log của job (runner ghi thêm vào đây)."""
    return LOG_DIR / f"{job_id}.log"


def purge(keep: int = 200) -> int:
    """Xoá job cũ (terminal state) và event của chúng. Trả số job đã xoá."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id FROM jobs
               WHERE state IN (?, ?, ?)
               ORDER BY created_at DESC LIMIT -1 OFFSET ?""",
            (*TERMINAL_STATES, keep),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM events WHERE job_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM jobs WHERE id IN ({marks})", ids)
        return len(ids)
    finally:
        conn.close()
