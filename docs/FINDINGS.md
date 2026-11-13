# Kết quả nghiên cứu & nguồn

Ghi chép các phát hiện có **load-bearing** (ảnh hưởng quyết định kiến trúc). Mỗi claim đều đã verify bằng primary source.

---

## A. Vì sao `antigravity` / `agy` không dùng được cho Deep Research

### A.1 `agy.exe` CÓ code Deep Research — nhưng gọi AI Studio

String analysis trên `%LOCALAPPDATA%\agy\bin\agy.exe` (ASCII, regex context 80 ký tự):

```
deep-research-preview-04-2026
deep-research-max-preview-04-2026
deep-research-pro-preview-12-2025
DeepResearchAgentConfig
CreateAgentInteractionAgentConfig: %w
could not unmarshal `%s` into expected (Type == deep-research) type DeepResearchAgentConfig within InteractionAgentConfig: %w
type_!const:"deep-research" json:"type"
google_search
```

Protobuf field:
```
DeepResearchConfigN protobuf:"bytes,3,opt,name=deep_research_config,json=deepResearchConfig,oneof"
```

→ `agy` **có** hỗ trợ Deep Research về mặt code, nhưng nó nằm trong `interactions` API (AI Studio).

### A.2 AI Studio Interactions API là pay-as-you-go

Endpoint: `https://generativelanguage.googleapis.com/v1beta/interactions`
Agents: `deep-research-preview-04-2026`, `deep-research-max-preview-04-2026`

**Xác minh thực tế:** OAuth token Antigravity → HTTP **403** `ACCESS_TOKEN_SCOPE_INSUFFICIENT`.

→ Không dùng được token Antigravity, mà dùng API key AI Studio thì **tính phí riêng**, không trừ vào gói AI Pro. **Loại.**

### A.3 `agy` có `google_search` internal nhưng CLI gọi được thì timeout

Test trực tiếp (3 lần, cả `--dangerously-skip-permissions`):

```json
{"conversation_id":"d6c197b9-...","status":"ERROR","response":"",
 "error":"print timeout after 2m30s with the turn still in progress; the response is partial",
 "duration_seconds":115.13,"num_turns":1,
 "usage":{"input_tokens":20626,"output_tokens":341,"thinking_tokens":285,"cache_read_tokens":15886}}
```

→ Không dựa vào `agy` cho web access.

---

## B. `int04/ChatCmd` — đánh giá tính khả thi

**Kết luận: KHÔNG khả thi cho mục tiêu này.**

### B.1 Bằng chứng

Clone vào `%TEMP%\ChatCmd` (commit `e604ac8c`, 2026-09-25).

```
rg -i "gemini|google\.com" %TEMP%\ChatCmd -l
→ (không kết quả)
```

`chatgpt-extension/manifest.json`:
```json
"host_permissions": ["https://chatgpt.com/*", "http://localhost/*", "http://127.0.0.1/*"],
"content_scripts": [{ "matches": ["https://chatgpt.com/*"], ... }]
```

Selector ChatGPT (từ `content-chatgpt-dom.js`):
```js
'[data-message-author-role="assistant"],[data-chatgpt-search-unit-key$=":assistant"]'
'form[data-type="unified-composer"]'
'button[data-testid="send-button"]'
'button[data-testid="stop-button"]'
```

### B.2 Metadata repo (GitHub API, 2026-11-13)

| Trường | Giá trị |
|---|---|
| created | 2026-08-26 |
| pushed | **2026-09-26** (6 tuần không cập nhật) |
| stars / forks | 47 / 16 |
| open issues | 8 |
| contributors | 1 (`int04`) |
| language | Rust |
| releases | 3 |

Issue mở đáng chú ý:
- #33 — *"[Bug] Lots of Bugs why nobody is working on this?"* (2026-11-11)
- #28 — *"[Bug] ChatGPT extension is not installed"*
- #24 — *"[Feature] Nút force tắt running task"*
- #20 — *"[Bug] Chat phản hồi xong nhưng chatcmd agent vẫn hiện dừng"*
- #17, #12 — "[Bug]" (không mô tả)

### B.3 Phân tích rủi ro port sang Gemini

