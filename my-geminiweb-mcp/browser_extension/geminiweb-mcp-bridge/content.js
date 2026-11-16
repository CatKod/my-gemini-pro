// Đọc email của tài khoản Google đang đăng nhập, từ chính trang Gemini.
//
// Vì sao cần: một Chrome profile có thể chứa nhiều tài khoản Google, mỗi tài khoản
// một session cookie riêng. Extension không biết cookie nào thuộc tài khoản đang
// active — nhưng trang Gemini thì biết: nút account switcher có
// `aria-label="Tài khoản Google: <tên> (<email>)"`.
//
// Script này chỉ đọc DOM của trang đang mở, không gửi đi đâu, và chỉ trả lời
// service worker của chính extension này.

const EMAIL_PATTERN = /[\w.+-]+@[\w-]+\.[\w.]+/g;

function findEmail() {
  // 1. Nút account switcher: nhãn chứa cả tên lẫn email.
  const labelled = document.querySelectorAll("[aria-label]");
  for (const el of labelled) {
    const label = el.getAttribute("aria-label") || "";
    if (!/tài khoản google|google account/i.test(label)) continue;
    const m = label.match(EMAIL_PATTERN);
    if (m && m.length > 0) return m[0].toLowerCase();
  }

  // 2. Dự phòng: khối tài khoản trong menu đổi tài khoản.
  const menu = document.querySelector("[data-cta*='account'], [jsaction*='account']");
  if (menu) {
    const m = (menu.textContent || "").match(EMAIL_PATTERN);
    if (m && m.length > 0) return m[0].toLowerCase();
  }

  return "";
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  if (!msg || msg.type !== "GET_ACCOUNT_EMAIL") return false;
  respond({ ok: true, email: findEmail() });
  return true;
});
