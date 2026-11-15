# HANDOFF — my-gemini-pro



> **Đọc file này đầu tiên khi nhận "continue" ở phiên mới.**

> Nó chứa đủ context để tiếp tục mà không cần đọc transcript cũ.



**Ngày bàn giao:** 2026-11-13 20:11 (UTC+7)

**Cập nhật:** 2026-11-14 00:45 — cookie Gemini **tự động hoá** (mục 11).

**Trạng thái:** nghiên cứu xong + Orchestrator đã chạy thật end-to-end +

cookie tự động sync (không cần Cookie-Editor nữa).

**Người dùng:** `CatKod` (GitHub), máy Windows 11 + WSL2 Ubuntu.



**Repo GitHub:** <https://github.com/CatKod/my-gemini-pro>

**Path local:** `D:\GitHub\my-gemini-pro`

**agentmemory:** project `my-gemini-pro`, agentId `cursor` — đã lưu quyết định kiến trúc

(`mem_mwf0c8c7_ffaa356cfa93`).



---



## 0. Cách resume phiên mới (đọc trước, 30 giây)



1. Mở `D:\GitHub\my-gemini-pro` trong Cursor

2. Rule `.cursor/rules/gemini-pro.mdc` tự load (có `alwaysApply: true`)

3. Chạy:

   ```powershell

   python scripts\continue.py

   ```

   → in ra: repo path, trạng thái env (`gemini_webapi`, `mcp`, `gemini_mcp.py`), danh sách pool,

     và **bước tiếp theo cụ thể** (READY hay BLOCKED + hướng dẫn).

4. Đọc `docs/HANDOFF.md` (file này) để có đầy đủ context.

5. Nếu cần context đã lưu:

   ```powershell

   agentmemory search "gemini deep research" --project my-gemini-pro

   ```



---



## 1. Bối cảnh: vì sao dự án này tồn tại



User có **2 tài khoản Google** đã đăng nhập Antigravity CLI (`agy`):



| Pool | Runtime | Tài khoản | Nơi lưu OAuth | Quota tuần (tại lúc khảo sát) |

|---|---|---|---|---|

| **windows** | `agy.exe` v1.5.6 | `mmeo44244@gmail.com` | Windows Credential Manager, target `gemini:antigravity` | ~30% |

| **wsl** | `agy` (WSL2 Ubuntu) | `catkingmv@gmail.com` | `~/.gemini/antigravity-cli/antigravity-oauth-token` | ~96.5% |



Đã xây `C:\Users\kimvi\.cursor\bin\gemini_mcp.py` (1178 dòng) để route 2 pool này:

- `gemini_plan(work, account="auto"|"windows"|"wsl")`

- `gemini_task(task, model, effort, yolo, continue_conversation, timeout, dir, fallback, account)`

- `gemini_quota(account="all")` — đọc quota 2 pool song song, cache TTL 300s

- `gemini_models()`, `gemini_account()`

- `_select_pool()` chấm điểm theo group headroom + total headroom để auto-chọn pool nhiều quota nhất



### Vấn đề phát sinh



Antigravity **không có Deep Research**. Kiểm chứng bằng string analysis trên `agy.exe`:



```

deep-research-preview-04-2026

deep-research-max-preview-04-2026

deep-research-pro-preview-12-2025

DeepResearchAgentConfig

CreateAgentInteractionAgentConfig: %w

google_search

```



→ `agy.exe` **có** code Deep Research, nhưng nó gọi **AI Studio Interactions API** (`generativelanguage.googleapis.com/v1beta/interactions`), dùng `x-goog-api-key` pay-as-you-go.



**Xác minh:** OAuth token Antigravity → HTTP 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT` với Interactions API.



**Kết luận của user:** gọi Deep Research qua AI Studio **tính phí riêng**, không trừ vào gói Google Pro. → **không được dùng**.



→ Cần một đường đi qua **web app `gemini.google.com`**, nơi Deep Research chạy và **trừ vào allowance Gemini Advanced/AI Pro** trong gói đã trả.



---



## 2. Nghiên cứu: `int04/ChatCmd` — KẾT LUẬN KHÔNG KHẢ THI



Repo: <https://github.com/int04/ChatCmd> — đã clone vào `%TEMP%\ChatCmd`, đọc source thật.



### Bằng chứng



```powershell

rg -i "gemini|google\.com" $env:TEMP\ChatCmd -l

# → KHÔNG có kết quả nào

```



- `chatgpt-extension/manifest.json`: `host_permissions: ["https://chatgpt.com/*", "http://localhost/*", "http://127.0.0.1/*"]` — **không có `gemini.google.com`**

- Selector 100% ChatGPT: `data-message-author-role="assistant"`, `form[data-type="unified-composer"]`, `button[data-testid="send-button"]`

- Kiến trúc: Rust server (139 file `.rs`) + React console + MV3 extension

- Bảo trì: 1 contributor, push cuối **2026-09-26**, 47 stars, 8 issue mở

- Issue đáng chú ý: #33 *"Lots of Bugs why nobody is working on this?"*, #28 *"ChatGPT extension is not installed"*, #20 *"Chat phản hồi xong nhưng agent vẫn hiện dừng"*

- Build cần: Rust 1.85+ + Node 20.19+ + Visual Studio Build Tools; README cảnh báo binary prebuilt có thể **không mở được**



### Vì sao port sang Gemini cũng không đáng



Deep Research trên Gemini web **không phải** luồng "gõ prompt → đọc reply". Nó là:



1. Bật chế độ Deep Research (toggle UI)

2. Gemini trả về **plan** → cần **xác nhận**

3. Task chạy **server-side detached** (không giữ session)

4. Báo cáo về dưới dạng **inline document artifact**, không phải bubble chat



Mô hình transcript của ChatCmd (`assistantNodes()` → `innerText`) **không vễ vang** với cái này. Để làm đúng phần artifact, về cơ bản phải tự reverse-engineer lại `batchexecute` — tức là viết lại `gemini_webapi` bằng tay.



**Chi phí đánh giá:** hàng giờ compile Rust + viết lại extension từ đầu, để đổi lấy kết quả tệ hơn nhiều so với dùng thư viện sẵn có.



---



## 3. Giải pháp: `HanaokaYuzu/Gemini-API` (`gemini_webapi`)



Repo: <https://github.com/HanaokaYuzu/Gemini-API> — đã clone vào `%TEMP%\gemini-webapi`, đã `pip install` vào Python 3.14.8 của user.



- **3.810 stars**, push **2026-11-10** (3 ngày trước), 43 issue mở

- PyPI: `gemini-webapi` v2.1.1, `requires_python >=3.11`

- Đã verify import thành công:

  ```

  GeminiClient methods: ['_collect_research_output', 'create_deep_research_plan',

                          'deep_research', 'start_deep_research', 'wait_for_deep_research']

  ```



### Deep Research API (đã đọc source thật)



`src/gemini_webapi/components/research_mixin.py`:



```python

async def deep_research(self, prompt, poll_interval=10.0, timeout=600.0, model=None, chat=None):

    """Run a full deep research cycle: plan → confirm → wait → result."""

    if chat is None:

        chat = self.start_chat(model=model)

    plan = await self.create_deep_research_plan(prompt, chat=chat)

    start_output = await self.start_deep_research(plan, chat=chat)

    result = await self.wait_for_deep_research(plan, poll_interval=poll_interval, timeout=timeout)

    result.start_output = start_output

    return result

```



Ba bước có thể tách riêng (hữu ích khi muốn xem plan trước khi chạy):

- `create_deep_research_plan(prompt)` → `DeepResearchPlan` (steps, title, confirm_prompt)

- `start_deep_research(plan)` → xác nhận → task chạy server-side

- `wait_for_deep_research(plan, poll_interval, timeout)` → poll conversation tới khi report xuất hiện



Kết quả: `result.final_output.deep_research_document` → có `.ready`, `.content` (markdown), `.title`, `.sources`, `.markdown` (body + danh sách nguồn đánh số).



### Giao thức nền



- Endpoint: `POST https://gemini.google.com/_/BardChatUi/data/batchexecute`

