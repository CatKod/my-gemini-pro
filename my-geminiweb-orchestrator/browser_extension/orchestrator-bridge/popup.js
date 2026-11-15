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
  const saved = await chrome.storage.local.get(["endpoint", "token", "autoSend"]);
  if (saved.endpoint) el("endpoint").value = saved.endpoint;
  if (saved.token) el("token").value = saved.token;
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

async function saveConfig() {
  await chrome.storage.local.set({
    endpoint: el("endpoint").value.trim(),
    token: el("token").value.trim(),
    autoSend: el("auto").checked,
  });
  setStatus("Đã lưu cấu hình.", "ok");
}

async function send() {
  const endpoint = el("endpoint").value.trim();
  const token = el("token").value.trim();
  if (!endpoint || !token) {
    setStatus("Thiếu endpoint hoặc token. Dán token rồi bấm 'Lưu cấu hình'.", "err");
    return;
  }
  if (!/^http:\/\/127\.0\.0\.1(:\d+)?\//.test(endpoint)) {
    setStatus("Endpoint phải là http://127.0.0.1/... (loopback).", "err");
    return;
  }

  // Lưu trước để background dùng đúng cấu hình.
  await chrome.storage.local.set({
    endpoint,
    token,
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
        "OK — đã ghi vào pool '" + (d.pool || "?") + "'" +
        (d.account ? " (account " + d.account + ")" : "") +
        "\nTrạng thái: " + (d.account_status || "?") +
        "\nCấy vào Chrome riêng: " + (d.adopted_to_chrome ? "có" : "không"),
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
            "\n\nKiểm tra orchestrator đang chạy:\n" +
            "  python -m orchestrator.cookie_bridge start";
      setStatus(msg, "err");
    }
  } catch (e) {
    setStatus("Lỗi: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

el("send").addEventListener("click", send);
el("save").addEventListener("click", saveConfig);
el("auto").addEventListener("change", saveConfig);
loadSaved();
