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
const settingsForm = document.querySelector("#settings-form");
const settingsMessage = document.querySelector("#settings-message");
const settingsSummary = document.querySelector("#settings-summary");
const foargeForm = document.querySelector("#foarge-form");
const foargeSummary = document.querySelector("#foarge-summary");
const foargeMessage = document.querySelector("#foarge-message");
const foargeMasked = document.querySelector("#foarge-masked");
const foargeResults = document.querySelector("#foarge-results");
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

function cdkKindLabel(kind) {
  return kind === "foarge" ? "提链 + 支付" : "仅提链";
}

function renderCdks(items) {
  cdkTable.replaceChildren();
  for (const item of items) {
    const row = document.createElement("tr");
    const codeCell = document.createElement("td");
    codeCell.append(node("code", "", item.code));
    const kindCell = node("td", "", cdkKindLabel(item.kind));
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
    row.append(codeCell, kindCell, usageCell, statusCell, expiryCell, noteCell, actionsCell);
    cdkTable.append(row);
  }
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = node("td", "", "还没有 CDK。");
    cell.colSpan = 7;
    row.append(cell);
    cdkTable.append(row);
  }
}

async function loadCdks() {
  const data = await request("/api/admin/cdks", { cache: "no-store" });
  renderCdks(data.items || []);
}

async function loadSettings() {
  const data = await request("/api/admin/settings", { cache: "no-store" });
  document.querySelector("#settings-proxy-pool").value = (data.proxy_pool || []).join("\n");
  document.querySelector("#settings-login-proxy").value = data.login_proxy || "";
  document.querySelector("#settings-retries").value = data.approve_retries;
  document.querySelector("#settings-concurrency").value = data.approve_concurrency;
  document.querySelector("#settings-proxy-step").value = data.proxy_from_step;
  settingsSummary.textContent = `代理 ${data.proxy_pool.length} · 并发 ${data.approve_concurrency}`;
  settingsSummary.classList.add("ok");
}

async function loadFoarge() {
  const data = await request("/api/admin/foarge", { cache: "no-store" });
  foargeSummary.textContent = data.configured
    ? `可用 ${data.available_count} / 总计 ${data.configured_count}`
    : "未配置";
  foargeSummary.classList.toggle("ok", data.available_count > 0);
  foargeMasked.textContent = data.configured
    ? `可用 ${data.available_count} · 占用 ${data.reserved_count} · 已用 ${data.used_count}`
    : "尚未添加支付 PBK，支付型 CDK 暂不可用。";
  document.querySelector("#foarge-cdks").value = "";
  renderFoargeEntries(data.entries || []);
}

function foargeStatusLabel(status) {
  return ({ available: "可用", reserved: "占用中", used: "已用" })[status] || status;
}

function renderFoargeEntries(items) {
  foargeResults.replaceChildren();
  for (const item of items) {
    const row = node("div", "foarge-result");
    row.append(
      node("code", "", item.masked_cdk || "—"),
      node("span", `foarge-result-status ${item.status || ""}`, foargeStatusLabel(item.status)),
    );
    const detail = item.error
      ? item.error
      : item.ok === true
        ? `上游可用 · 剩余 ${item.uses_remaining ?? "未返回"} 次`
        : item.message || (item.upstream_status ? `上游状态 ${item.upstream_status}` : "");
    if (detail) row.append(node("span", "foarge-result-detail", detail));
    foargeResults.append(row);
  }
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
    await Promise.all([loadCdks(), loadSettings(), loadFoarge()]);
  } catch (error) {
    loginMessage.textContent = error.message || String(error);
    loginMessage.className = "message error";
  }
});

foargeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  foargeMessage.textContent = "正在保存…";
  foargeMessage.className = "message";
  try {
    await request("/api/admin/foarge", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cdks: document.querySelector("#foarge-cdks").value || null,
        clear: false,
      }),
    });
    foargeMessage.textContent = "一次性 CDK 已添加";
    await loadFoarge();
  } catch (error) {
    foargeMessage.textContent = error.message || String(error);
    foargeMessage.className = "message error";
  }
});

document.querySelector("#foarge-check").addEventListener("click", async () => {
  foargeMessage.textContent = "正在检查上游…";
  foargeMessage.className = "message";
  try {
    const data = await request("/api/admin/foarge/check", { method: "POST" });
    renderFoargeEntries(data.items || []);
    const available = (data.items || []).filter((item) => item.status === "available" && item.ok).length;
    foargeMessage.textContent = `检查完成 · 上游可用 ${available} 个`;
  } catch (error) {
    foargeMessage.textContent = error.message || String(error);
    foargeMessage.className = "message error";
  }
});

document.querySelector("#foarge-clear").addEventListener("click", async () => {
  if (!window.confirm("确认清除全部 Foarge PBK 及其使用状态？支付型 CDK 将暂时不可用。")) return;
  try {
    await request("/api/admin/foarge", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clear: true }),
    });
    foargeMessage.textContent = "支付配置已清除";
    await loadFoarge();
  } catch (error) {
    foargeMessage.textContent = error.message || String(error);
    foargeMessage.className = "message error";
  }
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  settingsMessage.textContent = "正在保存…";
  settingsMessage.className = "message";
  try {
    const data = await request("/api/admin/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        proxy_pool: document.querySelector("#settings-proxy-pool").value,
        login_proxy: document.querySelector("#settings-login-proxy").value,
        approve_retries: Number(document.querySelector("#settings-retries").value),
        approve_concurrency: Number(document.querySelector("#settings-concurrency").value),
        proxy_from_step: Number(document.querySelector("#settings-proxy-step").value),
      }),
    });
    settingsMessage.textContent = "全局配置已保存";
    settingsSummary.textContent = `代理 ${data.proxy_pool.length} · 并发 ${data.approve_concurrency}`;
    showToast("代理配置已保存");
  } catch (error) {
    settingsMessage.textContent = error.message || String(error);
    settingsMessage.className = "message error";
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
        kind: document.querySelector("#cdk-kind").value,
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
    if (session.authenticated) await Promise.all([loadCdks(), loadSettings(), loadFoarge()]);
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