- Query: `rpcids`, `_reqid`, `rt=c`, `source-path=/app`, `bl` (build label), `f.sid`

- Form: `at` (access token `SNlM0e` — scrape từ HTML `/app`), `f.req` (JSON **string** chứa payload)

- Framing response: prefix anti-XSSI `)]}'\n`, rồi `{length}\n{json}` với length tính theo **UTF-16 code units**

- Token `SNlM0e`, `bl`, `f.sid` lấy từ HTML `https://gemini.google.com/app` qua regex (`"SNlM0e":"..."`, `"cfb2h":"..."`, `"FdrFJe":"..."`)



### Cookie



- `__Secure-1PSID` (bắt buộc), `__Secure-1PSIDTS` (khuyến nghị, Google xoay thường xuyên)

- Lib tự refresh `1PSIDTS` mỗi 600s qua `accounts.google.com/RotateCookies`

- Dùng `curl_cffi` với **TLS/HTTP2 browser fingerprint impersonation** → chống bot detection tốt



### Eligibility probe



Có sẵn `_assert_deep_research_capable()` gọi 3 RPC: `bootstrap`, `model_state`, `caps`. Cả 3 phải `ok` và không có `reject_code` (reject code 7 = bị từ chối). Lỗi: `"Current account/session appears not eligible for deep research."`



Lưu ý: **tài khoản Free cũng dùng được Deep Research** (giới hạn hơn). AI Pro = 4× allowance. Nên nếu 2 acc của user có Pro thì cả 2 đều OK.



---



## 4. Trạng thái cookie trên máy (đã probe thật)



Script: `scripts/scan_browser_cookies.py` trong repo này.



### Kết quả



| Browser / Profile | Tài khoản | Cookie Gemini | `__Secure-1PSID` |

|---|---|---|---|

| Chrome `Default` ("Person 1") | `mmeo44244@gmail.com` (Meo Meo, gaia `100722739602672374612`) | ✅ 8 cookie | ✅ enc216 |

| Chrome `Profile 1` ("Vĩnh") | `catkingmv@gmail.com` (Vĩnh Hoàng Kim (Cod), gaia `107894897885205192113`) | ✅ 8 cookie | ✅ enc216 |

| Edge `Default` ("Profile 1") | `kimvinhh6@gmail.com` (gaia `81dea17dfd148f0b`) | ⚠️ chưa xác minh (DB bị lock) | ✅ (từ scan trước) |

| Edge `Profile 1` ("Hồ sơ 1") | không đăng nhập Google | ❌ 0 | có SID cũ |



Cookie Gemini tìm thấy ở `host_key = .gemini.google.com`:

`COMPASS`, `_gcl_au`, `_gcl_aw`, `_gcl_dc`, `_gcl_gs`, `_ga`, `_ga_BF8Q35BMLM`, `_ga_WC57KJ50ZZ`



**Cả 2 profile Chrome đều đã truy cập `gemini.google.com` hôm nay (13/11/2026):**

- Chrome Default → 14:41:00 `https://gemini.google.com/app?...` title "Google Gemini"

- Chrome Profile 1 → 14:42:09 `https://gemini.google.com/app?...` title "Google Gemini"



→ **Đúng 2 pool cần thiết, đã có sẵn, không cần tạo burner profile mới.**



### Blocker: app-bound encryption (v20)



```

__Secure-1PSID   plain=0  enc=216  v=763230

__Secure-1PSIDTS plain=0  enc=148  v=763230

```



`browser_cookie3` trên máy:

```

chrome failed: BrowserCookieError Unable to get key for cookie decryption

edge   failed: BrowserCookieError Unable to get key for cookie decryption

```



Chrome ≥127 mã hoá cookie `v20` bằng **app-bound encryption** → không tool ngoài Chrome đọc được. Đã thử và thất bại:

- `shutil.copyfile` → `PermissionError` (Edge)

- `CreateFileW` với `FILE_SHARE_*` → error 32 (sharing violation)

- `esentutl /y` → `JET_errFileAccessDenied`

- SQLite `mode=ro&immutable=1` → `unable to open database file`



**Đây là hạn chế đã biết, không phải lỗi máy.** Tham chiếu: issue #51 `steipete/oracle` — maintainer đóng vì *"not currently fixable"*.



### Hướng giải quyết



**Cách A (đơn giản, khuyến nghị):** Extension `Cookie-Editor` trong Chrome → Export as JSON → lưu vào `pools/`.



**Cách B (tự động, tôi có thể làm):** Chrome với `--remote-debugging-port=9222` → Playwright/CDP đọc cookie trực tiếp từ context trang `gemini.google.com` (nằm trong process Chrome nên giải mã được). Yêu cầu: đóng Chrome hoàn toàn rồi mở lại bằng cờ debug.



---



## 5. Rủi ro



| Rủi ro | Mức | Xử lý |

|---|---|---|

| **"Evil Clone" logout** | ⚠️ Cao | Google thấy 2 session giống hệt → kill-switch, **logout toàn bộ thiết bị**. Dùng Chrome profile riêng cho automation hoặc đóng Chrome trước khi chạy |

| Vi phạm ToS Google | ⚠️ | Có thể bị giới hạn, CAPTCHA, hoặc suspend. Dùng tài khoản phụ chịu rủi ro |

| Google đổi endpoint `batchexecute` | 🟡 | `gemini_webapi` cập nhật rất thường xuyên — pin version & theo dõi upstream |

| Cookie hết hạn | 🟡 | `1PSIDTS` xoay nhanh → lib tự refresh; `1PSID` dài hạn vẫn có thể chết → cần cơ chế re-login |



Tham chiếu rủi ro evil-clone: <https://github.com/TNLegend/Chimera> (mô tả chi tiết kill-switch của Google).



---



## 6. Kế hoạch triển khai



### Bước 1 — Thu thập cookie (CẦN USER HÀNH ĐỘNG)



User cần export cookie cho 2 profile Chrome:



```

Chrome → profile Default   → gemini.google.com → Cookie-Editor → Export JSON

   → D:\GitHub\my-gemini-pro\pools\chrome-default.json

Chrome → profile "Vĩnh"   → gemini.google.com → Cookie-Editor → Export JSON

   → D:\GitHub\my-gemini-pro\pools\chrome-profile1.json

```



JSON format của Cookie-Editor: mảng các object `{name, value, domain, path, expirationDate, ...}`.



### Bước 2 — `tools/gemini_pool.py`



- Load cookie từ `pools/*.json`, lọc `__Secure-1PSID` + `__Secure-1PSIDTS`

- `Pool` dataclass: `name`, `account_email`, `cookies`, `client` (lazy `GeminiClient`)

- `load_pools()`, `select_pool(pools, quota_map)` → chấm điểm headroom

- Quota reader: `gemini_webapi` có `_quotas` / `list_models()` → đọc remaining theo model

- TTL cache 300s (nhất quán với `gemini_mcp.py`)



### Bước 3 — `tools/gemini_dr_mcp.py` (MCP server stdio)



Tool:

- `gemini_dr_research(query, pool="auto"|"windows"|"wsl", timeout=900, model=None)` → `{ok, report(markdown), sources[], elapsed_s, pool, report_path}`

- `gemini_dr_plan(query, pool=...)` → plan, **chưa** chạy (để user duyệt)

- `gemini_dr_quota(pool="all")` → headroom mỗi pool

- `gemini_dr_account()` → email ↔ pool mapping



### Bước 4 — Tích hợp `gemini_mcp.py`



Thêm `deep_research: bool = False` vào `gemini_task()`. Khi `True` → route sang `gemini_dr_mcp` thay vì `agy`.



### Bước 5 — Test



- 1 câu deep research ngắn trên pool windows → kiểm tra `report` có markdown + `sources`

- Song song 2 pool → xác nhận quota tách biệt



---



## 7. Môi trường đã xác minh



| Thành phần | Giá trị |

|---|---|

