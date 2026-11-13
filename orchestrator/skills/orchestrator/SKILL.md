# Orchestrator — chạy task dài không chặn agent

## Vấn đề giải quyết

Gọi `gemini_dr_research` hay `agy` trực tiếp = agent phải chờ (blocking).
Orchestrator tách việc đó ra process con độc lập:

```
agent ──orch_submit──▶ SQLite (QUEUED) ──spawn──▶ runner process con
                                                       │ ghi event + result
agent ◀──orch_result────────────────────────────── SQLite (DONE)
```

Agent **không bao giờ poll**. Nếu cần bị đánh thức khi job xong, chạy nền
`python -m orchestrator.orch_cli watch <job_id>` — Cursor tự inject notification
khi tiến trình kết thúc.

## Cú pháp MCP tools

| Tool | Khi nào dùng |
|---|---|
| `orch_submit(kind, prompt, pool, model, timeout, params)` | Bắt đầu task. Trả `job_id` ngay. |
| `orch_status(job_id)` | Xem job đang làm gì (log event). |
| `orch_result(job_id)` | Lấy kết quả cuối. |
| `orch_list(state, kind)` | Xem tất cả job. |
| `orch_cancel(job_id)` | Huỷ job. |
| `orch_watch(job_ids)` | Chặn chờ — chỉ khi thực sự cần kết quả ngay. |
| `orch_chrome_profile(action, display_name)` | Tạo/liệt kê hồ sơ Chrome. |

## `kind` nào dùng khi nào

| kind | Dùng cho | Backend |
|---|---|---|
| `deep_research` | Nghiên cứu có nguồn, report dài | Gemini WebAPI, trừ allowance AI Pro |
| `chat` | Hỏi đáp thường, tóm tắt, so sánh nhanh | Gemini WebAPI |
| `canvas` | Tạo artifact React tương tác | Chrome thật qua CDP + Playwright |
| `canvas_api` | Artifact qua API (fallback) | Gemini WebAPI |
| `antigravity` | Task code/research của Gemini CLI | `agy --output-format stream-json` |
| `chrome_profile` | Tạo hồ sơ Chrome cho acc Google mới | Local, không network |

## `params` theo kind

```jsonc
// antigravity
{"wsl": true, "model": "gemini-3-pro", "effort": "high", "yolo": true,
 "cwd": "D:\\path", "continue_conversation": false, "timeout": 1800}

// deep_research
{"timeout": 3600, "model": "gemini-4-hydrogen"}

// chat / canvas_api
{"extended_thinking": true, "save_text": true, "force_canvas": true}

// canvas (browser)
{"profile_directory": "Profile 2", "headless": false, "timeout": 600,
 "debug_port": 9333}

// chrome_profile
{"display_name": "Acc 2", "open_after": true}
```

## Quy trình chuẩn (không chặn)

1. `orch_submit(...)` → nhận `job_id`.
2. Làm việc khác (đọc code, viết spec, trả lời người dùng...).
3. Khi cần kết quả:
   - Nếu chưa xong: chạy nền trong Cursor shell
     `python -m orchestrator.orch_cli watch <job_id>` rồi tiếp tục làm việc khác.
   - Nếu đã xong: `orch_result(job_id)`.

## Kết quả trả về

- `deep_research` → `{title, report, markdown, sources[], report_path}`.
  `report_path` trỏ tới `reports/<timestamp>-<slug>.md`.
- `chat` → `{text, citations[], cid}`.
- `canvas` → `{text, canvas_url, profile_directory, url}` (browser).
- `canvas_api` → `{text, canvas_url, artifact_code, artifact_path}`.
- `antigravity` → `{response, conversation_id, usage, duration_seconds}`.

## Quy tắc quan trọng

- **Đừng gọi `orch_watch` trong tool call chính** — nó chặn agent. Chạy nền
  bằng shell (`block_until_ms: 0`) để Cursor đánh thức bạn.
- `pool: "auto"` chấm điểm headroom quota, chọn pool nhiều quota nhất.
  Ghim `pool: "windows"` nếu cần tài khoản cụ thể.
- Deep Research chạy **server-side**: hết timeout local vẫn còn chạy trên
  Google. Dùng `cid` trong `external_id` để lấy lại sau.
- Cookie Gemini có thể chết (Evil Clone) → job FAILED với
  `UNAUTHENTICATED`. Xem `docs/COOKIE-SETUP.md`.
