"""Runner: Gemini qua `gemini_webapi` — Deep Research, chat thường, canvas.

Mỗi runner là 1 process con độc lập, chạy detached. Nó:
    1. claim job trong SQLite (QUEUED -> RUNNING)
    2. ghi event liên tục vào DB (để dashboard thấy tiến độ)
    3. kết thúc -> DONE / FAILED, kèm result JSON

Dùng chung cho cả 3 mode vì chúng khác nhau chỉ ở tham số gọi API:
    deep_research -> deep_research=True, có plan + confirm, chờ report
    chat         -> generate_content bình thường, có citations
    canvas       -> chat mode + yêu cầu Gemini tạo Canvas (React artifact),
                    lấy link artifact về
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from orchestrator import store  # noqa: E402

from gemini_pool import (  # noqa: E402
    OPTIONAL,
    REQUIRED,
    load_pools,
    quota_snapshot,
    save_report,
    select_pool,
)

# Cờ bắt buộc cho mỗi mode. `gemini_webapi` chỉ kiểm tra account status khi
# có deep_research/gem/files, nhưng ta luôn muốn biết account còn sống.
DR_POLL_INTERVAL = 15.0
DR_MAX_WAIT = 3600.0


def _emit(job_id: str, message: str, *, level: str = "info", kind: str | None = None) -> None:
    store.add_event(job_id, message, level=level, kind=kind)
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _resolve_pool(requested: str | None):
    pools = load_pools()
    if not pools:
        raise RuntimeError(
            "Chưa có pool nào trong pools/. Xem docs/COOKIE-SETUP.md để tạo pool."
        )
    if requested and requested != "auto":
        pool, detail = select_pool(pools, requested=requested)
    else:
        quota = quota_snapshot([p for p in pools if p.usable])
        pool, detail = select_pool(pools, quota_map=quota, requested="auto")
    if pool is None:
        raise RuntimeError(f"Không chọn được pool: {detail.get('reason')}")
    return pool, detail


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


def _citations_of(output: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in getattr(output, "citations", None) or []:
        out.append(
            {
                "index": getattr(c, "id", None),
                "title": getattr(c, "title", None),
                "url": getattr(c, "url", None),
            }
        )
    return out


async def _open_client(pool: Any):
    from gemini_webapi import GeminiClient

    client = GeminiClient(
        secure_1psid=pool.cookies[REQUIRED],
        secure_1psidts=pool.cookies.get(OPTIONAL),
        verbose=False,
    )
    await client.init(auto_refresh=True, refresh_interval=600)
    return client


# ---------------------------------------------------------------------------
# Deep Research
# ---------------------------------------------------------------------------


async def _create_plan(client: Any, query: str, model: str | None):
    """Tạo DR plan. Dò qua các model ứng viên vì model mặc định có thể từ chối.

    Nguyên tắc (đã verify trên account thật):
      - Model phải hỗ trợ DR, nếu không sẽ trả refusal chứ không báo lỗi.
      - Plan nằm ở frame GIỮA của stream, không phải frame cuối -> phải quét
        toàn bộ stream.
    """
    from gemini_webapi.exceptions import GeminiError
    from gemini_pool import DR_MODEL_CANDIDATES

    candidates = [model] if model else list(DR_MODEL_CANDIDATES)
    notes: list[str] = []

    for candidate in candidates:
        for attempt in range(1, 4):
            chat = client.start_chat(model=candidate) if candidate else client.start_chat()
            plan = None
            text = ""
            try:
                async for output in client.generate_content_stream(
                    query, chat=chat, deep_research=True
                ):
                    if output.deep_research_plan:
                        plan = output.deep_research_plan
                    if output.text:
                        text = output.text
            except GeminiError as e:
                notes.append(f"{candidate}#{attempt}: {e}")
                continue
            except Exception as e:
                notes.append(f"{candidate}#{attempt}: {type(e).__name__}: {e}")
                continue

            if plan:
                plan.metadata = list(chat.metadata)
                plan.cid = chat.cid or plan.cid
                if not plan.confirm_prompt:
                    plan.confirm_prompt = "Start research"
                return plan, candidate or "", chat, notes

            notes.append(f"{candidate}#{attempt}: {text.strip()[:120] or 'output rỗng'}")

    raise GeminiError(
        "Không model nào tạo được DR plan. "
        f"Thử {candidates}. Chi tiết: {' | '.join(notes[-5:])}"
    )


async def run_deep_research(job_id: str, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
    pool, detail = _resolve_pool(params.get("pool"))
    _emit(job_id, f"Pool: {pool.name} ({pool.account_email}) — {detail.get('reason')}", kind="status")
    store.update_job(job_id, pool=pool.name, account=pool.account_email)

    client = None
    try:
        client = await _open_client(pool)
        _emit(job_id, "Đã kết nối Gemini, đang tạo research plan...", kind="status")

        plan, used_model, chat, notes = await _create_plan(
            client, prompt, params.get("model") or pool.dr_model
        )
        store.update_job(job_id, model=used_model)
        _emit(job_id, f"Plan: {plan.title}", kind="plan")
        for i, step in enumerate(plan.steps or [], 1):
            _emit(job_id, f"  bước {i}: {step[:160]}", kind="plan")
        for n in notes:
            _emit(job_id, f"  (thử) {n}", level="warn", kind="plan")

        await client.start_deep_research(plan, chat=chat)
        cid = plan.cid
        store.update_job(job_id, external_id=cid, title=plan.title)
        _emit(job_id, f"Đã dispatch research server-side (cid={cid}). Đang chờ report...", kind="status")

        deadline = time.time() + float(params.get("timeout") or DR_MAX_WAIT)
        last_report = ""
        while time.time() < deadline:
            await asyncio.sleep(DR_POLL_INTERVAL)
            latest = await client.fetch_latest_chat_response(cid, limit=15)
            doc = getattr(latest, "deep_research_document", None) if latest else None
            if doc is None:
                store.heartbeat(job_id)
                continue
            ready = bool(getattr(doc, "ready", False))
            length = len(doc.content or "")
            if ready and length:
                report = doc.content
                _emit(job_id, f"Report xong: {length} ký tự, {len(doc.sources or [])} nguồn", kind="result")
                path = save_report(getattr(doc, "title", None), getattr(doc, "markdown", None) or report)
                store.finish_job(
                    job_id,
                    store.DONE,
                    result={
                        "title": getattr(doc, "title", None),
                        "report": report,
                        "markdown": getattr(doc, "markdown", None),
                        "sources": _sources_of(doc),
                        "document_id": getattr(doc, "id", None),
                        "cid": cid,
                        "pool": pool.name,
                        "model_used": used_model,
                    },
                    report_path=str(path),
                    external_id=cid,
                    title=getattr(doc, "title", None),
                )
                return {"ok": True, "report_path": str(path)}
            marker = f"len={length} ready={ready}"
            if marker != last_report:
                last_report = marker
                _emit(job_id, f"Đang nghiên cứu... ({marker})", kind="status")
            store.heartbeat(job_id)

        raise TimeoutError(
            f"Quá thời gian chờ ({params.get('timeout') or DR_MAX_WAIT}s). "
            f"Task vẫn chạy server-side — dùng orch_result(cid={cid}) để lấy sau."
        )
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Chat thường + Canvas
# ---------------------------------------------------------------------------

CANVAS_HINT = (
    "Hãy trả lời bằng Canvas (một artifact React có thể tương tác), "
    "không phải văn bản thuần."
)

# Gemini Canvas trả về artifact dạng code block `react`/`jsx`/`tsx` hoặc link
# trỏ tới canvas đã lưu. `gemini_webapi` không có API canvas riêng, nên ta
# dò trong text trả về.
_CANVAS_LINK_RE = re.compile(
    r"https?://[^\s\"'<>)]*canvas[^\s\"'<>)]*", re.IGNORECASE
)
_ARTIFACT_LANGS = ("react", "jsx", "tsx", "html")


def _extract_canvas(text: str) -> dict[str, Any]:
    """Tìm artifact canvas trong text trả về của Gemini.

    Trả về `{"canvas_url": str|None, "artifact_code": str|None,
    "artifact_lang": str|None}`.
    """
    out: dict[str, Any] = {
        "canvas_url": None,
        "artifact_code": None,
        "artifact_lang": None,
    }
    if not text:
        return out

    m = _CANVAS_LINK_RE.search(text)
    if m:
        out["canvas_url"] = m.group(0).rstrip(".,;\"'")

    # Code block ```react ... ``` (hoặc jsx/tsx/html)
    for lang in _ARTIFACT_LANGS:
        pattern = re.compile(
            rf"```{re.escape(lang)}\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
        )
        cm = pattern.search(text)
        if cm:
            out["artifact_code"] = cm.group(1)
            out["artifact_lang"] = lang.lower()
            break
    else:
        # Code block không ghi nhãn nhưng chứa JSX.
        generic = re.compile(r"```(?:[a-z]*)\s*\n(.*?)```", re.DOTALL)
        for cm in generic.finditer(text):
            body = cm.group(1)
            if "<" in body and "/" in body and ("function" in body or "const" in body):
                out["artifact_code"] = body
                out["artifact_lang"] = "unknown"
                break

    return out


async def run_chat(job_id: str, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
    """Chat thường với Gemini, không chờ plan, không cần confirm."""
    pool, detail = _resolve_pool(params.get("pool"))
    _emit(job_id, f"Pool: {pool.name} ({pool.account_email})", kind="status")
    store.update_job(job_id, pool=pool.name, account=pool.account_email)

    client = None
    try:
        client = await _open_client(pool)
        model = params.get("model")
        chat = client.start_chat(model=model) if model else client.start_chat()
        _emit(job_id, f"Đang gửi chat (model={model or 'default'})...", kind="status")

        text_parts: list[str] = []
        thoughts = ""
        last_text = ""
        async for output in client.generate_content_stream(
            prompt,
            chat=chat,
            extended_thinking=bool(params.get("extended_thinking")),
        ):
            # `output.text` là CUMULATIVE trong stream mode; `text_delta` mới là
            # phần mới. Nối `text` sẽ ra "PONG"+"PONG"+"PONG".
            delta = getattr(output, "text_delta", None)
            if delta:
                text_parts.append(delta)
            elif output.text:
                last_text = output.text
            if getattr(output, "thoughts", None):
                thoughts = output.thoughts
            store.heartbeat(job_id)

        text = "".join(text_parts) or last_text
        citations = _citations_of(output) if output else []
        _emit(job_id, f"Nhận {len(text)} ký tự, {len(citations)} citations", kind="result")

        result: dict[str, Any] = {
            "text": text,
            "citations": citations,
            "cid": getattr(chat, "cid", None),
            "pool": pool.name,
            "model_used": model,
        }
        if thoughts:
            result["thoughts"] = thoughts

        report_path = None
        if params.get("save_text") and text:
            path = save_report(params.get("title") or "chat", text)
            report_path = str(path)
            result["report_path"] = report_path

        store.finish_job(
            job_id,
            store.DONE,
            result=result,
            report_path=report_path,
            external_id=getattr(chat, "cid", None),
        )
        return {"ok": True}
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def run_canvas(job_id: str, prompt: str, params: dict[str, Any]) -> dict[str, Any]:
    """Canvas: chat + yêu cầu Gemini dựng artifact, rồi lấy link canvas.

    Gemini Canvas là một tính năng của gemini.google.com. `gemini_webapi` chưa
    có API riêng cho canvas, nên ta dùng đường chat: yêu cầu model tạo artifact
    và đọc link canvas nếu model trả về.
    """
    pool, detail = _resolve_pool(params.get("pool"))
    _emit(job_id, f"Pool: {pool.name} ({pool.account_email})", kind="status")
    store.update_job(job_id, pool=pool.name, account=pool.account_email)

    client = None
    try:
        client = await _open_client(pool)
        model = params.get("model")
        chat = client.start_chat(model=model) if model else client.start_chat()

        full_prompt = prompt
        if params.get("force_canvas", True):
            full_prompt = f"{prompt}\n\n{CANVAS_HINT}"

        _emit(job_id, "Đang yêu cầu Gemini tạo Canvas...", kind="status")
        text_parts: list[str] = []
        last_text = ""
        async for output in client.generate_content_stream(full_prompt, chat=chat):
            delta = getattr(output, "text_delta", None)
            if delta:
                text_parts.append(delta)
            elif output.text:
                last_text = output.text
            store.heartbeat(job_id)

        text = "".join(text_parts) or last_text
        art = _extract_canvas(text)

        # Lưu code artifact ra file để user có thể mở/sửa.
        artifact_path = None
        if art.get("artifact_code"):
            ext = {"react": "jsx", "jsx": "jsx", "tsx": "tsx", "html": "html"}.get(
                art.get("artifact_lang") or "", "txt"
            )
            reports = Path(__file__).resolve().parent.parent.parent / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            artifact_path = reports / f"{stamp}-canvas.{ext}"
            artifact_path.write_text(art["artifact_code"], encoding="utf-8")

        _emit(
            job_id,
            f"Canvas xong: {len(text)} ký tự"
            + (
                f", link={art['canvas_url']}"
                if art.get("canvas_url")
                else ""
            )
            + (
                f", code={artifact_path.name}"
                if artifact_path
                else " (không tìm thấy artifact)"
            ),
            kind="result",
        )
        store.finish_job(
            job_id,
            store.DONE,
            result={
                "text": text,
                "canvas_url": art.get("canvas_url"),
                "artifact_code": art.get("artifact_code"),
                "artifact_lang": art.get("artifact_lang"),
                "artifact_path": str(artifact_path) if artifact_path else None,
                "cid": getattr(chat, "cid", None),
                "pool": pool.name,
                "model_used": model,
            },
            report_path=str(artifact_path) if artifact_path else None,
            external_id=getattr(chat, "cid", None),
        )
        return {"ok": True}
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

RUNNERS = {
    "deep_research": run_deep_research,
    "chat": run_chat,
    "canvas": run_canvas,
    "canvas_api": run_canvas,
}


async def _main(job_id: str) -> int:
    job = store.get_job(job_id)
    if job is None:
        print(f"job {job_id} không tồn tại", flush=True)
        return 2

    if not store.claim_job(job_id, pid=os.getpid()):
        print(f"job {job_id} đã được claim", flush=True)
        return 3

    kind = job["kind"]
    runner = RUNNERS.get(kind)
    if runner is None:
        store.finish_job(job_id, store.FAILED, error=f"Không có runner cho kind={kind!r}")
        return 4

    _emit(job_id, f"Bắt đầu job {kind}", kind="status")
    try:
        await runner(job_id, job["prompt"] or "", job.get("params") or {})
        return 0
    except Exception as e:
        import traceback

        detail = f"{type(e).__name__}: {e}"
        _emit(job_id, f"LỖI: {detail}", level="error", kind="error")
        for line in traceback.format_exc().splitlines()[-8:]:
            _emit(job_id, line, level="error", kind="error")
        store.finish_job(job_id, store.FAILED, error=detail)
        return 1


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m orchestrator.runners.gemini <job_id>", flush=True)
        return 2
    return asyncio.run(_main(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
