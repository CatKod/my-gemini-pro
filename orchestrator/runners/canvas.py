"""Runner Canvas qua browser thật (Playwright + Chrome profile riêng).

Vì sao cần runner này:
    Gemini Canvas (artifact React tương tác) là tính năng của **web app**
    `gemini.google.com`. `gemini-webapi` không có API canvas — nó chỉ trả
    text, và account chỉ có `gemini-flash-lite` thì không sinh được artifact.

    Nên: điều khiển browser thật, đúng như người dùng.

Profile riêng:
    Runner dùng Chrome profile **do orchestrator tạo** (xem `chrome_profile.py`),
    không đụng profile đang mở → không gây Evil Clone. Người dùng đăng nhập
    1 lần vào profile đó, các lần sau tái sử dụng session.

    Nếu profile chưa đăng nhập → job FAILED với hướng dẫn cụ thể, không
    tự ý đăng nhập (vi phạm ToS / có 2FA).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from orchestrator import store  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PROFILE_DIR = "Profile 2"

GEMINI_URL = "https://gemini.google.com/app"

# Selector — Gemini đổi DOM thường xuyên, nên thử nhiều phương án.
INPUT_SELECTORS = (
    "textarea[placeholder*='Ask']",
    "div[contenteditable='true'][data-placeholder-text]",
    "rich-textarea .ql-editor",
    "textarea[placeholder*='Enter']",
)

CANVAS_BUTTON_SELECTORS = (
    "button[aria-label*='Canvas' i]",
    "button[title*='Canvas' i]",
    "mat-icon-button[aria-label*='Canvas' i]",
    "button:has-text('Canvas')",
)

ANSWER_SELECTORS = (
    "model-message .message-content",
    "message-content",
    ".conversation-turn-container",
    "model-response",
)


def _emit(job_id: str, message: str, *, level: str = "info", kind: str | None = None) -> None:
    store.add_event(job_id, message, level=level, kind=kind)
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _chrome_user_data() -> Path:
    override = os.environ.get("CHROME_USER_DATA")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def _profile_exists(directory: str) -> bool:
    return (_chrome_user_data() / directory).is_dir()


def chrome_exe() -> Path | None:
    for c in (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ):
        if c.is_file():
            return c
    return None


def _work_profile_dir(directory: str) -> Path:
    """Thư mục user-data riêng cho runner.

    Chrome không cho 2 tiến trình dùng chung 1 user-data-dir. Runner tạo bản
    sao nhẹ (chỉ `Default`/`Profile N` + `Local State`) sang `%LOCALAPPDATA%`
    để không đụng session thật của user, đồng thời không phải copy cả profile.
    """
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OrchestratorChrome"
    work = base / directory
    work.mkdir(parents=True, exist_ok=True)

    src = _chrome_user_data()
    for name in ("Local State", directory):
        s = src / name
        d = work / name
        if not s.exists():
            continue
        if s.is_dir():
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(s, d, ignore=shutil.ignore_patterns(
                "Cache", "Code Cache", "GPUCache", "Service Worker",
                "blob_storage", "Network", "Sessions", "IndexedDB",
                "Local Storage", "Extension State", "File System",
            ))
        else:
            shutil.copy2(s, d)
    return work


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------


async def _first_visible(page: Any, selectors: tuple[str, ...]) -> Any:
    """Trả về element đầu tiên tìm thấy và hiển thị."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.wait_for(state="visible", timeout=3000)
                return el
        except Exception:
            continue
    return None