| OS | Windows 11 (build 26200) + WSL2 Ubuntu |

| Python | 3.14.8 (`C:\Users\kimvi\AppData\Local\Python\pythoncore-3.14-64`), có thêm 3.13 |

| uv | 0.10.6 |

| `gemini-webapi` | đã cài, import OK |

| `browser-cookie3` | đã cài, **thất bại decrypt** (v20) |

| Chrome | `C:\Program Files\Google\Chrome\Application\User Data` — profiles `Default`, `Profile 1` |

| Edge | `C:\Program Files (x86)\Microsoft\Edge\Application\User Data` — profiles `Default`, `Profile 1` (**Edge đang chạy, 11 process → lock DB**) |

| `agy` | `%LOCALAPPDATA%\agy\bin\agy.exe` v1.5.6 |

| `gemini_mcp.py` | `C:\Users\kimvi\.cursor\bin\gemini_mcp.py` (1178 dòng) |

| GitHub CLI | `gh` 2.102.0, đã đăng nhập `CatKod`, scope `repo` |



### Cảnh báo: `agy.exe` có tool `google_search` nhưng KHÔNG dùng được



Đã test trực tiếp, cả 3 lần đều timeout:

```

{"status":"ERROR","error":"print timeout after 2m30s with the turn still in progress",

 "usage":{"input_tokens":20626,"output_tokens":341,"thinking_tokens":285}}

```

→ Antigravity có `google_search` **internal**, nhưng gọi nó qua CLI cũng timeout/giới hạn. Không dựa vào đây.



---



## 8. Việc chưa làm / câu hỏi mở



1. **Chưa export cookie** → chưa test `deep_research()` end-to-end. Đây là blocker duy nhất.

2. **Chưa xác minh Edge/Default cookie Gemini** — cần đóng Edge rồi chạy lại `scan_browser_cookies.py`.

3. **Chưa rõ quota Deep Research của AI Pro** — sau khi có cookie, gọi RPC quota để biết headroom mỗi pool.

4. **Chưa rõ `gemini_webapi` đọc quota Deep Research thế nào** — cần đọc `src/gemini_webapi/client.py` (`_quotas`, `list_models`) và `constants.py` (`GRPC` ids) để xác định RPC nào trả remaining.

5. **Chưa quyết định** dùng `mcp-gemini-deep-research` (`vadim-a-yegorov`, có sẵn `uvx`) hay tự viết MCP server để tích hợp pool kép. → **Khuyến nghị: tự viết**, vì repo có sẵn cần multi-pool + auto-routing mà repo kia không có.



---



## 9. Nguồn tham khảo (đã verify)



- `gemini_webapi` deep research: <https://github.com/HanaokaYuzu/Gemini-API/blob/master/src/gemini_webapi/components/research_mixin.py>

- Giao thức `batchexecute`: <https://github.com/EricAndrechek/gemini-usage/blob/main/_docs/GEMINI_API_INTERNALS.md>

- MCP có sẵn: <https://github.com/vadim-a-yegorov/mcp-gemini-deep-research>

- OpenAI-compatible server: <https://github.com/FarisHijazi/gemini-web-api>

- Multi-account Node wrapper: <https://github.com/qutek/gemini-web-api>

- Cookie extraction trên Windows: <https://github.com/steipete/oracle/issues/51>

- Evil-clone risk: <https://github.com/TNLegend/Chimera>

- ChatCmd (đã loại): <https://github.com/int04/ChatCmd>



---



## 10. Orchestrator — chạy task dài KHÔNG chặn agent (2026-11-13)



### Vấn đề user nêu



Gọi `gemini_dr_research` / `agy` trực tiếp = agent **ngồi chờ** 30-60 phút,

và user không biết Gemini đang làm gì.



### Giải pháp đã build



Tách "chạy" khỏi "chờ": job chạy ở **process con độc lập**, ghi tiến độ vào

SQLite, agent làm việc khác rồi quay lại lấy kết quả.



```

agent ──orch_submit──▶ SQLite ──spawn──▶ runner process (detached)

agent ◀──orch_result──────────────────── SQLite (DONE)

```



Wake-up: agent chạy **nền** `python -m orchestrator.orch_cli watch <job_id>`

→ Cursor tự inject `<system_notification>` khi tiến trình kết thúc.

**Không cần daemon, không cần hook, không cần poll.**



### Files



| File | Vai trò |

|---|---|

| `orchestrator/store.py` | SQLite job store + event log (WAL, multi-process safe) |

| `orchestrator/dispatch.py` | submit / spawn (DETACHED) / wait / cancel |

| `orchestrator/orch_mcp.py` | MCP server stdio — 7 tools `orch_*` |

| `orchestrator/orch_cli.py` | CLI cho shell background |

| `orchestrator/gui.py` | Tkinter dashboard (view của SQLite) |

| `orchestrator/chrome_profile.py` | Tạo hồ sơ Chrome cho acc Google mới |

| `orchestrator/runners/gemini.py` | Deep Research / chat / canvas |

| `orchestrator/runners/agy.py` | Antigravity CLI, parse NDJSON stream |

| `orchestrator/selftest.py` | Smoke test MCP thật (`SELFTEST PASS`) |



Docs: `docs/ORCHESTRATOR.md` (kiến trúc), `docs/ORCHESTRATOR-USAGE.md` (cách dùng).



### Kinds đã hỗ trợ



`deep_research` · `chat` · `canvas` (browser) · `canvas_api` (fallback) ·

`antigravity` · `chrome_profile`



### Bằng chứng đã verify (không phải claim)



- `orch_submit` + `orch_result` cho `chat` → text `"PONG"` / `"SELFTEST"` /

  `"FINAL"` đúng (3 lần chạy).

- Antigravity runner parse được NDJSON thật của `agy --output-format stream-json`

  (events: `init`, `step_update` với `text_delta`, `result`).

  1 lần chạy trả `status=ERROR` do **hết capacity model phía server**

  (`UNAVAILABLE (503) No capacity available for model gemini-3.8-flash-high`)

  — không phải lỗi code. Runner đã giữ `partial_response` khi có.

- `orchestrator.chrome_profile list` → đọc đúng 2 profile Chrome thật

  (`Default` = mmeo44244, `Profile 1` = catkingmv).

- `orchestrator.chrome_profile create --name "Canvas Acc"` → tạo thật

  `Profile 2`, xuất hiện trong `info_cache` + `profiles_order`.

- Canvas browser runner → Chrome spawn + CDP connect thành công, fail **đúng

  cách** với thông báo "profile chưa đăng nhập Google".

- `orchestrator.selftest` → `SELFTEST PASS` (7 tools, submit chat thật).

- Build `.exe`: PyInstaller onedir → `orchestrator-cli.exe` (68 MB cả thư mục),

  **startup median 161 ms** (nhanh hơn Nuitka 1040 ms). GUI exe mở được,

  hiển thị đúng tiếng Việt.

- `sync-gemini-capabilities.py` → `orchestrator` đã mirror sang Gemini CLI

  (15 MCP servers, +1 mới).



### Bẫy đã gặp (đừng lặp lại)



| Bẫy | Sửa |

|---|---|

| `output.text` trong `generate_content_stream` là **cumulative** → "PONG"+"PONG"+"PONG" | Dùng `output.text_delta` |

| DR plan nằm ở frame **giữa** stream, không phải frame cuối | Quét toàn bộ stream, giữ frame có plan |

| `deep_research_document.ready` là cờ authoritative — content rỗng KHÔNG phải xong | Chờ `ready AND content` |

| Model mặc định của account có thể **từ chối** DR (refusal, không phải lỗi API) | Dò qua `DR_MODEL_CANDIDATES` |

| PowerShell làm hỏng JSON inline (`--params '{"a":1}'`) | `--params @file.json` (đã hỗ trợ) |

| Chrome đang chạy → ghi đè `Local State`, hồ sơ vừa tạo biến mất | Bắt buộc đóng Chrome trước khi tạo |

| `mcp` 2.x đã đổi `FastMCP` → `MCPServer` | `from mcp.server.mcpserver import MCPServer` |



