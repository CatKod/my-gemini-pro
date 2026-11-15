// Popup — cấu hình + nút gửi cookie thủ công.
//
// Logic gửi nằm trong `background.js` (service worker) để dùng chung với
// chế độ tự động. Popup chỉ gọi qua `chrome.runtime.sendMessage`.

const el = (id) => document.getElementById(id);

function setStatus(text, cls) {
  const s = el("status");
  s.textContent = text;
  s.className = cls || "";
}

async function loadSaved() {
  const saved = await chrome.storage.local.get(["endpoint", "token", "autoSend", "account"]);
  if (saved.endpoint) el("endpoint").value = saved.endpoint;
  if (saved.token) el("token").value = saved.token;
  if (saved.account) el("account").value = saved.account;
  el("auto").checked = saved.autoSend === true;
  if (saved.token) {
    setStatus(
      saved.autoSend
        ? "Đã bật chế độ tự động — mỗi khi bạn mở tab Gemini, cookie tự được gửi."
        : "Đã lưu token. Bấm 'Gửi cookie' để gửi ngay, hoặc bật chế độ tự động.",
      "ok"
    );
  }
}

// Tự dò server khi endpoint còn trống, rồi hiển thị endpoint tìm được.
async function autoDiscover() {
  const saved = await chrome.storage.local.get(["endpoint"]);
  if (saved.endpoint) return;
  const res = await chrome.runtime.sendMessage({ type: "PING" });
  if (res && res.ok && res.endpoint) {
    el("endpoint").value = res.endpoint;
    setStatus("Đã tìm thấy server: " + res.endpoint + "\nDán token rồi bấm 'Gửi cookie'.", "ok");
  }
}

async function saveConfig() {
  await chrome.storage.local.set({
    endpoint: el("endpoint").value.trim(),
    token: el("token").value.trim(),
    account: el("account").value.trim(),
    autoSend: el("auto").checked,
  });
  setStatus("Đã lưu cấu hình.", "ok");
}

async function send() {
  let endpoint = el("endpoint").value.trim();
  let token = el("token").value.trim();

  // Chưa có token → tự lấy từ server (endpoint `/config` chỉ phục vụ extension).
  if (!token) {
    const res = await chrome.runtime.sendMessage({ type: "PING" });
    if (res && res.ok) {
      if (res.endpoint) {
        endpoint = res.endpoint;
        el("endpoint").value = endpoint;
      }
      if (res.token) {
        token = res.token;
        el("token").value = token;
      }
    }
  }

  if (!endpoint || !token) {
    setStatus(
      "Chưa kết nối được server.\n" +
      "Hãy chạy MyGeminiWebMcp (đã đăng ký trong mcp.json), rồi bấm lại.",
      "err"
    );
    return;
  }
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//.test(endpoint)) {
    setStatus("Endpoint phải là http://127.0.0.1/... (loopback).", "err");
    return;
  }

  // Lưu trước để background dùng đúng cấu hình.
  await chrome.storage.local.set({
    endpoint,
    token,
    account: el("account").value.trim(),
    autoSend: el("auto").checked,
  });

  const btn = el("send");
  btn.disabled = true;
  setStatus("Đang gửi...");

  try {
    const res = await chrome.runtime.sendMessage({ type: "SEND_COOKIES" });
    if (res && res.ok) {
      const d = res.data || {};
      setStatus(
        "OK — đã lưu vào account '" + (d.account || "?") + "'\n" +
        "Số cookie: " + (d.cookieCount || "?") +
        "\nCó __Secure-1PSIDTS: " + (d.hasPsidts ? "có" : "không"),
        "ok"
      );
    } else {
      const err = (res && res.error) || "không rõ lý do";
      const msg =
        err === "no-psid"
          ? "Không thấy cookie __Secure-1PSID.\nMở tab https://gemini.google.com/app và đăng nhập trước."
          : err === "no-token"
          ? "Chưa có token."
          : err === "bad-endpoint"
          ? "Endpoint phải là loopback."
          : "Không gửi được: " + err +
            "\n\nKiểm tra MCP server có đang chạy không\n" +
            "(token + endpoint in ra trên stderr khi server khởi động).";
      setStatus(msg, "err");
    }
  } catch (e) {
    setStatus("Lỗi: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

async function ping() {
  const btn = el("ping");
  btn.disabled = true;
  setStatus("Đang kiểm tra...");
  try {
    const res = await chrome.runtime.sendMessage({ type: "PING" });
    if (res && res.ok) {
      if (res.endpoint) el("endpoint").value = res.endpoint;
      setStatus(
        "Server đang chạy tại " + res.endpoint +
          (res.discovered ? "\n(endpoint đã được tự động lưu)" : ""),
        "ok"
      );
    } else {
      setStatus(
        "Không kết nối được: " + ((res && res.error) || "không rõ lý do") +
          "\nHãy chạy MyGeminiWebMcp (mcp.json đã đăng ký sẵn).",
        "err"
      );
    }
  } catch (e) {
    setStatus("Lỗi: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

el("send").addEventListener("click", send);
el("ping").addEventListener("click", ping);
el("save").addEventListener("click", saveConfig);
el("auto").addEventListener("change", saveConfig);
loadSaved().then(autoDiscover);