async def _run_browser(
    job_id: str, prompt: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Điều khiển Chrome thật qua CDP.

    Vì sao không `launch_persistent_context`: Playwright bundle Chromium, và
    `launch_persistent_context` với profile Chrome thật hay kẹt (Chrome đã
    cài, version mismatch, `no_viewport` lỗi). Cách ổn định hơn: tự spawn
    Chrome thật với `--remote-debugging-port`, rồi Playwright `connect_over_cdp`.
    """
    from playwright.async_api import async_playwright

    directory = params.get("profile_directory") or DEFAULT_PROFILE_DIR
    if not _profile_exists(directory):
        raise RuntimeError(
            f"Chrome profile {directory!r} chưa tồn tại. "
            "Tạo trước bằng: python -m orchestrator.chrome_profile create --name \"Acc Canvas\" "
            "rồi đăng nhập Google trong profile đó, sau đó chạy lại."
        )

    headless = bool(params.get("headless", False))
    timeout_ms = int(params.get("timeout", 600)) * 1000
    port = int(params.get("debug_port", 9333))

    chrome = chrome_exe()
    if chrome is None:
        raise RuntimeError("Không tìm thấy chrome.exe.")

    user_data = _chrome_user_data()
    # Copy profile sang thư mục riêng: Chrome không cho mở cùng 1 user-data-dir
    # từ 2 tiến trình, và để không đụng profile thật của user.
    work_dir = _work_profile_dir(directory)

    cmd = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={work_dir}",
        f"--profile-directory={directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        GEMINI_URL,
    ]
    if headless:
        cmd.append("--headless=new")

    _emit(job_id, f"Khởi động Chrome (profile {directory}, cdp :{port})...", kind="status")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    try:
        async with async_playwright() as pw:
            browser = None
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    browser = await pw.chromium.connect_over_cdp(
                        f"http://127.0.0.1:{port}"
                    )
                    break
                except Exception:
                    await asyncio.sleep(1)
            if browser is None:
                raise RuntimeError(
                    f"Không kết nối được Chrome qua CDP tại 127.0.0.1:{port}. "
                    "Có thể Chrome đang chạy sẵn — hãy đóng hết Chrome rồi thử lại."
                )

            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                return await _drive_page(job_id, page, prompt, params, directory, timeout_ms)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


async def _drive_page(
    job_id: str,
    page: Any,
    prompt: str,
    params: dict[str, Any],
    directory: str,
    timeout_ms: int,
) -> dict[str, Any]:
    _emit(job_id, f"Mở {GEMINI_URL} (profile {directory})...", kind="status")
    await page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)

    # Check đã đăng nhập chưa.
    signed_out = await page.evaluate(
        """() => {
            const t = (document.body.innerText || '').toLowerCase();
            return t.includes('sign in') || t.includes('đăng nhập');
        }"""
    )
    if signed_out:
        raise RuntimeError(
            f"Profile {directory!r} chưa đăng nhập Google. "
            "Hãy mở Chrome bằng profile đó, đăng nhập, rồi chạy lại job."
        )
    _emit(job_id, "Đã đăng nhập. Bật chế độ Canvas...", kind="status")

    # Bật Canvas.
    btn = await _first_visible(page, CANVAS_BUTTON_SELECTORS)
    if btn is None:
        _emit(job_id, "Không thấy nút Canvas — dùng chat thường.", level="warn", kind="status")
    else:
        await btn.click()
        await page.wait_for_timeout(1500)
        _emit(job_id, "Đã bật chế độ Canvas", kind="status")

    # Gõ prompt.
    box = await _first_visible(page, INPUT_SELECTORS)
    if box is None:
        raise RuntimeError(
            "Không tìm thấy ô nhập của Gemini. DOM có thể đã đổi — "
            "cần cập nhật INPUT_SELECTORS."
        )
    await box.click()
    try:
        await box.fill(prompt)
    except Exception:
        await page.keyboard.type(prompt, delay=20)
    await page.wait_for_timeout(800)
    await page.keyboard.press("Enter")
    _emit(job_id, "Đã gửi prompt, đang chờ Gemini dựng canvas...", kind="status")

    # Chờ câu trả lời ổn định.
    deadline = time.time() + (timeout_ms / 1000)
    text = ""
    last_len = -1
    stable = 0
    while time.time() < deadline:
        await page.wait_for_timeout(3000)
        answer = await _first_visible(page, ANSWER_SELECTORS)
        if answer is not None:
            try:
                text = (await answer.inner_text()).strip()
            except Exception:
                text = ""
        if text and len(text) == last_len:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
        last_len = len(text)
        store.heartbeat(job_id)

    if not text:
        raise RuntimeError("Không lấy được nội dung trả lời từ Gemini.")

    _emit(job_id, f"Nhận {len(text)} ký tự", kind="result")

    canvas_url = None
    for sel in ("a[href*='canvas' i]", "a[href*='artifact' i]"):
        loc = page.locator(sel)
        n = await loc.count()
        for i in range(min(n, 5)):
            href = await loc.nth(i).get_attribute("href")
            if href:
                canvas_url = href
                break
        if canvas_url:
            break

    return {
        "text": text,
        "canvas_url": canvas_url,
        "profile_directory": directory,
        "url": page.url,
    }


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m orchestrator.runners.canvas <job_id>", flush=True)
        return 2
    job_id = sys.argv[1]
    job = store.get_job(job_id)
    if job is None:
        print("job không tồn tại", flush=True)
        return 2
    if not store.claim_job(job_id, pid=os.getpid()):
        print("job đã được claim", flush=True)
        return 3

    _emit(job_id, "Bắt đầu job canvas (browser)", kind="status")
    try:
        import asyncio

        result = asyncio.run(_run_browser(job_id, job["prompt"] or "", job.get("params") or {}))
        store.finish_job(job_id, store.DONE, result=result, external_id=result.get("canvas_url"))
        return 0
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        _emit(job_id, f"LỖI: {detail}", level="error", kind="error")
        store.finish_job(job_id, store.FAILED, error=detail)
        return 1


if __name__ == "__main__":
    sys.exit(main())