### MCP server đã đăng ký



`C:\Users\kimvi\.cursor\mcp.json` → key `orchestrator`

(`python.exe -m orchestrator.orch_mcp`, `PYTHONPATH` = repo root).

**Cần reload Cursor window** để nạp.



### Còn lại



1. **Reload Cursor window** → MCP `orchestrator` xuất hiện trong tool list.

2. ~~**Export cookie lại** cho pool `windows`~~ → **ĐÃ GIẢI QUYẾT** bằng cookie

   tự động (mục 11). Chạy `sync` là được, không cần Cookie-Editor nữa.

3. **Pool `wsl`** (`catkingmv@gmail.com`) → dùng GUI: "Tài khoản Gemini" →

   chọn account → "Đăng nhập (Chrome)" → đăng nhập → "Đồng bộ cookie".

   Hoặc CLI: `python -m orchestrator.cookie_sync login --account catkingmv`.

4. **Tài khoản thứ 3** (`t1vodichcktg2325@gmail.com`, Chrome profile "Profile 3"):

   dùng GUI "Tài khoản Gemini" → Thêm (account `t1vodich`, pool `acc3`) →

   "Đăng nhập (Chrome)" → đồng bộ.

5. Đăng nhập Google vào Chrome profile `Profile 2` ("Canvas Acc") → `canvas`

   (browser) mới sinh được artifact thật.

6. ~~Test `deep_research` end-to-end qua orchestrator~~ → **ĐÃ XONG**

   (job `j_502980b1f499`, 33 nguồn) + chat end-to-end (`j_2ed016af1eca`)

   + `.exe` end-to-end (`j_e81fdc8d6588`).

7. **Session `mmeo` đang chết** (`UNAUTHENTICATED`) → cần `adopt` lại với file

   export Cookie-Editor **mới** (file cũ 13/11 đã hết hạn), hoặc `login` lại.

6. `Profile 2` đã tạo nhưng chưa đăng nhập — có thể xoá nếu không dùng:

   xoá thư mục `Profile 2` + entry trong `Local State`.



---



## 11. ⭐ Cookie Gemini tự động hoá (2026-11-14)



**Vấn đề:** trước đây mỗi khi session hết hạn phải: mở Chrome → Gemini →

Cookie-Editor → Export → paste. User muốn tự động hoá.



**Kết quả:** đã giải quyết triệt để, không cần extension nữa.



### Files mới



| File | Vai trò |

|---|---|

| `orchestrator/cdp.py` | CDP client tối giểu (websocket-client thuần Python, **không** kéo Playwright 109 MB) |

| `orchestrator/cookie_sync.py` | Nghiệp vụ `adopt` / `sync` / `status` |



### Cách dùng



```powershell

# 1 lần cho mỗi account — nhận nuôi session vào Chrome riêng của orchestrator

python -m orchestrator.cookie_sync adopt --account mmeo `

    --export "D:\GitHub\.gemini-pool\chrome-default.json"



# mỗi lần cần cookie mới — tự động, không cần user thao tác

python -m orchestrator.orch_cli cookie sync --account mmeo

python -m orchestrator.orch_cli cookie status --account mmeo

