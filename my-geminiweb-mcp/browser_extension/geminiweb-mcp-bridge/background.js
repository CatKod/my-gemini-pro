// Background service worker — đọc cookie Gemini trong browser và gửi về
// MyGeminiWebMcp trên 127.0.0.1.
//
// Vì sao cần extension:
//   Chrome/Edge mã hoá cookie trên đĩa bằng App-Bound Encryption (prefix `v20`) —
//   không process nào ngoài trình duyệt đọc được. Extension chạy BÊN TRONG trình
//   duyệt nên dùng được `chrome.cookies`, tức chính browser giải mã giúp.
//
// Ràng buộc an toàn:
//   - chỉ đọc cookie `.google.com`
//   - chỉ POST tới `http://127.0.0.1:*`
//   - mặc định TẮT; user bật ở popup (1 lần)
//   - không log cookie ra console

const AUTH_COOKIES = ["__Secure-1PSID", "__Secure-1PSIDTS"];
const GEMINI_HOSTS = ["gemini.google.com", "accounts.google.com"];

// Port mặc định của bridge C# + số dự phòng. Server tự dời sang port trống khi
// port mặc định bị chiếm (ví dụ instance cũ chưa tắt), nên extension phải dò
// thay vì hard-code.
const DEFAULT_PORT = 9478;
const PORT_SCAN_RANGE = 20;

// Cookie được giữ lại khi gửi đi. `NID` và họ `__Secure-3PSID*` phải đi kèm:
// session chỉ có `__Secure-1PSID` sẽ bị Google từ chối.
const KEEP_COOKIES = [
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "NID",
  "COMPASS",
];

async function getConfig() {
  const cfg = await chrome.storage.local.get(["endpoint", "token", "autoSend", "account"]);
  return {
    endpoint: cfg.endpoint || "",
    token: cfg.token || "",
    autoSend: cfg.autoSend === true,
    account: cfg.account || "",
  };
}

// Dò port của bridge: thử `/ping` trên port mặc định rồi các port kế tiếp.
// Trả về `{baseUrl, port}` của server đang sống, hoặc null.
async function discoverBridge() {
  for (let port = DEFAULT_PORT; port < DEFAULT_PORT + PORT_SCAN_RANGE; port++) {
    const base = `http://127.0.0.1:${port}`;
    try {
      const res = await fetch(base + "/ping", { method: "GET" });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        if (data && data.service === "my-geminiweb-mcp-bridge") {
          return { baseUrl: base, port };
        }
      }
    } catch {
      // Port này không có gì — thử port tiếp theo.
    }
  }
  return null;
}

// Lấy endpoint + token từ server (endpoint `/config` chỉ phục vụ extension origin).
async function fetchBridgeConfig(baseUrl) {
  try {
    const res = await fetch(baseUrl + "/config", { method: "GET" });
    if (!res.ok) return null;
    const data = await res.json().catch(() => ({}));
    if (data && data.ok && data.endpoint && data.token) {
      return { endpoint: data.endpoint, token: data.token };
    }
  } catch {
    // Không đọc được — người dùng sẽ dán tay.
  }
  return null;
}

// Endpoint + token đầy đủ, dùng để gửi cookie.
// Ưu tiên: token đã lưu → tự lấy từ server → dò port rồi lấy config.
async function resolveEndpoint() {
  const cfg = await getConfig();
  if (cfg.token && cfg.endpoint) return cfg;

  const found = await discoverBridge();
  if (!found) return cfg;

  const remote = await fetchBridgeConfig(found.baseUrl);
  if (remote) {
    await chrome.storage.local.set({
      endpoint: remote.endpoint,
      token: remote.token,
    });
    return { ...cfg, endpoint: remote.endpoint, token: remote.token };
  }

  return { ...cfg, endpoint: cfg.endpoint || `${found.baseUrl}/harvest` };
}

function isLoopback(url) {
  try {
    const u = new URL(url);
    return (
      (u.hostname === "127.0.0.1" || u.hostname === "localhost") &&
      (u.protocol === "http:" || u.protocol === "https:")
    );
  } catch {
    return false;
  }
}

