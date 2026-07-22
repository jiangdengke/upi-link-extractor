const loginView = document.querySelector("#login-view");
const adminView = document.querySelector("#admin-view");
const loginForm = document.querySelector("#login-form");
const loginMessage = document.querySelector("#login-message");
const generateForm = document.querySelector("#generate-form");
const generateMessage = document.querySelector("#generate-message");
const cdkTable = document.querySelector("#cdk-table");
const generatedBox = document.querySelector("#generated-box");
const generatedCodes = document.querySelector("#generated-codes");
const adminToast = document.querySelector("#admin-toast");
let toastTimer;

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  adminToast.textContent = message;
  adminToast.hidden = false;
  toastTimer = window.setTimeout(() => { adminToast.hidden = true; }, 2200);
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

function setAuthenticated(authenticated) {
  loginView.hidden = authenticated;
  adminView.hidden = !authenticated;
}

function formatTime(epoch) {
  if (!epoch) return "永不过期";
  const date = new Date(epoch * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function cdkStatus(item) {
  if (item.revoked) return ["已停用", "status-bad"];
  if (item.expires_at && item.expires_at * 1000 <= Date.now()) return ["已过期", "status-bad"];
  if (item.remaining_uses <= 0) return ["已用完", "status-bad"];
  return ["可用", "status-good"];
}

function renderCdks(items) {
  cdkTable.replaceChildren();
  for (const item of items) {
    const row = document.createElement("tr");
    const codeCell = document.createElement("td");
    codeCell.append(node("code", "", item.code));
    const usageCell = node("td", "", `${item.remaining_uses}/${item.max_uses}`);
    if (item.reserved_uses) usageCell.title = `运行中占用 ${item.reserved_uses} 次`;
    const [label, className] = cdkStatus(item);
    const statusCell = node("td", className, label);
    const expiryCell = node("td", "", formatTime(item.expires_at));
    const noteCell = node("td", "", item.note || "—");
    const actionsCell = node("td", "table-actions");
    const copy = node("button", "secondary compact", "复制");
    copy.type = "button";
    copy.addEventListener("click", () => copyText(item.code).then(() => showToast("CDK 已复制")));
    const toggle = node("button", `${item.revoked ? "" : "cancel"} compact`, item.revoked ? "启用" : "停用");
    toggle.type = "button";
    toggle.addEventListener("click", async () => {
      await request(`/api/admin/cdks/${encodeURIComponent(item.code)}/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revoked: !item.revoked }),
      });
      await loadCdks();
    });
    actionsCell.append(copy, toggle);
    row.append(codeCell, usageCell, statusCell, expiryCell, noteCell, actionsCell);
    cdkTable.append(row);
  }
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = node("td", "", "还没有 CDK。");
    cell.colSpan = 6;
    row.append(cell);
    cdkTable.append(row);
  }
}

async function loadCdks() {
  const data = await request("/api/admin/cdks", { cache: "no-store" });
  renderCdks(data.items || []);
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginMessage.textContent = "正在登录…";
  loginMessage.className = "message";
  try {
    await request("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.querySelector("#admin-password").value }),
    });
    document.querySelector("#admin-password").value = "";
    setAuthenticated(true);
    await loadCdks();
  } catch (error) {
    loginMessage.textContent = error.message || String(error);
    loginMessage.className = "message error";
  }
});

generateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  generateMessage.textContent = "正在生成…";
  generateMessage.className = "message";
  try {
    const data = await request("/api/admin/cdks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        count: Number(document.querySelector("#cdk-count").value),
        max_uses: Number(document.querySelector("#cdk-uses").value),
        expires_in_days: Number(document.querySelector("#cdk-days").value),
        prefix: document.querySelector("#cdk-prefix").value,
        note: document.querySelector("#cdk-note").value,
      }),
    });
    generatedCodes.value = data.items.map((item) => item.code).join("\n");
    generatedBox.hidden = false;
    generateMessage.textContent = `已生成 ${data.count} 个`;
    await loadCdks();
  } catch (error) {
    generateMessage.textContent = error.message || String(error);
    generateMessage.className = "message error";
  }
});

document.querySelector("#copy-generated").addEventListener("click", async () => {
  await copyText(generatedCodes.value);
  showToast("本批 CDK 已全部复制");
});
document.querySelector("#refresh-cdks").addEventListener("click", () => loadCdks());
document.querySelector("#logout-button").addEventListener("click", async () => {
  await request("/api/admin/logout", { method: "POST" });
  setAuthenticated(false);
});

async function initialize() {
  try {
    const session = await request("/api/admin/session", { cache: "no-store" });
    setAuthenticated(session.authenticated);
    if (session.authenticated) await loadCdks();
    if (!session.configured) {
      loginMessage.textContent = "服务器尚未配置 UPI_ADMIN_PASSWORD。";
      loginMessage.className = "message error";
    }
  } catch (error) {
    loginMessage.textContent = error.message || String(error);
    loginMessage.className = "message error";
  }
}

initialize();
