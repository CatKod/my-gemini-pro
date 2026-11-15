# Cookie Bridge — lấy cookie tự động từ browser thật

> **Mục tiêu:** *"lúc nào tất cả các acc đã đăng nhập và xác thực đều luôn trong
> trạng thái available và có thể dùng được"* — không phải copy/paste cookie
> thủ công nữa.

---

## 1. Vấn đề gốc

Chrome/Edge ≥ 127 mã hoá cookie trên đĩa bằng **App-Bound Encryption** (`v20`).
Khoá gắn với *tiến trình trình duyệt* → **không đoạn code nào ngoài tiến trình
đó đọc được cookie từ đĩa**.

### Những cách đã thử và đã thất bại (verify thật, 2026-11-14)

| Cách | Kết quả | Bằng chứng |
|---|---|---|
| Đọc DB cookie ngoài tiến trình | ❌ prefix `v20` | `reports/_edge_who.py` |
| DPAPI thuần (`CryptUnprotectData`) | ❌ `v20` không phải DPAPI | thử tay, fail |
| Copy profile Chrome → thư mục khác → CDP | ❌ Chrome **xoá** cookie (DBSC) | đã test |
| Chrome thật + `--remote-debugging-port` | ❌ Chromium 136+ chặn (default dir) | `reports/_try_user_browser.py` |
| Junction → user-data-dir khác → CDP | ❌ không giải mã được | `reports/_edge_junction.py` |
| Copy Edge profile → CDP | ❌ chỉ trả cookie tiktok | `reports/_edge_cdp.py` |
| **CDP `Storage.getCookies` trên profile RIÊNG** | ✅ trả cookie đã giải mã | `orchestrator/cookie_sync.py` |

### Đường hợp lệ duy nhất

**Cookie-Editor chạy BÊN TRONG trình duyệt** → dùng API `chrome.cookies` →
trình duyệt tự giải mã và trả về plaintext.

Ta làm đúng điều đó: extension nhỏ trong `browser_extension/orchestrator-bridge/`.

> **Không cần REA / dịch ngược.** Cơ chế đã rõ và đã verify bằng chính bridge
> này (HTTP 200, `account_status=AVAILABLE`, token sai → 403).

---

## 2. Kiến trúc

```
   Chrome/Edge của bạn (đã đăng nhập acc)
        │
        │  mở tab gemini.google.com  ──┐
        │                               │  (auto-send, hoặc bấm icon)
        │  chrome.cookies.getAll({domain: ".google.com"})
        ▼                               │
   POST http://127.0.0.1:9477/harvest ◄┘
        { token, psid, psidts }
        │
        ▼
   orchestrator.cookie_bridge.ingest()
        │  1. xác thực token (403 nếu sai)
        │  2. match owner: theo email → theo 1PSID đã lưu → probe từng pool
        │  3. probe API thật → nếu cookie mới chết mà cookie cũ sống → giữ cũ
        │  4. ghi pools/<pool>.json (2 cookie auth)
        │  5. adopt vào Chrome riêng của orchestrator (session sống lâu dài)
        │  6. xoá cache cookie cũ của gemini_webapi
        │  7. invalidate cache quota → dashboard hiện ngay
        ▼
   pool file + profile riêng → AVAILABLE
```

---

## 3. Cài đặt (một lần)

### Bước 1 — lấy token

```powershell
python -m orchestrator.cookie_bridge info
```

Output:
```json
{
  "url": "http://127.0.0.1:9477",
  "endpoint": "http://127.0.0.1:9477/harvest",
  "token": "zwsuFeYpIcEhgUQ1Q-5fJ8lezOu2JnYh",
  "extension_dir": "D:\\GitHub\\my-gemini-pro\\browser_extension\\orchestrator-bridge"
}
```

### Bước 2 — cài extension

1. Mở `chrome://extensions` (Chrome) hoặc `edge://extensions` (Edge)
2. Bật **Developer mode** (góc trên bên phải)
3. Bấm **Load unpacked** → chọn thư mục `extension_dir` ở trên

### Bước 3 — cấu hình

1. Bấm icon extension trên toolbar
2. Dán **token** → bấm **Lưu cấu hình**
3. Bật checkbox **"Tự gửi mỗi khi mở tab Gemini"** (khuyến nghị)
4. Bấm **Gửi cookie Gemini** để thử ngay

### Bước 4 — chạy bridge

