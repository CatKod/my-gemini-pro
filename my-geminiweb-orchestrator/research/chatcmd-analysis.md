# Phân tích `int04/ChatCmd` — TẠI SAO BỎ QUA

> **Kết luận:** `int04/ChatCmd` **không khả thi** cho mục tiêu điều khiển Gemini Deep Research trên web.
> Repo này chỉ có bridge cho ChatGPT, và kiến trúc của nó **không vễ vang** với mô hình Deep Research.

Nghiên cứu ngày 2026-11-13. Chi tiết kỹ thuật đầy đủ ở [`../docs/FINDINGS.md`](../docs/FINDINGS.md) §B.

---

## 1. Repo là gì

<https://github.com/int04/ChatCmd>

> *"A local-first MCP bridge that turns web-based AI (ChatGPT, Grok, Gemini) into a supervised worker on your computer."*

README có nhắc "Gemini", nhưng **đó chỉ là câu marketing**. Kiểm chứng bằng source:

```powershell
rg -i "gemini|google\.com" %TEMP%\ChatCmd -l
# → KHÔNG có kết quả nào
```

### Thành phần thực tế

| Thành phần | Mô tả |
|---|---|
| `crates/` | Rust server — 4 crate (`core`, `mcp`, `runtime`, `storage`), 139 file `.rs` |
| `web/` | React management console (Vite) |
| `chatgpt-extension/` | MV3 extension — **chỉ** `chatgpt.com` |
| `crates/chatcmd-storage/migrations/` | 27 migration SQLite |

`chatgpt-extension/manifest.json`:
```json
"host_permissions": ["https://chatgpt.com/*", "http://localhost/*", "http://127.0.0.1/*"],
"content_scripts": [
  { "matches": ["https://chatgpt.com/*"], "js": ["content-runtime.js", "content-chatgpt-*.js", ...] },
  { "matches": ["http://localhost/*", "http://127.0.0.1/*"], "js": ["content-runtime.js", "content-chatcmd.js"] }
]
```

→ **Không có `gemini.google.com`** ở bất kỳ đâu.

---

## 2. Selector hardcode cho ChatGPT

Từ `chatgpt-extension/content-chatgpt-dom.js`:

```js
function assistantNodes() {
  return [...document.querySelectorAll(
    '[data-message-author-role="assistant"],[data-chatgpt-search-unit-key$=":assistant"]'
  )].filter(isVisible);
}

function findStopButton() {
  const composers = [...document.querySelectorAll('form[data-type="unified-composer"]')].filter(isVisible);
  const direct = findVisibleWithin(root, [
    'button[data-testid="stop-button"]',
    'button[data-testid="stop-generating-button"]',
    'button[aria-label*="Stop" i]',
    'button[aria-label*="Dừng" i]',   // ← có hỗ trợ tiếng Việt
    ...
  ]);
}

function findSendButton(composer) {
  const selectors = [
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Gửi"]',      // ← tiếng Việt
    ...
  ];
}
```

Không có selector nào dùng được trên Gemini. Gemini dùng cấu trúc hoàn toàn khác (`rich-textarea`, `mat-expansion-panel` cho mode, artifact panel cho document).

---

## 3. Vấn đề cấu trúc: transcript ≠ Deep Research

### Mô hình ChatCmd

Schema (`0004_chatgpt_bridge.sql`):
```sql
CREATE TABLE chatgpt_bridge_requests (
    id TEXT PRIMARY KEY NOT NULL,
    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    turn_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES mcp_agents(id) ON DELETE RESTRICT,
    model TEXT NOT NULL,
    user_content TEXT NOT NULL,
    submitted_content TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued','running','stop_requested','completed','stopped','failed')),
    conversation_id TEXT,
    conversation_url TEXT,
    assistant_content TEXT,     -- ← chỉ TEXT
    error_message TEXT,
    ...
);
```

Luồng: `user_content` → gửi → chờ → `assistant_content` (text) → `completed`.

### Mô hình Gemini Deep Research

```
1. Bật toggle Deep Research trong UI
2. Gemini trả PLAN (danh sách bước) → cần USER XÁC NHẬN
3. Task chạy SERVER-SIDE DETACHED — không giữ session
4. Báo cáo về dưới dạng INLINE DOCUMENT ARTIFACT
   Reply chỉ là câu "I've completed your research"
```

Khác biệt cốt lõi:

