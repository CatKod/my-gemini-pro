# Orchestrator — kiến trúc

## Vấn đề

Agent (Cursor) gọi Gemini Deep Research hoặc Antigravity CLI bằng tool call →
tool call **chặn** cho tới khi task xong (30-60 phút với DR). Trong lúc đó
agent không làm được việc gì khác, và user không biết Gemini đang làm gì.

## Giải pháp

Tách "chạy task" ra khỏi "chờ kết quả".

```
┌──────────────┐  orch_submit   ┌──────────────┐   spawn   ┌─────────────────┐
│  MCP Server  │ ─────────────▶ │  SQLite WAL  │ ─────────▶ │  runner process │
│  (stdio)     │                │  jobs/events │            │  (detached)     │
└──────────────┘                └──────────────┘ ◀───────── └─────────────────┘
       ▲                              ▲                    ghi event + result
       │  orch_result                 │
┌──────┴───────┐                      │
│    Agent     │ ─────────────────────┘
└──────────────┘
```

Ba điều kiện để không phải ngồi canh:

1. **Detach thật** — runner là process con với `DETACHED_PROCESS |
   CREATE_NEW_PROCESS_GROUP` (Windows) / `start_new_session=True` (POSIX).
   Nó sống độc lập, MCP server có thể tắt.
2. **State bền vững** — kết quả nằm trong SQLite, không phải trong stdout
   của process đã chết. Đọc lại bất kỳ lúc nào, từ process nào.
3. **Wake-up của Cursor** — khi agent thực sự cần biết kết quả, nó chạy
   `python -m orchestrator.orch_cli watch <job_id>` **với `block_until_ms: 0`**.
   Khi tiến trình kết thúc, Cursor tự inject `<system_notification>` và
   đánh thức agent. Không cần daemon, không cần hook.

## Thành phần

| File | Vai trò |
|---|---|
| `orchestrator/store.py` | SQLite job store + event log (WAL, multi-process safe) |
| `orchestrator/dispatch.py` | Submit / spawn / wait / cancel |
| `orchestrator/orch_mcp.py` | MCP server (stdio) — cổng gọi của agent |
| `orchestrator/orch_cli.py` | CLI — dùng cho shell background (`watch`) và script |
| `orchestrator/gui.py` | Tkinter dashboard (tuỳ chọn, không bắt buộc) |
| `orchestrator/chrome_profile.py` | Tạo hồ sơ Chrome cho multi-account |
| `orchestrator/cdp.py` | CDP client tối giểu (websocket-client thuần Python) |
| `orchestrator/cookie_sync.py` | Tự động lấy/cập nhật cookie Gemini (không cần Cookie-Editor) |
| `orchestrator/runners/gemini.py` | Runner Deep Research / chat / canvas |
| `orchestrator/runners/agy.py` | Runner Antigravity CLI (NDJSON stream) |

## Luồng job

```
QUEUED ──claim_job()──▶ RUNNING ──▶ DONE | FAILED | CANCELLED
```

`claim_job` là UPDATE có điều kiện `WHERE state = 'QUEUED'` → an toàn khi
2 process cùng tranh job.

## Runner Gemini (`runners/gemini.py`)

### Deep Research

1. Chọn pool (auto = headroom quota cao nhất).
2. `generate_content_stream(query, deep_research=True)` — quét **toàn bộ** stream
   vì plan nằm ở frame giữa, không phải frame cuối.
3. Dò qua các model ứng viên (`DR_MODEL_CANDIDATES`) vì model account để mặc
   định có thể *từ chối* DR (refusal, không phải lỗi API).
4. `start_deep_research(plan, chat=chat)` → dispatch server-side.
5. Poll `fetch_latest_chat_response(cid)` mỗi 15s, chờ
   `deep_research_document.ready == True` **và** content khác rỗng.
   (`ready` là cờ authoritative — content rỗng KHÔNG phải là hoàn tất.)
6. Lưu report vào `reports/`, kết thúc job.

### Chat