Schema SQLite (`0004_chatgpt_bridge.sql`):
```sql
CREATE TABLE chatgpt_bridge_requests (
    ... model TEXT NOT NULL, user_content TEXT NOT NULL,
    assistant_content TEXT, conversation_url TEXT, ...
);
```

Mô hình: **prompt → assistant_content (text)**.

Deep Research Gemini web thực tế là:
1. Toggle Deep Research
2. Gemini trả **plan** → cần xác nhận
3. Task chạy **server-side detached**
4. Báo cáo = **inline document artifact**, không phải bubble chat

→ `assistantNodes() → innerText` bắt được "I've completed your research", **không** bắt được nội dung report.

Để làm đúng, phải reverse-engineer `batchexecute` — tức là viết lại `gemini_webapi` bằng tay.

### B.4 Chi phí

- Build: Rust 1.85+ + Node 20.19+ + Visual Studio Build Tools (MSVC C++ workload)
- README cảnh báo: *"If the pre-built versions from GitHub fail to open, please download the entire project repository and open it using https://www.jetbrains.com/rust/"*
- Sau đó: viết extension Gemini mới + rework toàn bộ transcript/artifact model

**Đánh giá:** đánh đổi tệ. Loại.

---

## C. Giải pháp: `gemini_webapi`

Repo <https://github.com/HanaokaYuzu/Gemini-API> — clone vào `%TEMP%\gemini-webapi`, commit `f14269ab` (2026-11-10).

### C.1 Metadata

| Trường | Giá trị |
|---|---|
| stars | 3.810 |
| open issues | 43 |
| pushed | 2026-11-10 (3 ngày trước khi khảo sát) |
| language | Python |
| PyPI | `gemini-webapi` **2.1.1**, `requires_python >=3.11` |
| License | MIT |

### C.2 Deep Research API — trích từ source

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

Ba bước tách riêng — dùng để cho user duyệt plan trước:

| Hàm | Input | Output |
|---|---|---|
| `create_deep_research_plan(prompt, chat=None, model=None)` | prompt | `DeepResearchPlan` (`steps`, `title`, `confirm_prompt`, `cid`, `research_id`) |
| `start_deep_research(plan, chat=None, confirm_prompt=None)` | plan | `ModelOutput` — xác nhận → task chạy |
| `wait_for_deep_research(plan, poll_interval=10.0, timeout=600.0)` | plan | `DeepResearchResult` |

Điểm quan trọng từ docstring của `wait_for_deep_research`:

> *"Gemini exposes no task entity for this flow: the task id field holds the literal `agency-placeholder-task-id` from the moment research starts until the report lands, both task-listing RPCs stay empty throughout, and no id appears anywhere in the raw stream — so there is nothing to poll for status with. The report's own arrival is the only signal."*

→ Không thể poll progress %, chỉ biết khi report xuất hiện.

Result extraction:
```python
document = result.final_output.deep_research_document
document.ready      # bool
document.content    # markdown body
document.title
document.sources    # web sources, khớp với [cite: N]
document.markdown   # body + danh sách nguồn đánh số
```

### C.3 Eligibility probe

```python
probes = [
    ("activity",    GRPC.BARD_SETTINGS,           '[[["bard_activity_enabled"]]]'),
    ("bootstrap",   GRPC.DEEP_RESEARCH_BOOTSTRAP, '["en",null,null,null,4,null,null,[2,4,7,15],null,[[5]]]'),
    ("model_state", GRPC.DEEP_RESEARCH_MODEL_STATE,"[[[1,4],[6,6],[1,15]]]"),
    ("quota",       GRPC.DEEP_RESEARCH_MODEL_STATE,"[[[1,11],[2,11],[6,11]]]"),
    ("caps",        GRPC.DEEP_RESEARCH_CAPS,      "[]"),
]
dr_available = all(
    result["rpc"].get(p, {}).get("ok", False)
    and result["rpc"].get(p, {}).get("reject_code") is None
    for p in ("bootstrap", "model_state", "caps")
)
```

Lỗi khi không đủ điều kiện:
```
"Current account/session appears not eligible for deep research. Rejected: [...], Failed: [...]"
```
`reject_code == 7` = bị Google từ chối.

### C.4 Giao thức `batchexecute`

