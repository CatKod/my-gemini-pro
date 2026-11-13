# Orchestrator — Hướng dẫn dùng

## Cài đặt MCP server

Đã thêm vào `C:\Users\kimvi\.cursor\mcp.json`:

```json
"orchestrator": {
  "type": "stdio",
  "command": "C:\\Users\\kimvi\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
  "args": ["-u", "-X", "utf8", "-m", "orchestrator.orch_mcp"],
  "env": { "PYTHONPATH": "D:\\GitHub\\my-gemini-pro" },
  "cwd": "D:\\GitHub\\my-gemini-pro"
}
```

Restart Cursor (hoặc reload window) để MCP server được nạp.

## Vòng đời 1 task (không chặn agent)

```powershell
# 1. Agent submit -> nhận job_id ngay
python -m orchestrator.orch_cli submit deep_research "So sánh X vs Y năm 2026" --pool windows

# 2. Agent làm việc khác (đọc code, viết spec, trả lời bạn...)

# 3. Khi cần kết quả -> chạy NỀN, Cursor tự đánh thức khi xong
python -m orchestrator.orch_cli watch j_xxxxx
```

Bước 3 phải chạy nền (`block_until_ms: 0`) — Cursor tự inject notification
khi tiến trình kết thúc. Đó là cơ chế **không cần daemon, không cần poll**.

## GUI

```powershell
python -m orchestrator.gui
```

- Danh sách job (lọc: đang chạy / tất cả / xong / lỗi)
- Nhật ký tiến độ của job đang chọn (auto refresh 2s)
- Kết quả cuối
- Nút **Tạo hồ sơ Chrome** — tạo hồ sơ mới + mở Chrome để bạn đăng nhập
- Nút **Đồng bộ cookie** — lấy cookie Gemini mới tự động (không cần Cookie-Editor)
- Nút **Huỷ job**, **Mở report**

Đóng GUI **không** ảnh hưởng job đang chạy — GUI chỉ đọc SQLite.

## Cookie Gemini tự động (không cần Cookie-Editor)

Orchestrator có **Chrome riêng** cho mỗi account tại
`%LOCALAPPDATA%\OrchestratorChrome\<account>\` — không đụng Chrome của bạn.

### Lần đầu cho mỗi account

**Cách ưu tiên — không cần Cookie-Editor:**

```powershell
python -m orchestrator.cookie_sync login --account mmeo
```

Chrome riêng của orchestrator mở ra → bạn đăng nhập Google → mở tab
`gemini.google.com/app` → đóng Chrome.

**Nếu đã có file Cookie-Editor export sẵn** (làm 1 lần, nhanh hơn):

```powershell
python -m orchestrator.cookie_sync adopt `
    --account mmeo `
    --export "D:\GitHub\.gemini-pool\chrome-default.json" `
    --pool windows `
    --email "mmeo44244@gmail.com"
```

→ `"signed_in": true` nghĩa là đã nhận nuôi session thành công.

### Mỗi lần cần cookie mới (sync)

```powershell
python -m orchestrator.orch_cli cookie sync --account mmeo
python -m orchestrator.orch_cli cookie status --account mmeo
```

> Pool tự map theo account (`mmeo` → `windows`). `--pool` chỉ cần khi dùng
> account mới.

Hoặc từ agent qua MCP: `orch_cookie_sync(action="sync", account="mmeo")`.

> ⚠️ Đừng kill cứng Chrome của orchestrator — phải đóng graceful để cookie
> được flush. Module `cookie_sync` tự làm việc này (CDP `Browser.close`).
> Chi tiết kỹ thuật: `docs/COOKIE-SETUP.md` §3, `docs/ORCHESTRATOR.md`.

## Tạo hồ sơ Chrome cho acc Google mới

```powershell
# Liệt kê
python -m orchestrator.chrome_profile list

# Tạo (Chrome PHẢI đang đóng)
python -m orchestrator.chrome_profile create --name "Acc 2"
```

Hoặc nút trong GUI. Sau khi tạo:
1. Chrome mở với hồ sơ mới → bạn tự đăng nhập + 2FA.
2. Export cookie bằng Cookie-Editor (chọn đúng hồ sơ đó).
3. Lưu thành `pools/<tên>.json` (xem `docs/COOKIE-SETUP.md`).

> Chrome đang chạy thì không tạo được — Chrome ghi đè `Local State` khi thoát
> và xoá hồ sơ vừa tạo.

## Các loại job

| kind | Dùng cho | Quota |
|---|---|---|
| `deep_research` | Nghiên cứu có nguồn | Allowance Gemini Advanced (gói AI Pro) |
| `chat` | Hỏi đáp thường | Allowance Gemini Advanced |
| `canvas` | Artifact React tương tác — **điều khiển browser thật** | Allowance Gemini Advanced |
| `canvas_api` | Artifact qua API (fallback, không cần browser) | Allowance Gemini Advanced |
| `antigravity` | Task code của Gemini CLI | Pool Antigravity (windows/wsl) |
| `chrome_profile` | Hồ sơ Chrome mới | Không dùng quota |

### Canvas: 2 đường

**`canvas`** (mặc định) — điều khiển Chrome thật qua CDP, bật nút Canvas,
gõ prompt, chờ artifact. Cần một **Chrome profile đã đăng nhập Google**.

```powershell
# 1. Tạo profile (Chrome phải đóng)
python -m orchestrator.chrome_profile create --name "Acc Canvas"

