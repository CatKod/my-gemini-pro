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

// Bề rộng dải dò. Dải rộng vì port có thể đã được cấu hình (biến môi trường
// GEMINIWEB_MCP_BRIDGE_PORT), nên đừng giả định nó nằm ngay cạnh mặc định.
const PORT_SCAN_RANGE = 100;

// Dải port để dò.
//
// Thứ tự ưu tiên: port trong storage (người dùng đã cấu hình) → port mặc định.
// Một dải rộng vì port có thể đã được cấu hình qua biến môi trường
// GEMINIWEB_MCP_BRIDGE_PORT, nên đừng giả định nó nằm ngay cạnh mặc định.
function portCandidates(savedPort) {
  const start = Number(savedPort) || DEFAULT_PORT;
  const list = [];
  for (let port = start; port < start + PORT_SCAN_RANGE; port++) list.push(port);
  for (let port = DEFAULT_PORT; port < DEFAULT_PORT + PORT_SCAN_RANGE; port++) {
    if (!list.includes(port)) list.push(port);
  }
  return list;
}

// Cookie được giữ lại khi gửi đi. `NID` và họ `__Secure-3PSID*` phải đi kèm:
// session chỉ có `__Secure-1PSID` sẽ bị Google từ chối.
const KEEP_COOKIES = [
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "NID",
  "COMPASS",
  // Cookie của Google Accounts: danh sách tài khoản đã đăng nhập trong profile.
  // Cần để xem Chrome lưu session thế nào cho từng tài khoản.
  "ACCOUNT_CHOOSER",
  "__Host-1PLSID",
  "__Host-3PLSID",
  "__Host-GAPS",
  "OSID",
  "__Secure-OSID",
];

async function getConfig() {
  const cfg = await chrome.storage.local.get(["endpoint", "token", "autoSend", "account", "lastSync", "lastResult"]);
  return {
    endpoint: cfg.endpoint || "",
    token: cfg.token || "",
    // Auto-send is on by default: the point of the bridge is that the pool heals
    // itself, so the user should not have to press a button every time a session
    // expires. The popup toggle turns it off.
    autoSend: cfg.autoSend !== false,
    account: cfg.account || "",
    lastSync: cfg.lastSync || "",
    lastResult: cfg.lastResult || "",
  };
}

// Chu ky dong bo ngam: 20 phut — khop voi SessionKeepAlive cua server (~20p).
const AUTO_SYNC_MINUTES = 20;

async function ensureAutoSync() {
  try {
    await chrome.alarms.create("geminiweb-sync", { periodInMinutes: AUTO_SYNC_MINUTES });
  } catch {
    // Trinh duyet cu khong co alarms — bo qua, van con che do gui khi mo tab.
  }
}

