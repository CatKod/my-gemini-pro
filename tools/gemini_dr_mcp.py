"""MCP server: Gemini Deep Research qua `gemini_webapi` + cookie Gemini Advanced.

Cung cấp 4 tool:
    gemini_dr_research(query, pool, timeout, model)  → chạy full research, trả report markdown
    gemini_dr_plan(query, pool)                      → chỉ xem plan, CHƯA chạy
    gemini_dr_quota(pool)                            → headroom mỗi pool
    gemini_dr_account()                              → email ↔ pool mapping

Deep Research trên gemini.google.com chạy server-side và trừ vào allowance
Gemini Advanced (gói AI Pro đã trả) — KHÁC với AI Studio Interactions API
(tính phí pay-as-you-go riêng).

Transport: stdio MCP server (MCP Python SDK v2).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gemini_pool import (  # noqa: E402
    DR_MODEL_CANDIDATES,
    OPTIONAL,
    REQUIRED,
    Pool,
    load_pools,
    quota_snapshot,
    save_report,
    select_pool,
)

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

mcp = MCPServer("gemini-dr")

# Số lần thử lại cho mỗi model khi tạo plan. Server đôi khi trả plan ở lần
# gọi này và không trả ở lần sau, nên cần retry.
_PLAN_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "ERROR", "error": message, **extra}


def _resolve_pool(requested: str) -> tuple[Pool | None, dict[str, Any], list[Pool]]:
    pools = load_pools()
    if not pools:
        return None, {"mode": "none", "chosen": None, "reason": "Chưa có pool nào."}, []
    pool, detail = select_pool(pools, requested=requested)
    return pool, detail, pools


def _sources_of(doc: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in getattr(doc, "sources", None) or []:
        out.append(
            {
                "index": getattr(s, "id", None),
                "title": getattr(s, "title", None),
                "url": getattr(s, "url", None),
            }
        )
    return out


async def _open_client(pool: Pool):
    from gemini_webapi import GeminiClient

    client = GeminiClient(
        secure_1psid=pool.cookies[REQUIRED],
        secure_1psidts=pool.cookies.get(OPTIONAL),
        verbose=False,
    )
    await client.init(auto_refresh=True, refresh_interval=600)
    return client


def _plan_payload(plan: Any) -> dict[str, Any]:
    return {
        "research_id": getattr(plan, "research_id", None),
        "title": getattr(plan, "title", None),
        "query": getattr(plan, "query", None),
        "steps": list(getattr(plan, "steps", []) or []),
        "eta_text": getattr(plan, "eta_text", None),
        "confirm_prompt": getattr(plan, "confirm_prompt", None),
        "modify_prompt": getattr(plan, "modify_prompt", None),
        "cid": getattr(plan, "cid", None),
    }


async def _create_plan(
    client: Any, query: str, model: str | None
) -> tuple[Any, str, Any]:
    """Tạo Deep Research plan, tự dò model nếu chưa biết.

    Hai điều phải xử lý, cả hai đều đã verify trên account thật:

    1. **Model phải hỗ trợ Deep Research.** Model account để mặc định hoặc các
       model "thinking" có thể *từ chối* — trả về câu "tôi chỉ là mô hình ngôn
       ngữ" hoặc output rỗng — thay vì báo lỗi API. Ta thử lần lượt các model
       ứng viên.

    2. **Plan nằm ở frame GIỮA của stream, không phải frame cuối.**
       `generate_content()` chỉ trả frame cuối, nên plan bị bỏ sót và ta tưởng
       là model từ chối. Vì vậy ta dùng `generate_content_stream()` và giữ
       frame bất kỳ nào có plan.

    Ngoài ra server đôi khi trả plan ở lần gọi này và không trả ở lần sau, nên
    mỗi model được thử tối đa `_PLAN_ATTEMPTS` lần.

    Trả về `(plan, model_da_dung, chat_session)`. Chat session phải được giữ lại
    vì bước confirm (`start_deep_research`) là lượt tiếp theo của *cùng* một
    cuộc hội thoại.
    """
    from gemini_webapi.exceptions import GeminiError

    candidates = [model] if model else list(DR_MODEL_CANDIDATES)
    notes: list[str] = []
    last_error = ""

    for candidate in candidates:
        for attempt in range(1, _PLAN_ATTEMPTS + 1):
            chat = client.start_chat(model=candidate) if candidate else client.start_chat()
            plan = None
            text = ""
            try:
                # Quét TOÀN BỘ stream: plan có thể nằm ở frame giữa.
                async for output in client.generate_content_stream(
                    query, chat=chat, deep_research=True
                ):
                    if output.deep_research_plan:
                        plan = output.deep_research_plan
                    if output.text:
                        text = output.text
            except GeminiError as e:
                last_error = str(e)
                notes.append(f"{candidate}#{attempt}: {e}")
                continue
            except Exception as e:  # lỗi mạng / retry hết
                last_error = f"{type(e).__name__}: {e}"
                notes.append(f"{candidate}#{attempt}: {last_error}")
                continue

            if plan:
                plan.metadata = list(chat.metadata)
                plan.cid = chat.cid or plan.cid
                if not plan.confirm_prompt:
                    plan.confirm_prompt = "Start research"
                if not plan.response_text:
                    plan.response_text = text or None
                return plan, candidate or "", chat

            detail = text.strip()[:200] if text.strip() else "output rỗng, không có plan"
            last_error = f"model {candidate!r} lần {attempt}: {detail}"
            notes.append(last_error)

    raise GeminiError(
        "Không model nào tạo được Deep Research plan. "
        f"Thử: {candidates} × {_PLAN_ATTEMPTS} lần. "
        f"Chi tiết: {' | '.join(notes[-6:])}"
    )


def _remember_model(pool: Pool, model: str) -> None:
    """Ghi model đã biết vào file pool để lần sau không phải dò lại."""
    if not model or pool.dr_model == model:
        return
    pool.dr_model = model
    path = pool.source_file
    if not path:
        return
    try:
        import json
        from pathlib import Path

        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        data["dr_model"] = model
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def gemini_dr_account() -> dict[str, Any]:
    """Report which Google account each Deep Research pool belongs to.

    Free and instant: reads the local pool files in `pools/`, no network call
    and no quota is spent.

    Returns the pool name, the account email and the cookie source for each
    configured pool.
    """
    pools = load_pools()
    return {
        "status": "SUCCESS",
        "pools": [
            {
                "name": p.name,
                "account": p.account_email,
                "browser": p.browser,
                "profile": p.profile,
                "usable": p.usable,
                "has_1psidts": bool(p.cookies.get(OPTIONAL)),
                "source_file": p.source_file,
            }
            for p in pools
        ],
        "count": len(pools),
        "note": (
            "Cookie đọc từ file pool cục bộ. Nếu tool báo auth error, hãy export "
            "lại cookie bằng Cookie-Editor (docs/COOKIE-SETUP.md)."
        ),
    }


@mcp.tool()
def gemini_dr_quota(pool: str = "all") -> dict[str, Any]:
    """Report remaining Gemini Advanced allowance per pool.

    Reads the live usage info of each account, so no research quota is spent.
    Check this BEFORE launching a batch of parallel research runs (to size the
    batch) and AGAIN AFTER (to see what the batch cost).

    Args:
        pool: Which pool to read.
            "all" (default) - read every configured pool.
            "windows" - mmeo44244@gmail.com.
            "wsl" - catkingmv@gmail.com.

    Returns per-window remaining credits and remaining percentage for the
    5-hour and weekly windows.
    """
    pools = load_pools()
    if not pools:
        return _error("Chưa có pool nào. Xem docs/COOKIE-SETUP.md.")

    requested = (pool or "all").strip().lower()
    if requested not in ("all", "auto"):
        pools = [p for p in pools if p.name == requested]
        if not pools:
            return _error(f"Không có pool {pool!r}.")

    snapshot = quota_snapshot(pools, force=True)
    return {
        "status": "SUCCESS",
        "pools": snapshot,
    }


@mcp.tool()
async def gemini_dr_plan(query: str, pool: str = "auto", model: str | None = None) -> dict[str, Any]:
    """Create a Deep Research plan WITHOUT starting the research.

    Gemini proposes a plan (title, steps, ETA) and waits for confirmation.
    Use this to review or refine the scope before spending research quota.

    Args:
        query: The research topic or question.
        pool: Which pool to use - "auto" (default), "windows" or "wsl".
        model: Optional model override. Omit to let the bridge pick a model
            that supports Deep Research.

    Returns the plan. Nothing is started; call `gemini_dr_research` to run it.
    """
    if not (query or "").strip():
        return _error("query rỗng.")

    target, detail, _ = _resolve_pool((pool or "auto").strip().lower())
    if target is None:
        return _error("Không chọn được pool.", **detail)

    client = None
    started = time.time()
    try:
        client = await _open_client(target)
        plan, used_model, _chat = await _create_plan(
            client, query, model or target.dr_model
        )
        _remember_model(target, used_model)
        return {
            "status": "SUCCESS",
            "pool": target.name,
            "account": target.account_email,
            "model_used": used_model,
            "elapsed_s": round(time.time() - started, 1),
            "plan": _plan_payload(plan),
            "pool_selection": detail,
            "note": (
                "Plan đã tạo nhưng research CHƯA chạy. Gọi gemini_dr_research "
                "với cùng query để chạy, hoặc dùng modify_prompt để yêu cầu điều chỉnh."
            ),
        }
    except Exception as e:
        return _error(f"{type(e).__name__}: {e}", pool=target.name)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


@mcp.tool()
async def gemini_dr_research(
    query: str,
    pool: str = "auto",
    timeout: int = 900,
    model: str | None = None,
) -> dict[str, Any]:
    """Run a full Gemini Deep Research and return the report.

    This runs on gemini.google.com with a paid Google AI Pro / Gemini Advanced
    session, so the run consumes the Advanced allowance (NOT a separate
    pay-as-you-go API bill).

    The flow is plan → confirm → wait. Reports commonly take 5-10 minutes, so
    the default timeout is 900s. If the timeout hits, the research task keeps
    running server-side - the response says so via `stalled`.

    Args:
        query: The research topic or question.
        pool: Which pool to spend - "auto" (default, picks the one with the
            most remaining allowance), "windows" or "wsl".
        timeout: Seconds to wait for the report (default 900).
        model: Optional model override. Omit to use the account default.

    Returns the report markdown, its sources, and the pool it ran on.
    """
    if not (query or "").strip():
        return _error("query rỗng.")

    target, detail, _ = _resolve_pool((pool or "auto").strip().lower())
    if target is None:
        return _error("Không chọn được pool.", **detail)

    client = None
    started = time.time()
    try:
        client = await _open_client(target)

        plan, used_model, chat = await _create_plan(
            client, query, model or target.dr_model
        )
        _remember_model(target, used_model)

        await client.start_deep_research(plan, chat=chat)
        result = await client.wait_for_deep_research(
            plan, poll_interval=10.0, timeout=float(timeout)
        )

        doc = result.document
        report = result.text or ""
        sources = _sources_of(doc)

        report_path = None
        if report:
            report_path = str(save_report(getattr(doc, "title", None), doc.markdown if doc else report))

        return {
            "status": "SUCCESS",
            "pool": target.name,
            "account": target.account_email,
            "model_used": used_model,
            "elapsed_s": round(time.time() - started, 1),
            "done": result.done,
            "stalled": not result.done,
            "title": getattr(doc, "title", None) if doc else None,
            "plan": _plan_payload(plan),
            "report": report,
            "sources": sources,
            "report_path": report_path,
            "pool_selection": detail,
        }
    except Exception as e:
        return _error(f"{type(e).__name__}: {e}", pool=target.name)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


@mcp.tool()
async def gemini_dr_collect(
    cid: str,
    pool: str = "auto",
    timeout: int = 300,
    poll_interval: int = 15,
) -> dict[str, Any]:
    """Collect the report of an already-running Deep Research task.

    A Deep Research task runs server-side and detached: it keeps going after the
    caller times out or the session ends. This tool re-reads that conversation
    and returns the report once it is rendered.

    Use it when `gemini_dr_research` returned `stalled: true` - it includes the
    `cid` of the conversation in its `plan` payload.

    Args:
        cid: The conversation id, e.g. "c_xxxxxxxxxxxxxxxx".
        pool: Which pool owns the conversation - "auto" (default), "windows" or "wsl".
        timeout: Seconds to keep polling (default 300).
        poll_interval: Seconds between polls (default 15).

    Returns the report markdown and its sources once the task finishes.
    """
    if not (cid or "").strip():
        return _error("cid rỗng.")

    target, detail, _ = _resolve_pool((pool or "auto").strip().lower())
    if target is None:
        return _error("Không chọn được pool.", **detail)

    client = None
    started = time.time()
    try:
        client = await _open_client(target)

        deadline = time.time() + float(timeout)
        doc = None
        polls = 0
        while time.time() < deadline:
            latest = await client.fetch_latest_chat_response(cid, limit=15)
            polls += 1
            candidate = getattr(latest, "deep_research_document", None) if latest else None
            if candidate is not None:
                doc = candidate
                # `ready` is the authoritative flag: a not-yet-rendered report has
                # ready=False and empty content, which is indistinguishable from
                # "no report yet" if you only look at the body length.
                if candidate.ready and candidate.content and candidate.content.strip():
                    break
            await asyncio.sleep(float(poll_interval))

        report = (doc.content or "") if (doc and doc.ready) else ""
        sources = _sources_of(doc)
        report_path = None
        if report:
            report_path = str(
                save_report(getattr(doc, "title", None), doc.markdown if doc else report)
            )

        return {
            "status": "SUCCESS",
            "pool": target.name,
            "cid": cid,
            "elapsed_s": round(time.time() - started, 1),
            "polls": polls,
            "done": bool(report),
            "stalled": not report,
            "title": getattr(doc, "title", None) if doc else None,
            "document_id": getattr(doc, "id", None) if doc else None,
            "report": report,
            "sources": sources,
            "report_path": report_path,
            "pool_selection": detail,
            "note": (
                None
                if report
                else "Report vẫn đang chạy server-side. Gọi lại tool này sau."
            ),
        }
    except Exception as e:
        return _error(f"{type(e).__name__}: {e}", pool=target.name, cid=cid)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


if __name__ == "__main__":
    mcp.run()