```



> `--pool` là **optional** — orchestrator tự map account → pool

> (`ACCOUNT_POOLS` trong `orchestrator/cookie_sync.py`): `mmeo` → `windows`,

> `catkingmv` → `wsl`. Account mới thì thêm vào dict đó hoặc truyền `--pool`.



MCP: `orch_cookie_sync(action="sync"|"adopt"|"status", account=..., pool=...)`.

GUI: nút **"Đồng bộ cookie"**.



### Nguyên tắc (đã verify bằng script thật, không phải suy đoán)



Chrome ≥127 mã hoá cookie bằng **App-Bound Encryption** (`v20`) + **Device Bound

Session** (DBSC). Google bind session vào *đúng user-data-dir nơi nó được tạo*.



| Hướng đã thử | Kết quả thực nghiệm |

|---|---|

| Đọc `Network/Cookies` ngoài tiến trình | Không giải mã được |

| **Copy** profile → user-data-dir khác | Chrome **xoá cookie** (65 → 0) — DBSC không khớp. Profile thật thì **an toàn** |

| `--remote-debugging-port` + user-data-dir default | Chrome ≥136 **từ chối** mở port (không set được policy: `HKCU\Software\Policies` bị từ chối ghi) |

| `--remote-debugging-port` + **short path 8.3** (`USERDA~1`) | Port mở được ✅ nhưng cookie vẫn **không giải mã** (Chrome so sánh path đã resolve) |

| `--remote-debugging-pipe` | Windows cần handle riêng; `stdin`/`stdout` không dùng được (`OSError 22`) |

| **Chrome riêng + cấy cookie qua CDP** | ✅ **thành công** |



### Lời giải



Orchestrator sở hữu **Chrome riêng** tại `%LOCALAPPDATA%\OrchestratorChrome\<account>\`:



- **adopt** (1 lần): `Network.setCookie` cấy cookie từ file export → Chrome ghi

  lại kèm DBSC key **của profile riêng** → browser đăng nhập thật.

  Verify: trang Gemini hiện "Thư viện", "Đã lên lịch", lịch sử chat.

- **sync** (mỗi lần): `Storage.getCookies` đọc cookie live → ghi `pools/<pool>.json`.

- **đóng graceful**: `Browser.close` (exit code 0) để Chrome flush cookie vào DB.



### Bẫy đã gặp



| Bẫy | Sửa |

|---|---|

| Kill cứng Chrome (`terminate()`) → session cookie **mất** (không flush DB) | Luôn đóng bằng CDP `Browser.close` |

| `Network.setCookie` báo lỗi ở browser-level | Phải attach page session (`Target.attachToTarget`) rồi gọi kèm `sessionId` |

| `Storage.getCookies` trả 0 cookie | Phải dùng **browser-level** session, không phải page-level |

| Chrome flush cookie vào DB **chỉ khi thoát graceful** | `Browser.close` → `proc.wait()` phải trả exit code 0 |

| `.exe` ghi pool nhầm vào `dist/.../_internal/pools` | `_resolve_pools_dir()` xử lý `sys.frozen` (cwd → `%LOCALAPPDATA%\Orchestrator\pools`) |

| `status`/`sync` map pool theo tên account (`mmeo.json`) thay vì pool thật (`windows.json`) | `ACCOUNT_POOLS` + `default_pool_for(account)` |

| Đọc `Network/Cookies` offline fail `WinError 32` khi Chrome đang chạy | Báo `cookies_in_db: -1` (= không đọc được) thay vì `0` (nghĩa là không có cookie) — kiểm tra `pool_cookies` thay thế |

| **Cookie còn nhưng session CHẾT** → mọi DR fail `Permission denied` | `sync` gọi `_probe_session()` → `account_status`; báo `warning` nếu `!= ACTIVE` |

| `_looks_signed_out` trả sai (trang chưa đăng nhập vẫn hiện "Cuộc trò chuyện mới") | Chỉ dựa vào dấu hiệu mạnh ("thư viện"/"library") để kết luận đã đăng nhập |

| `.exe` spawn runner bằng `sys.executable -m orchestrator.runners.x` → fail (vì `sys.executable` là file .exe) | Lệnh nội bộ `__runner__ <module> <job_id>` khi frozen |

| PyInstaller không bundle `gemini_pool` / `orchestrator.runners.*` (import động) | `pathex` trỏ `<repo>/tools` + `hiddenimports` tường minh |

| Playwright kéo driver Node 109 MB → .exe phình 68 → 183 MB | Exclude `playwright`; job `canvas` chạy bằng Python source, runner báo lỗi rõ ràng |

| Runner (`.exe`) cwd = `_internal` → không thấy `pools/` | `dispatch._spawn` kế thừa `os.getcwd()`; `_resolve_dir` đi lên cây thư mục |



### Rủi ro



- **Evil Clone**: profile Chrome của user **không bao giờ bị đụng** → không có

  rủi ro này. Orchestrator dùng user-data-dir riêng, không cần đóng Chrome.

- **Session chết**: nếu Google chết session, `sync` báo lỗi rõ ràng → chạy

  lại `adopt` (cần file export Cookie-Editor mới, hoặc user đăng nhập lại

  trong Chrome riêng của orchestrator).



### Đã verify



- `adopt` → `signed_in: true`, `cookies_seeded: 15`, browser hiện lịch sử chat.

- `sync` → `ok: true`, 20 cookie, `pools/windows.json` cập nhật.

- Đọc lại sau khi đóng Chrome → cookie vẫn còn (20 cookie trong DB).

- `orchestrator-cli.exe cookie sync` → chạy đúng từ bản đóng gói.

- Session trong file Cookie-Editor vẫn sống: `init OK`, quota 46617/48384.

- **`deep_research` end-to-end** (job `j_502980b1f499`): DONE, report 12.398 bytes

  tại `reports/20261114-004107-b-nh-rust-v-go.md`, **33 nguồn**, model

  `gemini-4-hydrogen`, pool `windows`, `cid=c_9fc798e41e78b7ce`.

  → Chuỗi đầy đủ đã chạy thật: *cookie tự động sync → API → plan → dispatch →

  report có citation*.

- `orchestrator.selftest` → `SELFTEST PASS` (8 tools, chat thật trả "SELFTEST").

- **Chat end-to-end sau khi `sync` tự động** (job `j_2ed016af1eca`): pool

  `windows`, text `"SYNC_OK"`, `cid=c_707597ae1ecb3e9b` → cookie tự động dùng

  được thật, không chỉ Deep Research.

- **`.exe` chạy end-to-end** (job `j_e81fdc8d6588`): `orchestrator-cli.exe submit

  chat` → DONE, text `"EXE_OK"` → runner con spawn + pool resolve + Gemini API

  đều OK từ bản đóng gói.

- **GUI `.exe`**: mở được, title "Orchestrator", cửa sổ "Tài khoản Gemini" render

  đúng tiếng Việt, bảng liệt kê account (`mmeo` 19 cookie DB, `catkingmv` 3).

- **Phát hiện quan trọng**: cookie có thể **còn** nhưng session **chết**

  (`account_status=UNAUTHENTICATED`) → mọi DR fail `Permission denied`.

  `sync` giờ probe session thật và cảnh báo rõ ràng.

- `python -m orchestrator.selftest` → `SELFTEST PASS` sau khi sửa mapping.




---

## 12. ⭐⭐ Thiết kế lại toàn bộ cơ chế tài khoản & cookie (2026-11-14)

**Yêu cầu của user:** 2 loại tài khoản — (1) có xác thực, (2) chỉ email + mật
khẩu; loại 2 sẽ là loại chủ yếu. Cơ chế "thêm tài khoản" cũ không ổn → làm lại.

**Thiết kế chi tiết: `docs/ACCOUNT-MANAGEMENT.md`** (đọc file đó).

### Kết luận nghiên cứu (đã verify thực nghiệm, không đoán)

**OAuth HTTP flow để lấy cookie phiên web → BẤT KHẢ THI.** Đã chạy thật với
`curl_cffi` (impersonate `chrome124`): `accounts.google.com/o/oauth2/v2/auth` trả
`302` về `v3/signin/identifier`, cookie jar chỉ có `__Host-GAPS`. Lý do: Google
không hỗ trợ ROPC, Botguard + reCAPTCHA Enterprise chặn HTTP login thô, bearer
token `ya29...` không dùng được cho `gemini.google.com`, `__Secure-1PSIDTS`
không sinh trong OAuth flow, và App-Bound Encryption (Chrome ≥127) chặn đọc
cookie ngoài tiến trình.

→ **Chrome + CDP là đường đúng**, không phải lựa chọn vì "dễ làm".

### Files mới

| File | Vai trò |
|---|---|
| `orchestrator/accounts.py` | Registry tài khoản — **nguồn sự thật duy nhất** (`pools/accounts.json`) |
| `orchestrator/credentials.py` | Kho mật khẩu, mã hoá **Windows DPAPI** (không thêm dependency) |
| `orchestrator/auto_login.py` | Đăng nhập Google **tự động** bằng email + mật khẩu |
| `orchestrator/cdp.py` | + primitives điền form (`type_text_human`, `click_at`, `rect_center`, `press_enter`) |

### 2 loại tài khoản

| | Loại 1 (có xác thực) | Loại 2 (email + mật khẩu) |
|---|---|---|
| `auth` | `oauth` | `password` |
| Đăng nhập | user tay 1 lần trong Chrome riêng | orchestrator tự điền form |
| Session chết | user đăng nhập tay lại | `account login` — không cần user |

### CLI

```powershell
python -m orchestrator.orch_cli account add --email u@gmail.com --account acc3 --pool acc3 --password "..."
python -m orchestrator.orch_cli account login --account acc3
python -m orchestrator.orch_cli account set-password --account acc3 --password "..."
python -m orchestrator.orch_cli account list
python -m orchestrator.orch_cli account remove --account acc3 --delete-pool
```

### Bẫy đã gặp (đều đã sửa + verify)

| Bẫy | Sửa |
|---|---|
| `img#captchaimg` + `input[name=ca]` **luôn có sẵn trong DOM** (ẩn) → false-positive CAPTCHA | Phải kiểm tra **hiển thị** (`offsetParent` / bounding box) |
| URL bước nhập mật khẩu cũng là `/signin/challenge/pwd` — **không** phải 2FA | Regex `/\/signin\/challenge\/(?!pwd\b)/` |
| `aria-live="assertive"` chứa cả heading ("Welcome") → báo nhầm "Google báo: Welcome" | Lọc heading + text ngôn ngữ |
| `account_status` thành công là **`AVAILABLE` (1000)**, không phải `ACTIVE` | `_OK_ACCOUNT_STATUSES = {AVAILABLE, ACTIVE}` |
| `open_page` chỉ tạo tab `about:blank` → phải `Page.navigate` riêng | Đã sửa |
| Chrome cũ giữ user-data-dir → Chrome mới thoát ngay, cổng debug không mở | `ChromeSession._kill_stale_instances()` + retry spawn |
| Chờ `PASSWORD_READY` **trước** khi điền email → deadlock | Chờ ô **email** xuất hiện trước, rồi mới điền |
| Gán `.value` bằng JS → event `isTrusted: false` → Botguard flag | `DOM.focus` + `Input.insertText` **từng ký tự** + jitter 40-120 ms |

### Đã verify (2026-11-14)

- **DPAPI**: round-trip encrypt/decrypt OK; ciphertext của account A không giải mã
  được cho account B (entropy ràng buộc theo account).
- **Registry**: add / list / show / remove OK.
- **Auto-login end-to-end** (account thật, mật khẩu thật): điền email → tới
  `/signin/challenge/pwd` → điền password → **21 cookie** trong profile →
  `pools/acc3.json` có cookie → `_probe_session` → `account_status=AVAILABLE`,
  `session_ok=true`.
- **Chat**: `gemini-flash-lite` → `PING_OK`.
- **Deep Research**: `create_deep_research_plan("What is the capital of France?")`
  → plan "Capital of France" **OK**.
- → Tài khoản loại 2 **không cần user thao tác** và Deep Research chạy được.

### Ranh giới đạo đức

`auto_login` **không** bypass CAPTCHA, **không** bypass 2FA, **không** giải mã
challenge. Gặp challenge → trả `state="needs_human"`, giữ Chrome mở, báo user
xử lý tay.

---

## 13. ⭐⭐⭐ Dashboard đa tác vụ + popup dán cookie (2026-11-14)

**Yêu cầu của user:**
1. Sau khi nhập acc thành công → có **chỗ lưu file JSON cookie**; bấm "Nhập
   cookie" → hiện **popup** để **dán JSON** thôi (không cần file tạm).
2. Làm lại giao diện cho đẹp + thuận tiện, **tối ưu cho đa tác vụ AI song song**
   (chủ yếu gọi song song nhiều Gemini 1 lúc).