// Đọc cookie của tài khoản đang đăng nhập trong tab Gemini.
//
// `chrome.cookies.getAll` trả về cookie của mọi profile trên máy. Server bên
// nhận sẽ tự khớp account bằng `__Secure-1PSID`, nên gửi đủ bộ cookie để
// server có thể dò account.
async function collectCookies() {
  const all = await chrome.cookies.getAll({ domain: ".google.com" });

  const byName = new Map();
  for (const c of all) {
    if (!byName.has(c.name)) byName.set(c.name, c.value);
  }

  const psid = byName.get("__Secure-1PSID");
  if (!psid) return { ok: false, error: "no-psid" };

  const cookies = {};
  for (const name of KEEP_COOKIES) {
    const value = byName.get(name);
    if (value) cookies[name] = value;
  }

  // Email (nếu đọc được) giúp server gắn cookie vào đúng account trong pool.
  let email = "";
  try {
    const tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
    for (const tab of tabs) {
      if (tab.title) {
        const m = tab.title.match(/[\w.+-]+@[\w-]+\.[\w.]+/);
        if (m) {
          email = m[0];
          break;
        }
      }
    }
  } catch {
    // Không lấy được email thì server sẽ tự khớp bằng __Secure-1PSID.
  }

  return {
    ok: true,
    psid,
    psidts: byName.get("__Secure-1PSIDTS") || "",
    cookies,
    email,
    allNames: Array.from(byName.keys()),
  };
}

async function sendCookies({ quiet = true } = {}) {
  const cfg = await resolveEndpoint();
  if (!cfg.token) {
    if (!quiet) console.warn("[bridge] chưa có token — mở popup và dán token.");
    return { ok: false, error: "no-token" };
  }
  if (!isLoopback(cfg.endpoint)) {
    if (!quiet) console.warn("[bridge] endpoint không phải loopback.");
    return { ok: false, error: "bad-endpoint" };
  }

  const harvested = await collectCookies();
  if (!harvested.ok) return harvested;

  try {
    const res = await fetch(cfg.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: cfg.token,
        account: cfg.account || "",
        email: harvested.email,
        psid: harvested.psid,
        psidts: harvested.psidts,
        cookies: harvested.cookies,
        source: "geminiweb-mcp-extension",
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!quiet) {
      console.info(
        "[bridge] " + (res.ok && data.ok ? "OK" : "lỗi") +
        " account=" + (data.account || "-") +
        " cookies=" + (data.cookieCount || "?")
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
  if (msg && msg.type === "PING") {
    (async () => {
      const cfg = await getConfig();
      // Endpoint đã lưu → ping đúng chỗ đó, kèm lấy token nếu chưa có.
      if (cfg.endpoint) {
        const base = cfg.endpoint.replace(/\/harvest.*$/, "");
        try {
          const res = await fetch(base + "/ping");
          if (res.ok) {
            const remote = await fetchBridgeConfig(base);
            if (remote) {
              await chrome.storage.local.set({
                endpoint: remote.endpoint,
                token: remote.token,
              });
              respond({ ok: true, endpoint: remote.endpoint, token: remote.token, discovered: true });
              return;
            }
            respond({ ok: true, endpoint: cfg.endpoint, token: cfg.token });
            return;
          }
        } catch (e) {
          // Rơi xuống dò port bên dưới.
        }
      }
      // Chưa cấu hình → dò port rồi lấy config.
      const found = await discoverBridge();
      if (found) {
        const remote = await fetchBridgeConfig(found.baseUrl);
        if (remote) {
          await chrome.storage.local.set({
            endpoint: remote.endpoint,
            token: remote.token,
          });
          respond({ ok: true, endpoint: remote.endpoint, token: remote.token, discovered: true });
          return;
        }
        respond({ ok: true, endpoint: `${found.baseUrl}/harvest`, token: "" });
        return;
      }
      respond({
        ok: false,
        error: "không tìm thấy MyGeminiWebMcp bridge",
        endpoint: cfg.endpoint || "",
      });
    })();
    return true;
  }
  return false;
});
