const form = document.querySelector("#job-form");
const jobsRoot = document.querySelector("#jobs");
const formMessage = document.querySelector("#form-message");
const submitButton = document.querySelector("#submit-button");
const refreshButton = document.querySelector("#refresh-button");
const health = document.querySelector("#health");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}

function statusLabel(status) {
  return ({ queued: "排队中", running: "提链中", success: "成功", failed: "失败", cancelled: "已取消" })[status] || status;
}

function safePaymentLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "payments.stripe.com" ? url.href : "";
  } catch {
    return "";
  }
}

async function cancelJob(id) {
  await request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
  await loadJobs();
}

function renderJob(job) {
  const card = element("article", "job");
  const head = element("div", "job-head");
  const identity = element("div");
  identity.append(element("h3", "job-email", job.email || "未知账号"));
  identity.append(element("div", "job-meta", `${job.id.slice(0, 10)} · ${job.created_at || ""}`));

  const controls = element("div", "job-head");
  controls.append(element("span", `badge ${job.status}`, statusLabel(job.status)));
  if (["queued", "running"].includes(job.status)) {
    const cancel = element("button", "cancel", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", () => cancelJob(job.id).catch(showError));
    controls.append(cancel);
  }
  head.append(identity, controls);
  card.append(head);

  if (job.result) {
    const result = element("div", "result");
    const details = element("div");
    const link = safePaymentLink(job.result.payment_link || "");
    if (link) {
      details.append(element("p", "", "Stripe UPI 支付链接"));
      const anchor = element("a", "payment-link", link);
      anchor.href = link;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      details.append(anchor);
    }
    if (job.result.amount) details.append(element("p", "job-meta", `金额（最小货币单位）：${job.result.amount}`));
    if (job.result.qr_expires_at) {
      details.append(element("p", "job-meta", `二维码过期：${new Date(job.result.qr_expires_at * 1000).toLocaleString()}`));
    }
    if (job.result.error) details.append(element("p", "result-error", job.result.error));
    result.append(details);
    if (job.result.qr_url) {
      const image = element("img", "qr");
      image.src = `${job.result.qr_url}?v=${encodeURIComponent(job.finished_at || Date.now())}`;
      image.alt = "UPI 二维码";
      result.append(image);
    }
    card.append(result);
  }

  if (job.logs && job.logs.length) {
    card.append(element("pre", "logs", job.logs.join("\n")));
  }
  return card;
}

async function loadJobs() {
  const data = await request("/api/jobs", { cache: "no-store" });
  jobsRoot.replaceChildren();
  if (!data.jobs.length) {
    jobsRoot.append(element("div", "empty", "还没有任务。"));
    return;
  }
  data.jobs.forEach((job) => jobsRoot.append(renderJob(job)));
}

function showError(error) {
  formMessage.textContent = error.message || String(error);
  formMessage.className = "message error";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formMessage.textContent = "正在提交…";
  formMessage.className = "message";
  submitButton.disabled = true;
  const data = new FormData(form);
  const body = {
    credential: data.get("credential"),
    email: data.get("email"),
    proxy_pool: data.get("proxy_pool"),
    login_proxy: data.get("login_proxy"),
    approve_retries: Number(data.get("approve_retries")),
    approve_concurrency: Number(data.get("approve_concurrency")),
    proxy_from_step: Number(data.get("proxy_from_step")),
    authorized: data.get("authorized") === "on",
  };
  try {
    await request("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    document.querySelector("#credential").value = "";
    document.querySelector("#authorized").checked = false;
    formMessage.textContent = "任务已创建，凭证输入已清空。";
    await loadJobs();
  } catch (error) {
    showError(error);
  } finally {
    submitButton.disabled = false;
  }
});

refreshButton.addEventListener("click", () => loadJobs().catch(showError));

async function initialize() {
  try {
    const data = await request("/api/health", { cache: "no-store" });
    health.textContent = `服务正常 · v${data.version} · 并发 ${data.max_concurrency}`;
    health.classList.add("ok");
  } catch (error) {
    health.textContent = "服务异常";
    showError(error);
  }
  await loadJobs().catch(showError);
  window.setInterval(() => loadJobs().catch(() => {}), 1500);
}

initialize();