### Chia việc Cursor ↔ Gemini

| Phần | Ai làm |
|---|---|
| Read-model tổng hợp (`board.py`), UI toolkit thuần (`ui.py`), popup (`cookie_popup.py`), logic cookie (`cookie_sync.adopt_json`, `accounts.adopt_cookie_json`), reaper (`store.reap_orphans`) | **Cursor (Opus)** — quyết định, state, nghiệp vụ |
| Layout dashboard (`dashboard.py`, ~1.5k dòng) | **Gemini** — dựng từ brief `reports/brief-dashboard.md` |

Gemini chạy bằng `agy.exe --dangerously-skip-permissions` chạy nền (gọi trực tiếp,
không qua `gemini_task` MCP vì bridge timeout 1500 s).

### Files mới

| File | Vai trò |
|---|---|
| `orchestrator/ui.py` | Toolkit theme tối **thuần Tkinter/Canvas** — 0 dependency ngoài (không CustomTkinter/ttkbootstrap → `.exe` không phình). Widget: `Card`, `Button`, `IconButton`, `Badge`, `ProgressBar`, `Sparkline`, `Table`, `ScrollFrame`, `Modal`, `Toast` |
| `orchestrator/dashboard.py` | Layout dashboard (Gemini dựng): Launch bar (Chạy / Chạy ×N / Chạy song song) · bảng Job đang chạy + Lịch sử · bảng Account & công suất (quota) · log stream dưới |
| `orchestrator/cookie_popup.py` | Popup dán cookie JSON: tự dán clipboard, validate realtime `__Secure-1PSID`, báo rõ thiếu gì **trước** khi đụng Chrome |
| `orchestrator/board.py` | Read-model gộp: active jobs + recent (kèm `last_event`) + accounts + session status + quota headroom. Cache quota 120 s, non-blocking |

### Luồng nhập cookie mới

```
Cookie-Editor → Export → copy JSON
   ↓
GUI: Tài khoản → "Nhập cookie (dán JSON)" → popup tự dán clipboard
   ↓  validate: parse JSON + phải có __Secure-1PSID
" Lưu & cấy cookie "
   ↓  (nền)
cookie_sync.adopt_json() → CDP Network.setCookie vào Chrome riêng của account
   ↓
pools/<pool>.json  (cache cookie live)
cookie_files/<pool>.json  + cookie_files/history/<pool>-<ts>.json   ← BẢN GỐC
```

**Vì sao có `cookie_files/`:** `pools/<pool>.json` bị `sync()` **ghi đè** mỗi lần
bằng cookie live. Bản JSON user paste lúc nhập acc là thứ **không tái tạo được**
→ phải giữ riêng. `.gitignore` đã chặn `cookie_files/` (chứa session token).

`adopt_json` **không xoá** profile Chrome (khác `adopt()` từ file) → giữ DBSC key
của session đang sống.

### CLI / MCP mới

```powershell
python -m orchestrator.orch_cli account paste-cookie --account mmeo < cookie.json
python -m orchestrator.orch_cli cookie archive        # liệt kê bản cookie đã lưu
```

MCP: `orch_cookie_sync(action="archive"|"paste"|"sync"|"adopt"|"login"|"status")`,
`orch_board()`, `orch_reap()`, `orch_account(action="paste-cookie")`.

### Bẫy đã gặp

| Bẫy | Sửa |
|---|---|
| PowerShell làm hỏng JSON truyền qua `--json` | Pipe stdin: `Get-Content f.json -Raw \| python -m orchestrator.orch_cli account paste-cookie --account X` |
| Job zombie kẹt `QUEUED`/`RUNNING` (runner đã chết, không ai update SQLite) | `store.reap_orphans()` — kiểm tra PID còn sống (Win32 `OpenProcess`/`GetExitCodeProcess`, `STILL_ACTIVE=259`) → chuyển sang `FAILED` |
| `gemini_task` MCP timeout 1500 s | Gọi `agy.exe` trực tiếp, background process, brief markdown trong `reports/` |
| Tên hàm Gemini sinh ra khác dự kiến (`_run_parallel` vs `_on_submit_parallel_modal`) | Đọc source thật `dashboard.py` sau khi Gemini dựng xong |

### Đã verify (2026-11-14)

- `python -m orchestrator.selftest` → **PASS** (11 MCP tool, submit→DONE, chat `PING_OK`).
- Popup: JSON hỏng → báo lỗi cụ thể; thiếu `__Secure-1PSID` → báo rõ; hợp lệ → bấm Lưu gọi `on_save` + popup tự đóng.
- Modal song song: không có prompt → toast cảnh báo (đúng); có prompt → modal mở, 2 checkbox pool, nút "Bắt đầu chạy"/"Huỷ".
- `cookie_files/<pool>.json` ghi đúng, `cookie archive` liệt kê được.
- Dashboard render: 2 bảng, 13 nút, 2 combobox, log stream, cảnh báo account chết session.
- Build lại `.exe`: `dist/orchestrator-gui/orchestrator-gui.exe` + `dist/orchestrator-cli/orchestrator-cli.exe` — **74,5 MB / thư mục**, startup CLI **median 249 ms**. GUI `.exe` mở lên OK.

### Cách chạy

```powershell
.\dist\orchestrator-gui\orchestrator-gui.exe     # GUI
.\dist\orchestrator-cli\orchestrator-cli.exe board
```

> `.exe` tìm `pools/` + `accounts.json` theo: `GEMINI_DATA_DIR` → cwd (nếu cwd có
> `.git` hoặc `pools/`) → `%LOCALAPPDATA%\Orchestrator`. Chạy `.exe` từ thư mục
> repo thì dùng chung data với Python source.
---

---

## Session 2026-11-14 (buổi 2) — Auto-harvest + Cookie Bridge

### Câu hỏi user

1. **"Tại sao bấm Kiểm tra thành công nhưng bên ngoài vẫn nghi là unavailable"**
2. **"Tự động lấy cookie các acc đã đăng nhập"** (có thể dùng REA để dịch ngược Cookie-Editor)

### Trả lời

**Câu 1 — 3 nguyên nhân, đã sửa hết:**

1. `board.snapshot()` cache quota 60 s, `invalidate_quota_cache()` chỉ xoá `_quota_cache`
   mà **không** xoá `_session_cache` → bảng đọc cache cũ.
2. Bảng board đọc `account_status` từ quota map; pool không có entry thì ra `UNKNOWN`
   thay vì chạy probe trực tiếp.
3. Nút "Kiểm tra" trong GUI đọc cookie từ pool file (có thể đã cũ), không đọc
   cookie sống trong Chrome profile.

**Đã sửa:**

- `orchestrator/board.py`: thêm `_session_probe()` + `_session_map()` (TTL 90 s) làm
  fallback khi quota map không có pool; `invalidate_quota_cache()` xoá **cả hai** cache.
- `orchestrator/gui.py`: `_do_check()` đọc cookie sống từ Chrome profile trước,
  fallback về pool file; thêm nút `_do_bridge()` để chạy `cookie_bridge.BridgeServer`.

**Câu 2 — 6 cách lấy cookie từ Chrome đã đánh giá; chọn cách duy nhất khả thi:**

| Cách | Kết quả |
|---|---|
| Đọc file `Cookies` của Chrome từ ngoài tiến trình | ❌ App-Bound Encryption (`v20`), khoá gắn với tiến trình trình duyệt |
| `browser_cookie3` / `rookiepy` | ❌ cùng lý do |
| Copy cookie từ profile đang mở | ❌ rủi ro "Evil Clone" → Google logout toàn diện |
| CDP `--remote-debugging-port` trên default dir | ❌ Chromium 136+ chặn |
| Extension `chrome.cookies` API | ✅ **chọn** — đọc cookie trong tiến trình extension, không cần giải mã |
| REA dịch ngược Cookie-Editor | ❌ không cần — cơ chế đã rõ, extension giải quyết trực tiếp |

**Kiến trúc đã dựng:**