Nguồn: <https://github.com/EricAndrechek/gemini-usage/blob/main/_docs/GEMINI_API_INTERNALS.md>

**Token lấy từ HTML:**
```
GET https://gemini.google.com/app   (với cookie hợp lệ)
"SNlM0e":"<access_token>"   → param `at`
"cfb2h":"<build_label>"     → param `bl`
"FdrFJe":"<session_id>"     → param `f.sid`
```

**Endpoint:**
```
POST https://gemini.google.com/_/BardChatUi/data/batchexecute
```

**Query:** `rpcids` (CSV), `_reqid` (+100000 mỗi lần), `rt=c`, `source-path=/app`, `bl`, `f.sid`

**Form:**
```
at   = <SNlM0e>
f.req = [[["{rpcid}","{payload_as_string}",null,"generic"]]]
```

> **CRITICAL:** payload bên trong `f.req` phải là JSON **string**, không phải raw JSON.
> Ví dụ gọi `MaZiqc` với payload `[50]`:
> ```json
> [[["MaZiqc","[50]",null,"generic"]]]
> ```
> `"[50]"` là string, không phải array `[50]`.

**Framing response:**
1. Optional anti-XSSI prefix `)]}'\n`
2. Một hoặc nhiều frame: `{length}\n{json_content}`
3. Length tính theo **UTF-16 code units** (khớp `String.length` của JS)

Sau khi parse: `["{rpcid}", null, "{inner_json_string}", ...]` — data nằm ở index `[2]`, phải parse JSON lần nữa.

### C.5 Cookie

| Cookie | Bắt buộc | Ghi chú |
|---|---|---|
| `__Secure-1PSID` | ✅ | dài hạn (tháng) |
| `__Secure-1PSIDTS` | khuyến nghị | Google xoay thường xuyên → lib tự refresh mỗi 600s qua `accounts.google.com/RotateCookies` |

Source `client.py`:
```python
self.auto_refresh: bool = True
self.refresh_interval: float = 600
self.impersonate: BrowserTypeLiteral = BROWSER_TYPE
self.watchdog_timeout: float = 120  # seconds before declaring a zombie stream
self._quotas: dict[str, dict] = {}
```

→ Dùng `curl_cffi` với browser TLS fingerprint impersonation.

### C.6 So sánh với các lựa chọn khác

| Repo | Ghi chú |
|---|---|
| `vadim-a-yegorov/mcp-gemini-deep-research` | MCP sẵn, `uvx mcp-gemini-deep-research`, tool `gemini_deep_research(query, timeout=600)` → `{ok, report, sources, elapsed_s, research_id, report_path, stalled}`. **Đơn pool, không auto-routing** → sẽ tự viết thay vì dùng |
| `FarisHijazi/gemini-web-api` | OpenAI-compatible server (`/v1`). 2 backend: `webapi` (reverse-engineered) + `chrome` (extension). Chưa thấy hỗ trợ Deep Research trong README |
| `qutek/gemini-web-api` | Node/Hono, multi-account pooling + failover, cookie rotation 3 phút, `impit` browser fingerprint. Cảnh báo ToS rõ ràng |
| `HanaokaYuzu/Gemini-API` | ✅ **Chọn** — Python, MIT, 3.8k stars, cập nhật 3 ngày trước, `deep_research()` hoàn chỉnh |

---

## D. Cookie trên máy — kết quả probe thật

Script: `scripts/scan_browser_cookies.py`.

### D.1 Bảng tổng hợp

| Browser / Profile | Tài khoản (từ `Preferences` / `Local State`) | Gemini cookie | `__Secure-1PSID` |
|---|---|---|---|
| Chrome `Default` | `mmeo44244@gmail.com` · gaia `100722739602672374612` | ✅ 8 | ✅ enc 216 |
| Chrome `Profile 1` | `catkingmv@gmail.com` · gaia `107894897885205192113` | ✅ 8 | ✅ enc 216 |
| Edge `Default` | `kimvinhh6@gmail.com` · gaia `81dea17dfd148f0b` | ⚠️ chưa xác minh (DB lock) | ✅ |
| Edge `Profile 1` | — (không đăng nhập) | ❌ 0 | SID cũ, không phải 1PSID |