```powershell
python -m orchestrator.cookie_bridge start
```

Hoặc bấm nút **"Lấy cookie từ browser"** trong GUI Orchestrator
(`Cửa sổ tài khoản`).

Bridge tự tắt sau **15 phút** không nhận request.

---

## 4. Cách hoạt động "luôn AVAILABLE"

Có **3 lớp phòng thủ** chạy song song:

| Lớp | Cơ chế | Tần suất |
|---|---|---|
| **1. Extension auto-send** | Mở tab Gemini → tự POST cookie | ngay khi bạn dùng Gemini |
| **2. `harvest_all`** | Mở Chrome riêng → đọc cookie live → ghi pool → probe | nút "Thu hoạch tất cả" / CLI |
| **3. `auto_heal_tick` (GUI)** | Phát hiện account chết → tự `harvest_all` + `auto_login` | mỗi 180s khi GUI mở |

### Tự chữa session chết

Khi session chết (Google đã hết hạn server-side):

- **Loại 2** (có mật khẩu trong `credentials.json`, mã hoá DPAPI) →
  `auto_login.auto_login()` điền form đăng nhập tự động.
  Verify đã chạy thật: `t1vodich` được hồi sinh từ `UNAUTHENTICATED` → `AVAILABLE`.
- **Loại 1** (OAuth / 2FA) → **không** bypass được → báo `needs_human`,
  mở Chrome riêng cho bạn đăng nhập tay. Có **backoff** (10s → 30s → 1h → 2h)
  để không spam.

---

## 4b. Tài khoản loại 2 — email + mật khẩu (auto_login)

Tài khoản **không cần xác thực 2 lớp**: chỉ email + mật khẩu → hệ thống tự
điền form Google, lấy cookie, ghi pool, probe session.

### Đăng ký + đăng nhập

```powershell
# 1. đăng ký (mật khẩu lưu mã hoá DPAPI — không ghi plaintext ra file)
python -m orchestrator.orch_cli account add `
  --email kimperiod0126@gmail.com `
  --account kimperiod0126 `
  --password "***REDACTED***"

# 2. đăng nhập tự động (mở Chrome riêng, điền form, lấy cookie)
python -m orchestrator.orch_cli account login --account kimperiod0126
```

### Luồng `auto_login.auto_login()`

```
mở Chrome (profile riêng) → accounts.google.com/ServiceLogin?hl=en
  → điền email → Next
  → dò trạng thái bằng DOM: CAPTCHA | BLOCKED | CHALLENGE | PASSWORD_READY
  → điền password → Next
  → chờ cookie 1PSID MỚI (so với cookie trước khi điền form)
  → sync cookie → probe session
  → nếu TOS_PENDING: mở gemini.google.com → bấm "Tôi hiểu" → sync lại
  → đóng Chrome → probe lần cuối
