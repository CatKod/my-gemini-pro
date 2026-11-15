// Background service worker — tự gửi cookie khi người dùng mở Gemini.
//
// Mục tiêu của user: *"lúc nào tất cả các acc đã đăng nhập và xác thực đều
// luôn trong trạng thái available"* → không được bắt user bấm nút mỗi lần.
//
// Cơ chế: khi người dùng mở/refresh tab gemini.google.com (đã đăng nhập),
// extension đọc cookie qua `chrome.cookies` và POST về orchestrator local.
//
// Ràng buộc an toàn:
//   - chỉ đọc cookie `.google.com`
//   - chỉ gửi 2 cookie xác thực
//   - chỉ POST tới `http://127.0.0.1:*`
//   - mặc định TẮT; user bật ở popup (1 lần)
//   - không log cookie ra console

const REQUIRED = "__Secure-1PSID";
const OPTIONAL = "__Secure-1PSIDTS";
const GEMINI_HOSTS = ["gemini.google.com", "accounts.google.com"];

async function getConfig() {
  const cfg = await chrome.storage.local.get(["endpoint", "token", "autoSend"]);
  return {
    endpoint: cfg.endpoint || "http://127.0.0.1:9477/harvest",
    token: cfg.token || "",
    autoSend: cfg.autoSend === true,
  };
}

async function sendCookies({ quiet = true } = {}) {
  const cfg = await getConfig();
  if (!cfg.token) {
    if (!quiet) {
      console.warn("[bridge] chưa có token — mở popup và dán token.");
    }
    return { ok: false, error: "no-token" };
  }
  if (!/^http:\/\/127\.0\.0\.1(:\d+)?\//.test(cfg.endpoint)) {
    if (!quiet) console.warn("[bridge] endpoint không phải loopback.");
    return { ok: false, error: "bad-endpoint" };
  }

  try {
    const all = await chrome.cookies.getAll({ domain: ".google.com" });
    const map = {};
    for (const c of all) if (!map[c.name]) map[c.name] = c.value;
    const psid = map[REQUIRED];
    if (!psid) return { ok: false, error: "no-psid" };

    const res = await fetch(cfg.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: cfg.token,
        psid,
        psidts: map[OPTIONAL] || "",
        source: "browser-extension-auto",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!quiet) {
      console.info(
        "[bridge] " + (res.ok && data.ok ? "OK" : "lỗi") +
        " pool=" + (data.pool || "-") +
        " status=" + (data.account_status || "-")
      );
    }
    return { ok: res.ok && data.ok === true, data };
  } catch (e) {
    if (!quiet) console.warn("[bridge] gửi lỗi: " + e.message);
    return { ok: false, error: String(e.message || e) };
  }
}

// Người dùng mở/refresh tab Gemini -> tự gửi (nếu đã bật autoSend).
chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete" || !tab || !tab.url) return;
  let host;
  try {
    host = new URL(tab.url).hostname;
  } catch {
    return;
  }
  if (!GEMINI_HOSTS.includes(host)) return;
  const cfg = await getConfig();
  if (!cfg.autoSend || !cfg.token) return;
  await sendCookies({ quiet: true });
});

// Cho popup gọi lại (nút "Gửi cookie" dùng chung logic).
chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (msg && msg.type === "SEND_COOKIES") {
    sendCookies({ quiet: false }).then((r) => respond(r));
    return true; // giữ kênh mở
  }
  return false;
});
