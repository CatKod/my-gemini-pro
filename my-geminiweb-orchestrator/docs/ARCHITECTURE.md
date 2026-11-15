# ARCHITECTURE.md

## Tổng quan

```
┌──────────────────────────────────────────────────────────────────────┐
│  Cursor / MCP client                                                  │
└───────────────┬──────────────────────────────────────────────────────┘
                │ stdio MCP
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  gemini_dr_mcp.py   (tools/ — chưa viết)                             │
│  ├─ gemini_dr_research(query, pool, timeout, model)                   │
│  ├─ gemini_dr_plan(query, pool)          → plan, CHƯA chạy           │
│  ├─ gemini_dr_quota(pool)                → headroom mỗi pool          │
│  └─ gemini_dr_account()                  → email ↔ pool               │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  gemini_pool.py   (tools/ — chưa viết)                               │
│  ├─ Pool(name, account_email, cookies, client)                       │
│  ├─ load_pools()          ← đọc pools/*.json                        │
│  ├─ select_pool()         ← chấm điểm theo quota headroom            │
│  └─ quota cache 300s      ← nhất quán với gemini_mcp.py              │
└───────────────┬──────────────────────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  gemini_webapi  (pip install gemini-webapi)                          │
│  ├─ GeminiClient(secure_1psid, secure_1psidts)                       │
│  ├─ curl_cffi + browser TLS fingerprint impersonation                 │
│  └─ auto_refresh 1PSIDTS mỗi 600s                                     │
└───────────────┬──────────────────────────────────────────────────────┘
                │ HTTPS  POST /_/BardChatUi/data/batchexecute
                ▼
        gemini.google.com   (Gemini Advanced / AI Pro)
        → Deep Research chạy server-side, trừ vào allowance gói đã trả
```

## Ba luồng song song trong hệ thống

| Luồng | Công cụ | Quota | Dùng cho |
|---|---|---|---|
| Antigravity CLI | `agy.exe` (Windows) + `agy` (WSL2) | 2 pool Antigravity | Code, refactor, task thường |
| Deep Research web | `gemini_webapi` (2 pool cookie) | Allowance Gemini Advanced | Research, phân tích sâu, viết báo cáo |
| AI Studio API | Interactions API | **Tiền riêng** | ❌ Đã loại |

## Pool mapping

| Pool name | Nguồn cookie | Tài khoản | Ghi chú |
|---|---|---|---|
| `windows` | `pools/chrome-default.json` | `mmeo44244@gmail.com` | Cùng acc với pool Antigravity Windows |
| `wsl` | `pools/chrome-profile1.json` | `catkingmv@gmail.com` | Cùng acc với pool Antigravity WSL |

→ `gemini_dr_research(pool="auto")` chọn pool nhiều quota còn nhất, **giống** `_select_pool()` trong `gemini_mcp.py`.

## Vòng đời một Deep Research run

```
1. create_deep_research_plan(prompt)
   └─ gọi RPC deep_research=True
   └─ Gemini trả về plan (steps, title, confirm_prompt)
   └─ _assert_deep_research_capable() đã chạy trước đó

2. start_deep_research(plan)
   └─ gửi confirm_prompt ("Start research")
   └─ Gemini tạo task server-side, trả về "agency-placeholder-task-id"

3. wait_for_deep_research(plan, poll_interval=10, timeout=600)
   └─ poll fetch_latest_chat_response(cid, limit=10, match=_has_report)
   └─ KHÔNG có progress signal — chỉ biết khi report xuất hiện
   └─ timeout → task VẪN chạy server-side, đọc lại chat sau được

4. Trích kết quả
   └─ result.final_output.deep_research_document
      ├─ .ready      bool
      ├─ .content    markdown body
      ├─ .title
      ├─ .sources    web sources khớp [cite: N]
      └─ .markdown   body + danh sách nguồn đánh số
```

## Quyết định thiết kế

### Vì sao tự viết MCP server thay vì dùng `mcp-gemini-deep-research`?

Repo có sẵn (`vadim-a-yegorov`) cung cấp đúng 1 tool `gemini_deep_research(query, timeout=600)`. Nhưng:
- **Đơn pool** — không có multi-account routing
- Không có `gemini_dr_plan` (xem plan trước khi chạy)
- Không có quota tool
- Không tích hợp được với `_select_pool()` sẵn có trong `gemini_mcp.py`

→ Tự viết ~300 dòng, tái dùng `gemini_webapi` làm thư viện.

### Vì sao không dùng `browser-cookie3`?

Vì Chrome ≥127 dùng app-bound encryption → `browser_cookie3` **luôn** fail với `Unable to get key for cookie decryption` (đã verify trên máy này, cả Chrome lẫn Edge).

### Vì sao không tự động hoá toàn bộ việc lấy cookie?

Vì cần chạy Chrome với `--remote-debugging-port`, mà:
1. Phải đóng Chrome sạch (mất phiên làm việc)
2. Rủi ro evil-clone nếu profile đang mở
3. `Cookie-Editor` chỉ mất 30 giây

→ Để user làm thủ công lần đầu, sau đó tự động hoá nếu cần.

### Vì sao giữ `gemini_mcp.py` riêng biệt?

Nó đang chạy production với 2 pool Antigravity. Thêm `deep_research=True` flag thay vì viết lại — giảm rủi ro hồi quy. Logic Deep Research nằm trong `gemini_dr_mcp.py`, `gemini_mcp.py` chỉ gọi sang.

## Định dạng dữ liệu trả về

```json
{
  "ok": true,
  "pool": "wsl",
  "account": "catkingmv@gmail.com",
  "elapsed_s": 412.7,
  "title": "So sánh SQLite và DuckDB cho phân tích",
  "report": "# So sánh...\n\n## Phần 1\n...",
  "sources": [
    { "index": 1, "title": "...", "url": "https://..." }
  ],
  "report_path": "D:\\GitHub\\my-gemini-pro\\reports\\20261113-143000-so-sanh-sqlite-duckdb.md",
  "stalled": false
}
```

`stalled: true` = timeout nhưng task vẫn chạy server-side → có thể đọc lại bằng `cid`.