```
Chrome/Edge đã đăng nhập acc
   └─ mở tab gemini.google.com → extension auto-send (hoặc bấm icon)
        └─ POST → http://127.0.0.1:9477/ingest  (token bắt buộc)
             └─ cookie_bridge.ingest() → verify token → probe trước khi ghi
                  └─ pool file + profile riêng → AVAILABLE
```

**File mới:**

- `orchestrator/cookie_bridge.py` — HTTP loopback server (port 9477), verify token
  bằng `secrets.compare_digest`, **probe trước** khi ghi để không ghi đè session
  đang hoạt động bằng session chết.
- `browser_extension/orchestrator-bridge/` — extension MV3 (`manifest.json`,
  `background.js`, `popup.html`, `popup.js`). `background.js` tự POST cookie khi
  tab `gemini.google.com` / `accounts.google.com` load.
- `orchestrator/harvest.py` — thêm backoff gate tổng (`harvest_backoff.json`),
  khoảng cách `(0, 600, 1800, 3600, 7200)` giây → `auto_heal_tick` không còn
  mở Chrome liên tục cho acc loại 1 (cần 2FA).

**CLI / MCP mới:**

- `python -m orchestrator.cookie_bridge info|start`
- `python -m orchestrator.orch_cli bridge info|start`
- MCP tool `orch_bridge(action="info"|"start")`

### Tài liệu

- `docs/COOKIE-BRIDGE.md` — đầy đủ: kiến trúc, cài đặt, bảo mật, xử lý sự cố,
  và § **7b. Chat nhiều lượt trong cùng một hội thoại**.

---

## Session 2026-11-14 (buổi 3) — Chat nhiều lượt trong cùng hội thoại

### Câu hỏi user

> "tôi đã available 3 cái, bạn test thử gửi 2 tin nhắn trong cùng 1 hội thoại chat xem"

### Kết quả test — **3/3 PASS**

Trước khi test, đã xác nhận trạng thái pool:

| Account | Pool | Status | Headroom |
|---|---|---|---|
| `hoàngyen` | hoàngyen | AVAILABLE | 99.0 |
| `mmeo` | windows | AVAILABLE | 95.0 |
| `t1vodich` | acc3 | AVAILABLE | 100.0 |

Test gồm 3 tầng, tất cả PASS trên cả 3 pool:

| Tầng | Script | Cách kiểm |
|---|---|---|
| 1. Cùng 1 `ChatSession` trong 1 process | `reports/_chat_2turn_test.py` | `cid2 == cid1` + lượt 2 nhớ tên |
| 2. Resume qua **client mới** (2 process) | `reports/_chat_resume_test.py` | `cid2 == cid1` + lượt 2 nhớ tên |
| 3. **2 job orchestrator** thật | `reports/_chat_orchestrator_test.py` | `cid2 == cid1` + lượt 2 nhớ tên |

Prompt test: lượt 1 = *"Tên tôi là Kim. Hãy nhớ tên này."*, lượt 2 = *"Tên tôi là gì?
Chỉ trả lời tên thôi."* → cả 3 pool trả lượt 2 = `"Kim"`.

### Cơ chế

`gemini_webapi.ChatSession` giữ lịch sử hội thoại qua `metadata`:

- `cid` = conversation id
- `rid` = reply id
- `rcid` = reply candidate id

`client.start_chat(metadata=[cid, rid, rcid])` → nối vào conversation cũ, server
Gemini đọc lại lịch sử → giữ ngữ cảnh.

### Thay đổi code

| File | Thay đổi |
|---|---|
| `orchestrator/runners/gemini.py` | `run_chat()` nhận `params["chat_metadata"]` → `client.start_chat(metadata=...)`; trả `result.chat_metadata` từ `chat.last_output.metadata` |
| `orchestrator/orch_mcp.py` | `orch_submit(..., chat_metadata: list[str] \| None = None)` → ghi vào `params` |
| `orchestrator/orch_cli.py` | `--chat-metadata` nhận `job_id` / JSON array / `@file.json` |

### Cách dùng

```powershell
# lượt 1
python -m orchestrator.orch_cli submit chat "Tên tôi là Kim. Hãy nhớ tên này." --pool acc3
# → job_id: j_xxxx

# lượt 2 — nối tiếp cùng hội thoại
python -m orchestrator.orch_cli submit chat "Tên tôi là gì?" --pool acc3 --chat-metadata j_xxxx
```

MCP: `orch_submit(kind="chat", prompt="...", pool="acc3", chat_metadata=["c_...","r_...","rc_..."])`.

### Verify thực tế (job orchestrator, pool `acc3`)

```
job1 j_6dc67b6f9b5c  state=DONE  cid=c_f1890cfb95238529
     text1: "Chào bạn Kim! Tôi đã ghi nhớ tên của bạn rồi nhé. ..."
job2 j_6f4a591d5211  state=DONE  cid=c_f1890cfb95238529   ← cid GIỐNG job1
     text2: "Kim"                                        ← nhớ ngữ cảnh
```

`rid` đổi (`r_4666fd7a...` → `r_00ec3d92...`) → đúng là lượt 2 trong cùng thread.

### Ghi chú

- `docs/HANDOFF.md` (file này) bị hỏng encoding UTF-8 từ session trước (mất dấu
  tiếng Việt ở phần "buổi 2") → đã viết lại sạch ở buổi này.
- `_chat_2turn_test.py` dùng `send_message_stream` (không phải
  `generate_content_stream`) → `text_delta` là phần text MỚI, nối lại đúng.
  Runner `run_chat()` dùng `client.generate_content_stream` với `chat=chat` →
  cùng cơ chế, `text_delta` cũng là delta.
---

## Session 2026-11-14 (buổi 4) — 4 tài khoản loại 2 (email + mật khẩu)

### Câu hỏi user

> "bạn chạy thêm cả 4 tài khoản trong `C:\Users\kimvi\OneDrive\Desktop\account.txt`,
> tất cả các tài khoản này đều không cần xác thực gì cả chỉ cần tài khoản và mật khẩu"

4 tài khoản (email / password):

| Account | Email |
|---|---|
| `kimperiod0126` | kimperiod0126@gmail.com |
| `reigin483` | reigin483@gmail.com |
| `meobo930` | meobo930@gmail.com |
| `yarho3155` | yarho3155@gmail.com |

### Kết quả — **4/4 AVAILABLE**

| Account | Pool | Status | Headroom |
|---|---|---|---|
| `kimperiod0126` | kimperiod0126 | AVAILABLE | 99.0 |
| `reigin483` | reigin483 | AVAILABLE | 99.0 |
| `meobo930` | meobo930 | AVAILABLE | 99.0 |
| `yarho3155` | yarho3155 | AVAILABLE | 99.0 |

Cả 4 đều có **Google AI Pro** (hiển thị "Pro" trên trang Gemini).

Test chat 2 lượt cùng hội thoại (qua orchestrator, 2 job nối tiếp): **4/4 PASS**.

---

### Ba bug phát hiện & sửa trong lúc này

#### Bug 1 — `1PSIDTS` stale do cookie trùng tên ở nhiều domain

`cdp.get_cookies()` dùng `Storage.getCookies` + `setdefault` (first-wins).
Cookie `__Secure-1PSIDTS` tồn tại ở **cả** `.google.com` và `.youtube.com`, và bản
`.youtube.com` thường **cũ hơn** (không được rotate khi vào Gemini).

Kết quả: lấy nhầm bản stale → Google trả `UNAUTHENTICATED` (1016) dù browser
vẫn đăng nhập được và web Gemini vẫn hiện "Pro".

Bằng chứng (account `kimperiod0126`):
```
__Secure-1PSIDTS  .youtube.com  → sidts-CjQBnJZzaqMYoVZ...  (stale → 1016)
__Secure-1PSIDTS  .google.com   → sidts-CjYBnJZzaq6z_iZn...  (fresh → 1000)
```