Nguồn xác định tài khoản — `Preferences` chứa:
```json
"email":"mmeo44244@gmail.com","full_name":"Meo Meo","gaia":"100722739602672374612"
"email":"catkingmv@gmail.com","full_name":"Vĩnh Hoàng Kim (Cod)","gaia":"107894897885205192113"
```

Edge `Default`:
```json
"email":"kimvinhh6@gmail.com","gaia":"81dea17dfd148f0b"
```

### D.2 Cookie Gemini tìm thấy

`host_key = .gemini.google.com`:
`COMPASS`, `_gcl_au`, `_gcl_aw`, `_gcl_dc`, `_gcl_gs`, `_ga`, `_ga_BF8Q35BMLM`, `_ga_WC57KJ50ZZ`

### D.3 Xác nhận đã đăng nhập thật (History DB)

```
Chrome/Default : 2026-11-13 14:41:00  https://gemini.google.com/app?is_sa=1&...  title="Google Gemini"
Chrome/Profile 1: 2026-11-13 14:42:09  https://gemini.google.com/app?is_sa=1&...  title="Google Gemini"
Edge/Default   : 2026-11-13 14:44:16  https://gemini.google.com/app                 title="Google Gemini"
```

### D.4 Blocker: app-bound encryption (v20)

```python
__Secure-1PSID   plain=0  enc=216  prefix=763230   # "v20"
__Secure-1PSIDTS plain=0  enc=148  prefix=763230
```

`browser_cookie3`:
```
chrome failed: BrowserCookieError Unable to get key for cookie decryption
edge   failed: BrowserCookieError Unable to get key for cookie decryption
```

Các cách bypass đã thử và thất bại:

| Cách | Kết quả |
|---|---|
| `shutil.copyfile` (Edge) | `PermissionError: [Errno 13]` |
| `CreateFileW` + `FILE_SHARE_READ\|WRITE\|DELETE` | error **32** (sharing violation) |
| `esentutl /y` | `JET_errFileAccessDenied` (-1032) |
| SQLite `mode=ro&immutable=1` | `unable to open database file` |
| `robocopy /B` | không copy được file đang lock |

**Đây là hạn chế đã biết của Chrome ≥127**, không phải lỗi máy.
Tham chiếu: <https://github.com/steipete/oracle/issues/51> — maintainer đóng với comment:
> *"Chrome's Windows auth cookies use app-bound v20 encryption, so external DPAPI/sqlite extraction cannot reliably recover them. The practical paths are still `--browser-manual-login` or `--browser-inline-cookies-file`."*

### D.5 "Evil Clone" — rủi ro logout toàn diện

Nguồn: <https://github.com/TNLegend/Chimera>

> *"Google's security backend is highly sensitive to concurrent sessions. If you extract cookies from your **main personal Chrome profile** and run this proxy, Google will see two identical sessions doing different things at the exact same time (one from your real browser, one from the headless Playwright browser). Google will assume your session was hijacked by a hacker and will immediately hit the kill switch, **logging you out of your Google account across all devices.**"*

Giải pháp theo nguồn: dùng **burner Chrome profile** hoặc **burner Google account** riêng cho automation.

---

## E. Quota Deep Research

Chưa verify được (cần cookie hợp lệ).

Từ tài liệu giá của Google (2026):

| Plan | Giá | Gemini app allowance | Deep Research |
|---|---|---|---|
| Free | $0 | baseline (refresh mỗi 5 giờ, có trần tuần) | ✓ (giới hạn) |
| AI Plus | $4.99 | 2× | ✓ |
| **AI Pro** | **$19.99** | **4×** | **✓** |
| AI Ultra | $99.99 | tới 20× | ✓ |

Lưu ý: kể từ 2026-05-17 Google **đo compute thay vì đếm prompt** — allowance refresh mỗi 5 giờ cho tới khi chạm trần tuần. Không công bố con số cố định.

→ **AI Pro (4×) là mức tốt nhất mà vẫn là gói đã trả.** Cả 2 acc của user đều nên dùng được.

Nguồn: <https://theaiadvantage.ca/google-ai-plans-explained/> · <https://felloai.com/is-gemini-free/>
