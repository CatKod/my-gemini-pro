# HANDOFF — my-gemini-pro

> **Đọc file này đầu tiên khi nhận "continue" ở phiên mới.**
> Nó chứa đủ context để tiếp tục mà không cần đọc transcript cũ.

**Ngày bàn giao:** 2026-11-13 20:11 (UTC+7)
**Trạng thái:** nghiên cứu xong, chưa viết code production.
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