`generate_content_stream` bình thường. Lưu ý: `output.text` trong stream là
**cumulative**, phải dùng `output.text_delta` để nối, nếu không sẽ ra
"PONG"+"PONG"+"PONG".

### Canvas

Hai đường, vì `gemini-webapi` **không có API canvas** (chỉ có hằng số
`GRPC.LIST_GEMINI_APP_ARTIFACTS` chưa dùng):

| Đường | Cách hoạc động | Khi nào dùng |
|---|---|---|
| `canvas` | Điều khiển **Chrome thật** qua CDP (Playwright `connect_over_cdp`) | Mặc định — sinh được artifact thật |
| `canvas_api` | `gemini_webapi` chat + prompt yêu cầu artifact | Account chỉ có `gemini-flash-lite` → không sinh code được |

Runner browser (`runners/canvas.py`):
1. Copy profile sang `%LOCALAPPDATA%\OrchestratorChrome\<name>` (không đụng
   session thật của user).
2. Spawn Chrome thật với `--remote-debugging-port=<port>`.
3. `connect_over_cdp` → mở `gemini.google.com/app`.
4. Kiểm tra đã đăng nhập chưa → nếu chưa thì **fail với hướng dẫn cụ thể**
   (không tự ý đăng nhập — có 2FA, và vi phạm ToS).
5. Bật nút Canvas → gõ prompt → Enter.
6. Chờ nội dung ổn định 3 vòng liên tiếp → coi là xong.

> Vì sao không `launch_persistent_context`: Playwright bundle Chromium, dùng
> profile Chrome thật hay kẹt. Tự spawn Chrome + CDP ổn định hơn.

## Runner Antigravity (`runners/agy.py`)

`agy --output-format stream-json` xuất NDJSON, 1 event/dòng:

| event | Ý nghĩa |
|---|---|
| `init` | `conversation_id`, `cwd`, `permission_mode`, danh sách tools |
| `step_update` | `step_index`, `state`, `step_type`, `text_delta` (streaming) |
| `result` | `status`, `response`, `duration_seconds`, `usage` |

Runner đọc từng dòng ngay khi tới → event vào DB → dashboard thấy tiến độ
real-time. `step_type` hữu ích: `user_input`, `agent_response`, `error_message`,
`tool_call`.

## Đóng gói `.exe` — kết quả benchmark thật

Đo trên máy này (Python 3.14.8, MSVC 18 Community):

| Công cụ | Cấu hình | Dung lượng | Startup median |
|---|---|---|---|
| **PyInstaller onedir** | console | 68 MB (cả thư mục) | **161 ms** ✅ |
| **PyInstaller onedir** | GUI (Tkinter) | 74 MB | ~200 ms |
| PyInstaller onefile | console | 26,9 MB | 3.309 ms (giải nén temp mỗi lần) |
| Nuitka onedir | no-GUI | 121,5 MB | 1.040 ms (build ~6 phút) |

**Chọn PyInstaller onedir.** Lý do:
- Startup thực tế nhanh nhất (161 ms vs 1040 ms của Nuitka).
- Build nhanh hơn nhiều (~80 s vs ~6 phút).
- Nuitka **không** dùng được cho bản GUI: Python 3.14 nhúng Tcl qua `zipfs://`
  bên trong `python314.dll`, khiến plugin `tk-inter` của Nuitka fail với
  `FATAL: Could not find Tcl`.
- Nuitka cũng không dùng được với MinGW trên Python ≥3.13
  (`FATAL: cannot use '--mingw64'`); MSVC 18 thì OK.

## Hồ sơ Chrome (`chrome_profile.py`)

Vì sao cần: mỗi acc Google cần 1 hồ sơ riêng để đăng nhập song song mà
không kick nhau (Evil Clone), và để export cookie đúng hồ sơ.

Cơ chế:
1. Đọc `%LOCALAPPDATA%\Google\Chrome\User Data\Local State`.
2. Chọn `Profile N` chưa dùng.
3. Tạo thư mục + `Preferences` tối thiểu.
4. Ghi `profile.info_cache` + `profiles_order` + `profiles_created`.
5. Mở Chrome `--profile-directory="<tên>"`.