# 2. Đăng nhập Google trong profile đó (mở Chrome → profile "Acc Canvas")

# 3. Chạy job
python -m orchestrator.orch_cli submit canvas "Dựng biểu đồ doanh thu" `
    --params @reports/canvas-params.json
```

`reports/canvas-params.json`:
```json
{ "profile_directory": "Profile 2" }
```

> Runner copy profile sang `%LOCALAPPDATA%\OrchestratorChrome` trước khi mở,
> nên **không đụng** session thật của bạn. Nhưng vẫn phải đóng hết Chrome
> đang chạy, nếu không Chrome sẽ không nhận `--remote-debugging-port`.

**`canvas_api`** — fallback qua `gemini_webapi`. Nhanh hơn nhưng chỉ sinh được
artifact khi account có model hỗ trợ (account chỉ có `gemini-flash-lite` thì
chỉ ra text, không ra code).

## Tuỳ chọn

```powershell
# Chỉ định model
python -m orchestrator.orch_cli submit chat "..." --model gemini-pro

# Antigravity qua WSL (pool thứ 2)
python -m orchestrator.orch_cli submit antigravity "..." --wsl --model gemini-3-pro

# Timeout (giây)
python -m orchestrator.orch_cli submit deep_research "..." --timeout 7200

# Params dạng file (tránh PowerShell làm hỏng JSON inline)
python -m orchestrator.orch_cli submit antigravity "..." --params @params.json
```

## Xem lại job

```powershell
python -m orchestrator.orch_cli list                    # tất cả
python -m orchestrator.orch_cli list --state active     # đang chạy
python -m orchestrator.orch_cli status j_xxxxx          # + nhật ký chi tiết
python -m orchestrator.orch_cli result j_xxxxx          # kết quả
python -m orchestrator.orch_cli cancel j_xxxxx
python -m orchestrator.orch_cli purge --keep 200
```

## Build .exe

```powershell
python scripts\build_exe.py
```

Kết quả:
- `dist/orchestrator-cli/orchestrator-cli.exe` — console/headless (~68 MB cả thư mục, startup **161 ms** median)
- `dist/orchestrator-gui/orchestrator-gui.exe` — GUI Tkinter (~74 MB)

> Benchmark cũ (Nuitka onedir 121 MB / 1040 ms) bị bỏ: PyInstaller build nhanh
> hơn nhiều và startup thực tế **161 ms** (đo lại sau khi tối ưu excludes).
> Vì sao không Nuitka cho bản GUI: Python 3.14 nhúng Tcl qua `zipfs://` trong
> `python314.dll` → plugin `tk-inter` của Nuitka không tìm thấy Tcl.
> Xem `docs/ORCHESTRATOR.md`.

## Vị trí dữ liệu

| Thứ | Đường dẫn |
|---|---|
| Job store (SQLite) | `%USERPROFILE%\.cursor-agents\orchestrator\jobs.db` |
| Log runner | `%USERPROFILE%\.cursor-agents\orchestrator\logs\<job_id>.log` |
| Report DR | `reports\<timestamp>-<slug>.md` |

Đổi bằng biến môi trường `ORCH_STATE_DIR`.

## Kiểm tra nhanh

```powershell
python -m orchestrator.selftest
```

Chạy MCP tools thật (submit chat + chờ + đọc kết quả). Phải in `SELFTEST PASS`.

## Ghi chú quan trọng

- Deep Research chạy **server-side**. Timeout local không huỷ task trên Google —
  `cid` nằm trong `external_id` để lấy lại sau.
- Cookie Gemini có thể chết (Evil Clone) → job FAILED với `UNAUTHENTICATED`.
  Xem `docs/COOKIE-SETUP.md`.
- Pool tên `windows` (mmeo44244@gmail.com) / `wsl` (catkingmv@gmail.com).
  `auto` chấm điểm headroom quota, chọn pool nhiều quota nhất.