// Dò port của bridge: thử `/ping` trên các port ứng viên.
// Trả về `{baseUrl, port}` của server đang sống, hoặc null.
async function discoverBridge(savedPort) {
  for (const port of portCandidates(savedPort)) {
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

  const savedPort = Number((cfg.endpoint || "").split(":")[2]) || 0;
  const found = await discoverBridge(savedPort);
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

// Đọc toàn bộ cookie store của profile, kèm các tab thuộc từng store.
//
// QUAN TRỌNG: Chrome phân vùng cookie theo tài khoản Google đã đăng nhập. Mỗi
// tài khoản có một store riêng, và store đó gắn với các tab đang mở của tài
// khoản đó qua `tabIds`. `chrome.cookies.getAll({url})` KHÔNG phân biệt được các
// store này — nó luôn trả về tài khoản đang active. Vì vậy phải đọc từng store,
// rồi ghép store với tab qua `tabIds`.
async function collectStores() {
  const stores = [];
  try {
    const allStores = await chrome.cookies.getAllCookieStores();
    for (const store of allStores) {
      if (store.incognito) continue;

      let storeCookies = [];
      try {
        storeCookies = await chrome.cookies.getAll({ storeId: store.id });
      } catch {
        continue;
      }

      const map = new Map();
      const rankMap = new Map();
      for (const c of storeCookies) {
        if (!c.name.startsWith("__Secure-1PSID") && !c.name.startsWith("__Secure-3PSID")) continue;
        const rank = c.path === "/" ? 0 : 1;
        if (!map.has(c.name) || (rankMap.get(c.name) || 0) < rank) {
          map.set(c.name, c.value);
          rankMap.set(c.name, rank);
        }
      }

      const storePsid = map.get("__Secure-1PSID");

      stores.push({
        storeId: store.id,
        tabIds: store.tabIds || [],
        cookieCount: storeCookies.length,
        hasPsid: !!storePsid,
        psid: storePsid || "",
        psidts: map.get("__Secure-1PSIDTS") || "",
        psid3: map.get("__Secure-3PSID") || "",
        psid3ts: map.get("__Secure-3PSIDTS") || "",
      });
    }
  } catch {
    // Không lấy được danh sách store — dùng các path slot đã quét.
  }

  return stores;
}

// Đọc email của tài khoản đang đăng nhập, từ chính tab đó.
async function emailOfTab(tab) {
  // 1. Nút account switcher: nhãn chứa cả tên lẫn email.
  try {
    const response = await chrome.tabs.sendMessage(tab.id, { type: "GET_ACCOUNT_EMAIL" });
    if (response && response.email) return response.email;
  } catch {
    // Content script chưa được inject vào tab này.
  }

  // 2. Tiêu đề tab.
  if (tab.title) {
    const m = tab.title.match(/[\w.+-]+@[\w-]+\.[\w.]+/);
    if (m) return m[0];
  }

  return "";
}

// Đọc cookie của tài khoản đang đăng nhập trong tab Gemini.
//
// `chrome.cookies.getAll` trả về cookie của mọi profile trên máy. Server bên
// nhận sẽ tự khớp account bằng `__Secure-1PSID`, nên gửi đủ bộ cookie để
// server có thể dò account.
async function collectCookies() {
  // QUAN TRỌNG: phải lấy cookie theo `url`, không theo `domain`.
  //
  // Một Chrome profile đăng nhập nhiều tài khoản Google sẽ lưu cookie phiên của
  // mỗi tài khoản ở path riêng trên `.google.com`: account chính ở `/`, các
  // account còn lại ở `/u/1`, `/u/2`, ... (đó là lý do Gemini mở `/u/1/app` cho
  // account thứ hai). `chrome.cookies.getAll({domain})` bỏ qua path, nên chỉ thấy
  // session của account chính. `getAll({url})` mới trả về đúng cookie mà trình
  // duyệt sẽ gửi cho URL đó.
  //
  // QUAN TRỌNG HƠN: Chrome phân vùng cookie theo tài khoản đã đăng nhập. Mỗi
  // tài khoản có một cookie store riêng, và store đó gắn với các tab đang mở
  // của tài khoản đó qua `tabIds`. `getAll({url})` KHÔNG phân biệt được các store
  // này — nó luôn trả về tài khoản đang active. Vì vậy phải đọc từng store, rồi
  // ghép store với tab qua `tabIds`.
  const stores = await collectStores();
  const sessions = await collectSessions();
  if (sessions.length === 0) return { ok: false, error: "no-psid" };

  const primary = sessions[0];
  const url = primary.url;
  const all = await chrome.cookies.getAll({ url });

  // `primary` đã được lọc theo path ưu tiên, nên dùng luôn giá trị đó.
  const psid = primary.psid;
  const psidts = primary.psidts;
  const cookies = primary.cookies;

  // Nhiều tài khoản Google trong cùng một Chrome profile: Google giữ session của
  // tài khoản đang active ở cookie `__Secure-1PSID`, còn các tài khoản khác nằm ở
  // cookie cùng tên nhưng khác `path` (mỗi tài khoản một "slot" trên
  // gemini.google.com/u/<n>). Gửi kèm danh sách đầy đủ để server có thể thử các
  // biến thể và tìm đúng session của tài khoản vừa được chuyển.
  const variants = [];
  for (const c of all) {
    if (!c.name.startsWith("__Secure-1PSID") && !c.name.startsWith("__Secure-3PSID")) continue;
    variants.push({
      name: c.name,
      value: c.value,
      path: c.path,
      domain: c.domain,
      storeId: c.storeId,
      hostOnly: c.hostOnly,
    });
  }

  // Quét thêm các path `/u/<n>` trên .google.com: mỗi tài khoản Google phụ trong
  // profile có session riêng nằm ở path riêng. Đọc cookie cho từng URL đó sẽ ra
  // đúng session của tài khoản tương ứng.
  const slots = [];
  for (let n = 1; n <= 10; n++) {
    const slotUrl = `https://www.google.com/u/${n}/`;
    let slotCookies;
    try {
      slotCookies = await chrome.cookies.getAll({ url: slotUrl });
    } catch {
      continue;
    }
    const map = new Map();
    const rankMap = new Map();
    for (const c of slotCookies) {
      if (!c.name.startsWith("__Secure-1PSID") && !c.name.startsWith("__Secure-3PSID")) continue;
      // Ưu tiên cookie có path riêng (`/u/1/`) hơn cookie path `/` của account chính.
      const rank = c.path === "/" ? 0 : 1;
      if (!map.has(c.name) || (rankMap.get(c.name) || 0) < rank) {
        map.set(c.name, c.value);
        rankMap.set(c.name, rank);
      }
    }
    const slotPsid = map.get("__Secure-1PSID");
    if (!slotPsid) continue;
    slots.push({
      slot: n,
      psid: slotPsid,
      psidts: map.get("__Secure-1PSIDTS") || "",
      psid3: map.get("__Secure-3PSID") || "",
      psid3ts: map.get("__Secure-3PSIDTS") || "",
    });
  }

  // DEBUG: liệt kê toàn bộ cookie .google.com (tên + path + 12 ký tự đầu giá trị).
  // Chỉ dùng để tìm hiểu cách Chrome lưu session của nhiều tài khoản.
  const debug = all.map((c) => ({
    name: c.name,
    path: c.path,
    storeId: c.storeId,
    prefix: (c.value || "").slice(0, 100),
  }));

  return {
    ok: true,
    psid,
    psidts,
    cookies,
    variants,
    slots,
    stores,
    debug,
    sessions,
    email: primary.email || "",
    allNames: Array.from(new Set(all.map((c) => c.name))),
  };
}

// Doc cookie ma khong can tab nao mo — dung cho alarms chay ngam.
//
// Tab co the khong co (Chrome chay nen ma khong mo Gemini). chrome.cookies
// van doc duoc vi no chay TRONG browser. Email de server tu resolve qua
// slot URL; neu co tab Gemini mo thi van kem sessions de ghep email chinh xac.
async function collectCookiesTabless() {
  let all = [];
  try {
    all = await chrome.cookies.getAll({ url: "https://gemini.google.com/app" });
  } catch {
    return { ok: false, error: "no-psid" };
  }

  const byName = new Map();
  const byPathRank = new Map();
  for (const c of all) {
    const rank = c.path === "/" ? 0 : 1;
    if (!byName.has(c.name) || (byPathRank.get(c.name) || 0) < rank) {
      byName.set(c.name, c.value);
      byPathRank.set(c.name, rank);
    }
  }

  const psid = byName.get("__Secure-1PSID");
  if (!psid) return { ok: false, error: "no-psid" };

  const cookies = {};
  for (const name of KEEP_COOKIES) {
    const value = byName.get(name);
    if (value) cookies[name] = value;
  }

  const variants = [];
  for (const c of all) {
    if (!c.name.startsWith("__Secure-1PSID") && !c.name.startsWith("__Secure-3PSID")) continue;
    variants.push({
      name: c.name,
      value: c.value,
      path: c.path,
      domain: c.domain,
      storeId: c.storeId,
      hostOnly: c.hostOnly,
    });
  }

  const slots = [];
  for (let n = 1; n <= 10; n++) {
    const slotUrl = `https://www.google.com/u/${n}/`;
    let slotCookies;
    try {
      slotCookies = await chrome.cookies.getAll({ url: slotUrl });
    } catch {
      continue;
    }
    const map = new Map();
    const rankMap = new Map();
    for (const c of slotCookies) {
      if (!c.name.startsWith("__Secure-1PSID") && !c.name.startsWith("__Secure-3PSID")) continue;
      const rank = c.path === "/" ? 0 : 1;
      if (!map.has(c.name) || (rankMap.get(c.name) || 0) < rank) {
        map.set(c.name, c.value);
        rankMap.set(c.name, rank);
      }
    }
    const slotPsid = map.get("__Secure-1PSID");
    if (!slotPsid) continue;
    slots.push({
      slot: n,
      psid: slotPsid,
      psidts: map.get("__Secure-1PSIDTS") || "",
      psid3: map.get("__Secure-3PSID") || "",
      psid3ts: map.get("__Secure-3PSIDTS") || "",
    });
  }

  // Co tab nao mo thi kem sessions de server ghep email chinh xac.
  // Khong co tab cung khong sao: server tu resolve email qua slot URL.
  let sessions = [];
  try {
    sessions = await collectSessions();
  } catch {
    sessions = [];
  }

  const stores = await collectStores();
  const debug = all.map((c) => ({
    name: c.name,
    path: c.path,
    storeId: c.storeId,
    prefix: (c.value || "").slice(0, 100),
  }));

  return {
    ok: true,
    psid,
    psidts: byName.get("__Secure-1PSIDTS") || "",
    cookies,
    variants,
    slots,
    stores,
    debug,
    sessions,
    email: sessions.length > 0 ? (sessions[0].email || "") : "",
    allNames: Array.from(new Set(all.map((c) => c.name))),
    tabless: true,
  };
}

async function recordSync(ok, detail) {
  try {
    await chrome.storage.local.set({
      lastSync: new Date().toISOString(),
      lastResult: ok ? ("OK" + (detail ? " — " + detail : "")) : ("Lỗi: " + (detail || "?")),
    });
  } catch {
    // Storage loi thi bo qua — lan sau ghi lai.
  }
}

// LƯU Ý: extension KHÔNG BAO GIỜ tự mở tab. Mở + đóng tab Gemini liên tục
// tạo ra pattern bất thường mà Google phát hiện được (nhiều lần tạo trang
// rồi bị bỏ trong thời gian ngắn). Người dùng tự điều hướng; extension chỉ đọc
// cookie của những tab đã mở sẵn.

// Đọc email + cookie của một tab đang mở.
//
// Ràng buộc: email và cookie phải đến từ CÙNG một tab. Lấy cookie từ tab `/app`
// nhưng email từ tab `/u/1/app` sẽ gắn sai session cho account.
async function readTabSession(tabId, url) {
  let all = [];
  try {
    all = await chrome.cookies.getAll({ url });
  } catch {
    return null;
  }

  const byName = new Map();
  const byPathRank = new Map();
  for (const c of all) {
    const rank = c.path === "/" ? 0 : 1;
    if (!byName.has(c.name) || (byPathRank.get(c.name) || 0) < rank) {
      byName.set(c.name, c.value);
      byPathRank.set(c.name, rank);
    }
  }

  const psid = byName.get("__Secure-1PSID");
  if (!psid) return null;

  const cookies = {};
  for (const name of KEEP_COOKIES) {
    const value = byName.get(name);
    if (value) cookies[name] = value;
  }

  let email = "";
  try {
    const tab = await chrome.tabs.get(tabId);
    email = await emailOfTab(tab);
  } catch {
    // Không đọc được thông tin tab.
  }
  // Ghi lại path của từng cookie: Chrome có thể lưu session của tài khoản phụ ở
  // path riêng (`/u/1/`) thay vì tách cookie store. Chỉ để chẩn đoán.
  const paths = [];
  for (const c of all) {
    if (!c.name.startsWith("__Secure-1PSID") && !c.name.startsWith("__Secure-3PSID")) continue;
    paths.push(`${c.name}@${c.path}[${c.storeId}]=${(c.value || "").slice(0, 24)}`);
  }

  return {
    url,
    email,
    psid,
    psidts: byName.get("__Secure-1PSIDTS") || "",
    cookies,
    paths,
  };
}

// Đọc session của TẤT CẢ tab Gemini đang mở.
//
// Chỉ đọc, không điều hướng, không mở tab. Người dùng chủ động chuyển tài khoản
// (bằng cách mở `/u/1/app`, `/u/2/app`…) thì mỗi lần chuyển sẽ thu hoạch được
// session của tài khoản tương ứng.
//
// Tab đang active được xếp đầu tiên: đó chính là tab người dùng vừa bấm
// "Gửi cookie", nên server lấy nó làm `primary` trong kết quả trả về.
async function collectSessions() {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: "https://gemini.google.com/*" });
  } catch {
    return [];
  }

  // Tab active (tab người dùng đang nhìn) — ưu tiên lên đầu để `primary`
  // chính là tài khoản vừa bấm gửi, không phải tab mở đầu tiên.
  let activeId = 0;
  try {
    const active = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (active && active.length > 0 && active[0] && active[0].id) activeId = active[0].id;
  } catch {
    // Không lấy được tab active — giữ thứ tự cũ.
  }
  if (activeId) {
    tabs = [...tabs].sort((a, b) => (b.id === activeId) - (a.id === activeId));
  }

  const sessions = [];
  const seen = new Set();

  for (const tab of tabs) {
    const session = await readTabSession(tab.id, tab.url);
    if (!session) continue;

    // Hai tài khoản khác nhau có thể dùng cùng một `__Secure-1PSID` (Google chỉ
    // phân biệt bằng `__Secure-1PSIDTS` + đường dẫn). Vì vậy khoá theo cặp
    // (psid, psidts), không theo psid đơn thuần.
    const key = session.psid + "|" + session.psidts;
    if (seen.has(key)) continue;
    seen.add(key);

    sessions.push(session);
  }

  return sessions;
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

  // Tabless truoc: doc thang cookie store ma khong can tab mo.
  // Che do nay chay duoc trong alarms (khong can tab Gemini nao mo).
  let harvested = await collectCookiesTabless();
  if (!harvested.ok) harvested = await collectCookies();
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
        variants: harvested.variants,
        slots: harvested.slots,
        stores: harvested.stores,
        sessions: harvested.sessions,
        debug: harvested.debug,
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
  if (!cfg.autoSend) return;
  // The token is discovered on the first attempt, so a fresh install works
  // without the user ever opening the popup.
  if (!cfg.token) {
    const discovered = await resolveEndpoint();
    if (!discovered.token) return;
  }
  const r = await sendCookies({ quiet: true });
  const d = (r && r.data) || {};
  await recordSync(
    !!(r && r.ok),
    r && r.ok
      ? "đã lưu " + ((d.accounts && d.accounts.length) ? d.accounts.join(", ") : d.account || "?")
      : (r && (r.error || "gửi thất bại"))
  );
});