**Chrome phải đang đóng** khi tạo, nếu không Chrome ghi đè `Local State` khi
thoát và hồ sơ vừa tạo biến mất.

Module này **không** đăng nhập, không xác thực — phần đó là của người dùng.

## Cookie tự động (`cookie_sync.py`)

Vấn đề: cookie Gemini phải export tay bằng Cookie-Editor mỗi khi session hết
hạn. Điều tra thực nghiệm cho thấy 6 hướng đều không khả thi:

| Hướng | Kết quả |
|---|---|
| Đọc `Network/Cookies` ngoài tiến trình | Không giải mã được (app-bound `v20`) |
| **Copy** profile → user-data-dir khác | Chrome **xoá cookie** (65 → 0) — Device Bound Session không khớp |
| `--remote-debugging-port` + user-data-dir default | Chrome ≥136 **từ chối** mở port |
| `--remote-debugging-port` + short path 8.3 | Port mở được, cookie vẫn không giải mã |
| `--remote-debugging-pipe` | Windows cần handle riêng, `stdin` không dùng được |
| Chrome riêng + **cấy cookie qua CDP** | ✅ thành công |

Nguyên tắc: Google bind session vào *đúng user-data-dir nơi nó được tạo* →
phải làm việc trong chính Chrome đó, đừng copy đi.

Kiến trúc:

```
%LOCALAPPDATA%\OrchestratorChrome\<account>\   ← Chrome RIÊNG của orchestrator
        │
        ├─ adopt (1 lần/account): cấy cookie từ file export Cookie-Editor
        │     → Chrome ghi lại cookie kèm DBSC key CỦA profile này
        │     → browser đăng nhập thật (verify: thấy "Thư viện", lịch sử chat)
        │
        ├─ sync (mỗi lần): mở Chrome → Storage.getCookies → ghi pools/<name>.json
        │
        └─ đóng bằng CDP Browser.close (graceful) → Chrome flush cookie vào DB
```

Hai điểm dễ sai đã verify bằng script thật:

1. **Phải đóng graceful.** `terminate()` cứng → Chrome không flush session
   cookie → lần sau mở ra trống. Phải dùng CDP `Browser.close` (exit code 0).
2. **Profile của user không bao giờ bị đụng.** Orchestrator dùng user-data-dir
   riêng → không có rủi ro Evil Clone, không cần đóng Chrome của bạn.

Lệnh:

```powershell
python -m orchestrator.cookie_sync login  --account mmeo          # mở Chrome để đăng nhập
python -m orchestrator.cookie_sync adopt --account mmeo --export <f.json>
python -m orchestrator.cookie_sync sync  --account mmeo --pool windows
python -m orchestrator.cookie_sync status --account mmeo
python -m orchestrator.orch_cli cookie sync --account mmeo --pool windows
```

MCP: `orch_cookie_sync(action="sync"|"adopt"|"status"|"login", account=..., pool=...)`.

### Vì sao dùng CDP client thuần thay vì Playwright

Playwright kéo theo driver Node (~109 MB) → bản `.exe` phình gấp đôi. Nhưng
`cookie_sync` chỉ cần 4 lệnh CDP (`Storage.getCookies`, `Network.setCookie`,
`Page.navigate`, `Runtime.evaluate`) → tự implement bằng `websocket-client`
(thuần Python). Bản `.exe` giữ nguyên ~68 MB, startup ~155 ms.

## Ranh giới

- KHÔNG dùng AI Studio Interactions API (tính phí pay-as-you-go riêng).
- KHÔNG đọc cookie từ tiến trình Chrome đang chạy (app-bound encryption).
- KHÔNG copy cookie từ profile đang mở (rủi ro Evil Clone).
- KHÔNG copy profile Chrome của user (Chrome sẽ xoá cookie do Device Bound
  Session) — dùng Chrome riêng của orchestrator.
- KHÔNG tự đăng nhập / bypass 2FA.
- KHÔNG commit `pools/` hay `reports/`.
- GUI là *view* của store — đóng GUI không ảnh hưởng job đang chạy.