| Khía cạnh | ChatGPT bridge | Gemini Deep Research |
|---|---|---|
| Có bước confirm | ❌ không | ✅ **có** (plan → confirm) |
| Task detached | ❌ chạy trong tab | ✅ chạy server-side |
| Kết quả | text trong bubble | **document artifact** + sources |
| Cách lấy nội dung | `innerText` node assistant | `deep_research_document.content` |
| Trạng thái giữa chừng | `stop-button` tồn tại | **không có** — chỉ biết khi xong |

Từ docstring `wait_for_deep_research` của `gemini_webapi` (chính là tác giả reverse-engineer đã giải thích tường minh):

> *"Gemini exposes no task entity for this flow: the task id field holds the literal `agency-placeholder-task-id` from the moment research starts until the report lands, both task-listing RPCs stay empty throughout, and no id appears anywhere in the raw stream — so there is nothing to poll for status with. The report's own arrival is the only signal."*

→ Ngay cả khi port extension, bạn cũng **không** lấy được report qua DOM một cách đơn giản, vì nó nằm trong artifact panel với cấu trúc riêng.

---

## 4. Chi phí port

| Hạng mục | Ước lượng |
|---|---|
| Build Rust desktop từ source | Rust 1.85+ + Node 20.19+ + **Visual Studio Build Tools (MSVC C++)** |
| Build lần đầu (139 file `.rs` + 4 crate) | hàng giờ |
| Viết extension Gemini mới | ~10 content script, selector mới, bản dịch Việt/Anh |
| Xử lý artifact + plan-confirm flow | re-engineer lại toàn bộ transcript model |
| Database migration | thêm bảng/route cho Gemini |
| **Tổng** | rất lớn, kết quả **vẫn kém** `gemini_webapi` |

Và README của chính repo cảnh báo:

> *"If the pre-built versions from GitHub fail to open, please download the entire project repository and open it using https://www.jetbrains.com/rust/. The issue may stem from the GitHub Actions compiler malfunctioning on certain machines."*

---

## 5. Tình trạng bảo trì

| Trường | Giá trị (2026-11-13) |
|---|---|
| created | 2026-08-26 |
| pushed | **2026-09-26** |
| stars / forks | 47 / 16 |
| open issues | 8 |
| contributors | **1** (`int04`) |
| releases | 3 |

Issue mở:

| # | Tiêu đề | Ngày |
|---|---|---|
| 33 | *[Bug] Lots of Bugs why nobody is working on this?* | 2026-11-11 |
| 32 | [Feature] English language. | 2026-10-01 |
| 28 | *[Bug] ChatGPT extension is not installed* | 2026-09-13 |
| 24 | [Feature] Nút force tắt running task | 2026-09-09 |
| 20 | *[Bug] Chat phản hồi xong nhưng chatcmd agent vẫn hiện dừng* | 2026-09-08 |
| 17, 12 | [Bug] | |

→ 1 maintainer, push cuối 6 tuần trước, và chính user đã report *"Lots of Bugs why nobody is working on this?"*

---

## 6. So sánh trực tiếp

| Tiêu chí | `int04/ChatCmd` | `HanaokaYuzu/Gemini-API` |
|---|---|---|
| Hỗ trợ Gemini | ❌ 0 dòng code | ✅ `deep_research()` hoàn chỉnh |
| Deep Research artifact | ❌ không có khái niệm | ✅ `deep_research_document` |
| Plan → confirm | ❌ | ✅ tách 3 bước |
| Ngôn ngữ | Rust (build nặng) | Python 3.11+ (`pip install`) |
| Bảo trì | 1 maintainer, push 2026-09-26 | push 2026-11-10, 3.8k stars |
| Setup | clone + cargo + npm + MSVC | `pip install gemini-webapi` |
| Rủi ro selector vỡ |cao (DOM đổi)| trung bình (protocol đổi, lib fix nhanh) |

---

## 7. Kết luận

**Bỏ qua `ChatCmd`.** Đi đường `gemini_webapi`.

Lý do cốt lõi: `ChatCmd` giải quyết một bài toán khác — *điều khiển ChatGPT web như một worker cho coding*. Nó **không** có khung khái niệm cho research workflow (plan → confirm → detached task → artifact + citations). Đó không phải thứ có thể "port" — đó là phần lớn giá trị.

`gemini_webapi` đã có sẵn toàn bộ phần đó, được maintain bởi cộng đồng lớn hơn nhiều, và chạy đúng trên `gemini.google.com` — nơi Deep Research **trừ vào gói AI Pro đã trả**.

---

## Tài liệu liên quan

- [`../docs/FINDINGS.md`](../docs/FINDINGS.md) — toàn bộ bằng chứng, có primary source
- [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) — kiến trúc giải pháp đã chọn
- [`../docs/HANDOFF.md`](../docs/HANDOFF.md) — bàn giao cho phiên mới