```

### Ba chi tiết quan trọng (đều đã verify 2026-11-14)

| Vấn đề | Nguyên nhân | Xử lý |
|---|---|---|
| **`1PSIDTS` stale** | `Storage.getCookies` trả cookie **cùng tên ở nhiều domain**; bản `.youtube.com` thường **cũ hơn** bản `.google.com`. `setdefault` (first-wins) lấy nhầm → server trả `UNAUTHENTICATED` (1016) dù session thật còn sống | `cdp.get_cookies()` ưu tiên domain Google (`_COOKIE_DOMAIN_PRIORITY`) |
| **`TOS_PENDING`** (1040) | Tài khoản mới chưa đồng ý điều khoản Gemini | `TOS_SCRIPT` bấm nút "Tôi hiểu" / "I understand" (so khớp bằng regex + `normalize('NFC')`) rồi sync lại |
| **Profile đã đăng nhập sẵn** | Google redirect khỏi trang signin → code cũ báo "không thấy ô nhập email" | `_verify_and_finish()` — bỏ qua form, đi thẳng vào bước verify (TOS + sync) |

> **Bằng chứng bug `1PSIDTS`** (account `kimperiod0126`):
> ```
> __Secure-1PSIDTS  .youtube.com  → sidts-CjQBnJZzaqMYoVZ...  (stale → 1016)
> __Secure-1PSIDTS  .google.com   → sidts-CjYBnJZzaq6z_iZn...  (fresh → 1000 AVAILABLE)
> ```

### Kết quả verify 2026-11-14 (4 tài khoản từ `account.txt`)

| Account | Email | Kết quả |
|---|---|---|
| `kimperiod0126` | kimperiod0126@gmail.com | ✅ AVAILABLE, headroom 99% |
| `reigin483` | reigin483@gmail.com | ✅ AVAILABLE, headroom 99% |
| `meobo930` | meobo930@gmail.com | ✅ AVAILABLE, headroom 99% |
| `yarho3155` | yarho3155@gmail.com | ✅ AVAILABLE, headroom 99% |

Cả 4 đều trải qua: điền form → đồng ý điều khoản → sync → probe `AVAILABLE`.

---

## 5. Bảo mật

| Ràng buộc | Thực thi |
|---|---|
| Chỉ bind loopback | `ThreadingHTTPServer(("127.0.0.1", port))` — không nhận từ mạng |
| Token bắt buộc | `secrets.compare_digest` — token sai → `403` (đã test) |
| Token random | `secrets.token_urlsafe(24)`, lưu file có ACL chỉ user hiện tại |
| Tự tắt | 15 phút idle → shutdown |
| Chỉ 2 cookie | `__Secure-1PSID` + `__Secure-1PSIDTS` |
| Chỉ `.google.com` | extension `chrome.cookies.getAll({domain: ".google.com"})` |
| Không log cookie | handler chỉ log `METHOD /path` |
| Không đụng profile user | mọi thao tác chỉ với `%LOCALAPPDATA%\OrchestratorChrome\<account>` |

---

## 6. Xử lý sự cố

| Triệu chứng | Nguyên nhân | Xử lý |
|---|---|---|
| `403 token sai` | Token đã rotate | `python -m orchestrator.cookie_bridge info --rotate` → dán lại |
| `Không thấy cookie __Secure-1PSID` | Chưa đăng nhập trong tab đó | Mở `gemini.google.com/app`, đăng nhập, rồi gửi lại |
| `Không xác định được cookie thuộc account nào` | Extension không lấy được email | Mở tab Gemini **đã đăng nhập** rồi gửi lại; hoặc dùng nút "Đồng bộ cookie" |
| Bridge không khởi động | Port 9477 đã bị chiếm | `orchestrator-cli bridge info` để xem; đóng process cũ |
| `Permission denied or unauthenticated` khi chạy task | Session chết | `orchestrator-cli harvest all` (tự chữa account loại 2) |

---

## 7. Lệnh CLI / MCP

```powershell
python -m orchestrator.cookie_bridge info      # token + hướng dẫn
python -m orchestrator.cookie_bridge start     # chạy server
python -m orchestrator.orch_cli bridge info    # tương đương
python -m orchestrator.orch_cli harvest all    # thu hoạch + tự chữa
```

MCP tool: `orch_bridge(action="info" | "start")`.

---

## 7b. Chat nhiều lượt trong CÙNG một hội thoại

Mỗi job `kind="chat"` mặc định tạo hội thoại **mới**. Muốn lượt 2 nối tiếp lượt 1
(cùng `cid`), truyền `chat_metadata` — list `[cid, rid, rcid]` lấy từ
`result.chat_metadata` của job trước.

**CLI:**

```powershell
# lượt 1
python -m orchestrator.orch_cli submit chat "Tên tôi là Kim. Hãy nhớ tên này." --pool acc3
# → job_id: j_xxxx

# lượt 2 — nối tiếp (truyền job_id của lượt 1)
python -m orchestrator.orch_cli submit chat "Tên tôi là gì?" --pool acc3 --chat-metadata j_xxxx
```

`--chat-metadata` nhận 3 dạng:
- `j_xxxx` — job_id của job chat trước (CLI tự lấy `chat_metadata`)
- `["c_...","r_...","rc_..."]` — JSON array thẳng
- `@file.json` — đọc từ file

**MCP tool `orch_submit`:**

```
orch_submit(kind="chat", prompt="Tên tôi là gì?", pool="acc3",
            chat_metadata=["c_f1890cfb95238529","r_...","rc_..."])