// Đồng bộ ngầm theo chu kỳ: KHÔNG cần người dùng bấm popup, không cần mở
// tab Gemini. Service worker của Chrome tự thức dậy theo alarm, đọc cookie
// trong browser rồi POST về bridge local.
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (!alarm || alarm.name !== "geminiweb-sync") return;
  const cfg = await getConfig();
  if (!cfg.autoSend) return;

  // Tự tìm endpoint/token nếu chưa có (sau khi browser restart, storage vẫn
  // còn nhưng token có thể đã đổi).
  let endpoint = cfg.endpoint;
  let token = cfg.token;
  if (!token) {
    const discovered = await resolveEndpoint();
    endpoint = discovered.endpoint;
    token = discovered.token;
  }
  if (!token || !endpoint) {
    await recordSync(false, "chưa tìm thấy bridge local");
    return;
  }

  const r = await sendCookies({ quiet: true });
  const d = (r && r.data) || {};
  await recordSync(
    !!(r && r.ok),
    r && r.ok
      ? "đã lưu " + ((d.accounts && d.accounts.length) ? d.accounts.join(", ") : d.account || "?")
      : (r && (r.error || "gửi thất bại"))
  );
});

// Khi extension vừa được cài (hoặc reload), tự tìm server và lấy token.
// Điều này làm cho lần cài đặt đầu tiên không cần thao tác tay: chỉ cần
// mở một tab Gemini là cookie được gửi.
chrome.runtime.onInstalled.addListener(async () => {
  await resolveEndpoint();
  await ensureAutoSync();
  // Đồng bộ ngay lần đầu để pool có cookie sẵn, không phải chờ 20 phút.
  const cfg = await getConfig();
  if (cfg.autoSend) {
    const r = await sendCookies({ quiet: true });
    const d = (r && r.data) || {};
    await recordSync(
      !!(r && r.ok),
      r && r.ok
        ? "đã lưu " + ((d.accounts && d.accounts.length) ? d.accounts.join(", ") : d.account || "?")
        : (r && (r.error || "gửi thất bại"))
    );
  }
});
chrome.runtime.onStartup.addListener(async () => {
  await resolveEndpoint();
  await ensureAutoSync();
});

// Service worker có thể bị Chrome đóng và khởi động lại — đảm bảo alarm còn
// tồn tại mỗi khi worker thức dậy.
ensureAutoSync();

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
      const found = await discoverBridge(
        Number((cfg.endpoint || "").split(":")[2]) || 0
      );
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