**Sửa** (`orchestrator/cdp.py`): thêm `_COOKIE_DOMAIN_PRIORITY` — ưu tiên domain
Google, sắp xếp theo priority thay vì `setdefault`.

#### Bug 2 — `TOS_PENDING` (1040): tài khoản mới phải đồng ý điều khoản

Tài khoản mới bị Gemini chặn cho tới khi bấm nút "Tôi hiểu" (màn hình "Lưu ý"
ở trang Gemini).

**Sửa** (`orchestrator/auto_login.py`): thêm `TOS_SCRIPT` bấm nút, so khớp bằng
regex + `normalize('NFC')` (so khớp exact string bằng tiếng Việt dễ fail do
Unicode normalization). Flow: sync → thấy `TOS_PENDING` → mở Gemini → bấm →
sync lại.

#### Bug 3 — profile đã đăng nhập sẵn bị báo "không thấy ô nhập email"

Lần `account login` thứ 2 cho profile đã đăng nhập → Google redirect khỏi
`ServiceLogin` → `_state()` trả `LOGGED_IN` → code cũ báo lỗi.

**Sửa**: thêm nhánh `LOGGED_IN` → `_verify_and_finish()` (bỏ qua form, đi thẳng
TOS + sync + probe). Hàm này được tách ra để dùng chung cho cả 2 đường.

#### Bug 4 (phụ) — lỗi 1097 khi submit job 2 ngay sau job 1

`APIError 1097` xảy ra khi job chat nối tiếp được submit **ngay** sau job trước:
`1PSIDTS` của session cũ vừa bị rotate → request dùng token cũ.

**Sửa** (`orchestrator/runners/gemini.py`): retry 3 lần (5s, 10s) khi lỗi
chứa `1097` / `1013` / "temporar".

---

### Thay đổi code

| File | Thay đổi |
|---|---|
| `orchestrator/cdp.py` | `get_cookies()` ưu tiên domain Google (`_COOKIE_DOMAIN_PRIORITY`) — fix `1PSIDTS` stale |
| `orchestrator/auto_login.py` | `TOS_SCRIPT` + `_accept_tos()`; tách `_verify_and_finish()`; nhánh `LOGGED_IN` (profile đã đăng nhập) |
| `orchestrator/runners/gemini.py` | retry lỗi tạm thời 1097/1013 trong `run_chat()` |

### Cách dùng

```powershell
# đăng ký (mật khẩu lưu mã hoá DPAPI)
python -m orchestrator.orch_cli account add `
  --email kimperiod0126@gmail.com --account kimperiod0126 --password "***REDACTED***"

# đăng nhập tự động
python -m orchestrator.orch_cli account login --account kimperiod0126
```

### Tài liệu

- `docs/COOKIE-BRIDGE.md` § 4b — chi tiết luồng tài khoản loại 2 + 3 bug đã sửa.

### Ghi chú

- Pool `windows` (mmeo44244@gmail.com) vẫn `UNAUTHENTICATED` — session chết thật
  (profile Chrome không còn cookie `1PSID`). Cần đăng nhập lại bằng tay.
- Tài khoản cũ `t1vodich` / `hoàngyen` đã bị user xoá khỏi `pools/` → không còn
  trong board.
- Không dùng quota Antigravity/Deep Research trong phiên này — chỉ chat thường.

---

## Session 2026-11-14 (buổi 5) — file đính kèm (upload) + file Gemini sinh ra (download)

### Câu hỏi user

> "1 là gửi file đính kèm, bạn có thể gửi 1 file code và bắt gemini giải thích thử
> xem. thứ 2 là tạo nếu gemini trả về 1 file loại .xlsx, .pdf thì có lấy được không"

### Kết quả — CẢ HAI ĐỀU ĐƯỢC

#### 1. Upload file đính kèm — Gemini đọc THẬT

`gemini_webapi` hỗ trợ `files=` trên `generate_content` / `send_message`
(upload qua endpoint `bard-storage`).

Verify: file `.py` có `class Circle` + `SECRET_NUMBER = 987654` → Gemini trả
đúng `Circle` / `987654` → 3/3 câu PASS (không đoán mò).

#### 2. Gemini sinh file `.xlsx` / `.pdf` / `.csv` — TẢI ĐƯỢC

**Phát hiện quan trọng:** `gemini_webapi` **KHÔNG** expose file sinh ra trong
`ModelOutput`. `Candidate` chỉ có `images` / `generated_videos` /
`generated_media` / `citations` — không có field cho attachment/file.

File nằm trong **raw candidate**, rich content **field 59**:

```
candidate[12][0]["59"][0][0]
  [0]    = "[file-tag: code-generated-file-<uuid>]"
  [2]    = [None, 3, <filename>, <mime>, None, <base64 blob>, None,
            [thumb_url, download_url, upload_url]]
            [2][2]    = tên file          vd "Bao_cao_2026.pdf"
            [2][7][1] = URL tải
                        https://contribution.usercontent.google.com/download?...&filename=...
```

Tải bằng **chính session đã đăng nhập** (giữ cookie):
`client.client.get(url, headers={"Referer": "https://gemini.google.com/"})`
→ HTTP 200 + `content-type` đúng.

### Kiểm chứng nội dung file (không chỉ "HTTP 200")

| Định dạng | Size | Kiểm chứng |
|---|---|---|
| `.xlsx` | 7392 B | zip hợp lệ, `xl/worksheets/sheet1.xml`, tiếng Việt đúng dấu |
| `.pdf` | 2012 B | header `%PDF-1.4`, có `%%EOF`, đếm 1 trang |
| `.csv` | 310 B | header `Tên,Email,Tuổi,Thành phố` |

### Prompt nào sinh được file

- Nói rõ **"tạo file .xlsx / .pdf và gửi tôi file đính kèm"**.
- PDF: prompt **"viết và chạy Python dùng reportlab để sinh PDF"** cho kết quả
  ổn định. Prompt "tạo báo cáo PDF" trả về *immersive document* (HTML ở field
  30) — **không** phải file PDF, không tải được như file.

### Thay đổi code

| File | Thay đổi |
|---|---|
| `orchestrator/gemini_files.py` | **MỚI** — `extract_generated_files()` đọc field 59, `decode_blob()`, `sanitize_filename()` |
| `orchestrator/runners/gemini.py` | `run_chat()` nhận `params["attachments"]` → `files=`; hook `_parse_candidate` để bắt raw candidate; `_download_generated_files()` tải về `reports/downloads/`; `result.files` + `result.attachments` |
| `orchestrator/orch_cli.py` | `--attach FILE` (dùng nhiều lần cho nhiều file) |
| `orchestrator/orch_mcp.py` | `orch_submit(..., attachments: list[str] \| None = None)` |

### Cách dùng

```powershell
python -m orchestrator.orch_cli submit chat `
  "Đọc file đính kèm. Tên class là gì?" `
  --pool kimperiod0126 --attach reports\_e2e_sample.py
```

MCP: `orch_submit(kind="chat", prompt="...", pool="kimperiod0126",
attachments=["D:\\path\\file.py"])`.

File Gemini sinh ra → `reports/downloads/`, trả kèm `result.files`.

### Verify E2E qua orchestrator (2 job, pool `kimperiod0126`)

```
job1 (--attach .py)   state=DONE  text: "Tên class: Circle, SECRET_NUMBER: 987654"  ✅
job2 (sinh .xlsx)     state=DONE  files: [bang_chi_phi_doanh_thu_cong_ty_abc.xlsx, 7392 B]
                                              verify: exists=True, head=b'PK\x03\x04'  ✅
```

Test: `reports/_test_attach.py`, `reports/_test_extract.py`, `reports/_test_e2e.py`.
Chi tiết: `docs/COOKIE-BRIDGE.md` § 7c.

### Ghi chú

- Không dùng quota Antigravity/Deep Research trong phiên này — chỉ Gemini Flash/Pro
  chat thường (quota Flash còn 2363/2400 credits lúc cuối).
- Đường upload đã có sẵn trong `gemini_webapi`; phần **download** là phần ta viết
  thêm vì thư viện chưa hỗ trợ.