```

**Cơ chế:** `gemini_webapi.ChatSession` giữ lịch sử qua `metadata`
(`cid` = conversation id, `rid` = reply id, `rcid` = reply candidate id).
`run_chat()` đọc `chat.last_output.metadata` sau mỗi lượt và trả về trong
`result.chat_metadata`; job sau truyền ngược lại vào `client.start_chat(metadata=...)`
→ server nối vào đúng conversation cũ.

**Verify (2026-11-14):** 3/3 pool (`acc3`, `hoàngyen`, `windows`) PASS cả 2 mức:
- cùng 1 `ChatSession` trong 1 process → `cid` giống, nhớ ngữ cảnh
- 2 job orchestrator (2 process, 2 client khác nhau) → `cid` giống, nhớ ngữ cảnh

Test: `reports/_chat_2turn_test.py`, `reports/_chat_resume_test.py`,
`reports/_chat_orchestrator_test.py`.

---

## 7c. File đính kèm (upload) và file Gemini sinh ra (download)

Cả hai đã **verify thật** trên account `kimperiod0126` ngày 2026-11-14.

### 7c.1 Upload file lên Gemini

`gemini_webapi` hỗ trợ `files=` trên `generate_content` / `send_message`.
Gemini **đọc thật** nội dung file (không phải đoán mò từ tên file).

```powershell
python -m orchestrator.orch_cli submit chat `
  "Đọc file đính kèm. Tên class là gì? SECRET_NUMBER bằng bao nhiêu?" `
  --pool kimperiod0126 `
  --attach reports\_e2e_sample.py
```

**MCP:**

```
orch_submit(kind="chat", prompt="Đọc file đính kèm...", pool="kimperiod0126",
            attachments=["D:\\path\\to\\file.py"])
```

Kết quả trả về `result.attachments` (file đã gửi) và `result.files` (file Gemini
sinh ra — rỗng nếu Gemini không tạo file nào).

### 7c.2 Gemini sinh file `.xlsx` / `.pdf` / `.csv` → tải về được

**Có.** Gemini chạy code Python (`openpyxl`, `reportlab`) rồi đính kèm file kết
quả. File nằm trong rich content **field 59** của raw candidate:

```
candidate[12][0]["59"][0][0]
  [0]    = "[file-tag: code-generated-file-<uuid>]"
  [2]    = [None, 3, <filename>, <mime>, None, <base64 blob>, None,
            [thumb_url, download_url, upload_url]]
            [2][2]   = tên file, vd "Bao_cao_2026.pdf"
            [2][7][1] = URL tải
                       https://contribution.usercontent.google.com/download?...&filename=...
```

Module `orchestrator/gemini_files.py` đọc cấu trúc này; runner chat tự tải về
`reports/downloads/`.

**Đường tải (đã verify):** dùng chính session đã đăng nhập
(`client.client.get(url, headers={"Referer": "https://gemini.google.com/"})`)
→ HTTP 200, `content-type` đúng (`application/pdf`,
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `text/csv`).

**Lưu ý quan trọng:** `gemini_webapi` **không** expose file sinh ra trong
`ModelOutput` — `Candidate` chỉ có `images` / `generated_videos` /
`generated_media` / `citations`. Muốn lấy file phải đọc **raw candidate**
(`_parse_candidate`) — đó là lý do `orchestrator/gemini_files.py` tồn tại.

**Prompt nào sinh được file:** nói rõ "tạo file .xlsx / .pdf và gửi tôi file
đính kèm". Với PDF, prompt kiểu "viết và chạy Python dùng reportlab để sinh
PDF" cho kết quả ổn định hơn là "tạo báo cáo PDF" (cái sau trả về *immersive
document* HTML, không phải file PDF).

**Verify (2026-11-14):**

| Định dạng | Kết quả | Kiểm chứng nội dung |
|---|---|---|
| `.py` (upload) | ✅ Gemini đọc đúng, trả đúng `Circle` / `987654` | 3/3 câu |
| `.xlsx` | ✅ 7392 bytes | zip hợp lệ, có `xl/worksheets/sheet1.xml`, tiếng Việt đúng dấu |
| `.pdf` | ✅ 2012 bytes | header `%PDF-1.4`, có `%%EOF`, 1 trang |
| `.csv` | ✅ 310 bytes | header `Tên,Email,Tuổi,Thành phố` |

Test: `reports/_test_attach.py`, `reports/_test_extract.py`, `reports/_test_e2e.py`.

---

## 8. Tài liệu liên quan

- `docs/COOKIE-SETUP.md` — quy trình gốc (dán cookie JSON)
- `docs/HANDOFF.md` — context tổng thể dự án
- `orchestrator/harvest.py` — module thu hoạch + tự chữa
- `orchestrator/cookie_sync.py` — đọc/ghi cookie, purge cache, probe session
- `orchestrator/board.py` — read-model cho dashboard
