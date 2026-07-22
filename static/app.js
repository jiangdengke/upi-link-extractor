const form = document.querySelector("#job-form");
const jobsRoot = document.querySelector("#jobs");
const formMessage = document.querySelector("#form-message");
const submitButton = document.querySelector("#submit-button");
const refreshButton = document.querySelector("#refresh-button");
const health = document.querySelector("#health");
const cdkInput = document.querySelector("#cdk");
const cdkStatus = document.querySelector("#cdk-status");
const checkCdkButton = document.querySelector("#check-cdk");
const qrModal = document.querySelector("#qr-modal");
const qrModalImage = document.querySelector("#qr-modal-image");
const qrModalEmail = document.querySelector("#qr-modal-email");
const qrOpenNew = document.querySelector("#qr-open-new");
const actionToast = document.querySelector("#action-toast");
let toastTimer;
let lastCdkCheck = null;

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

function formatDateTime(value) {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function formatRemaining(epochSeconds) {
  const remaining = Math.floor(Number(epochSeconds) - Date.now() / 1000);
  if (!Number.isFinite(remaining)) return "";
  if (remaining <= 0) return "已过期";
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return minutes > 0 ? `${minutes}分${seconds}秒` : `${seconds}秒`;
}

function normalizeCdk(value) {
  return String(value || "").trim().toUpperCase().replace(/\s+/g, "");
}

function paintCdkStatus(data) {
  lastCdkCheck = data;
  cdkStatus.className = `hint cdk-status ${data.ok ? "ok" : "bad"}`;
  if (data.ok) {
    const expiry = data.expires_at ? ` · 到期 ${formatDateTime(data.expires_at * 1000)}` : " · 永不过期";
    cdkStatus.textContent = `CDK 可用 · 剩余 ${data.remaining_uses}/${data.max_uses}${expiry}`;
  } else {
    cdkStatus.textContent = data.message || "CDK 不可用";
  }
}

async function verifyCdk({ quiet = false } = {}) {
  const code = normalizeCdk(cdkInput.value);
  cdkInput.value = code;
  if (!code) {
    const data = { ok: false, message: "请填写 CDK" };
    paintCdkStatus(data);
    return data;
  }
  if (!quiet) {
    checkCdkButton.disabled = true;
    cdkStatus.className = "hint cdk-status";
    cdkStatus.textContent = "正在检测…";
  }
  try {
    const data = await request("/api/cdk/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    paintCdkStatus(data);
    if (data.ok) localStorage.setItem("upi_cdk", code);
    return data;
  } catch (error) {
    const data = { ok: false, message: error.message || String(error) };
    paintCdkStatus(data);
    return data;
  } finally {
    checkCdkButton.disabled = false;
  }
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  actionToast.textContent = message;
  actionToast.hidden = false;
  toastTimer = window.setTimeout(() => {
    actionToast.hidden = true;
  }, 2200);
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("浏览器拒绝复制，请手动选择长链复制");
}

function openExternal(url) {
  const opened = window.open(url, "_blank", "noopener,noreferrer");
  if (opened) opened.opener = null;
}

function openQrModal(url, email) {
  qrModalImage.src = url;
  qrModalEmail.textContent = email || "";
  qrOpenNew.href = url;
  qrModal.hidden = false;
  qrModal.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  document.querySelector("#qr-modal-close").focus();
}

function closeQrModal() {
  qrModal.hidden = true;
  qrModal.setAttribute("aria-hidden", "true");
  qrModalImage.removeAttribute("src");
  document.body.classList.remove("modal-open");
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
    const resultActions = element("div", "result-actions");
    if (link) {
      details.append(element("p", "", "Stripe UPI 支付链接"));
      const anchor = element("a", "payment-link", link);
      anchor.href = link;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      details.append(anchor);

      const copyButton = element("button", "secondary compact", "复制长链");
      copyButton.type = "button";
      copyButton.addEventListener("click", async () => {
        try {
          await copyText(link);
          showToast("长链已复制");
        } catch (error) {
          showError(error);
        }
      });
      const openButton = element("button", "compact", "打开长链");
      openButton.type = "button";
      openButton.addEventListener("click", () => openExternal(link));
      resultActions.append(copyButton, openButton);
    }
    if (job.result.amount) details.append(element("p", "job-meta", `金额（最小货币单位）：${job.result.amount}`));
    if (job.result.generated_at || job.finished_at) {
      details.append(element("p", "job-meta", `长链生成时间：${formatDateTime(job.result.generated_at || job.finished_at)}`));
    }
    if (job.result.qr_expires_at) {
      const remaining = formatRemaining(job.result.qr_expires_at);
      details.append(element(
        "p",
        `job-meta ${remaining === "已过期" ? "expired" : ""}`,
        `链接过期时间：${formatDateTime(job.result.qr_expires_at * 1000)} · 剩余 ${remaining}`,
      ));
    }
    if (job.result.error) details.append(element("p", "result-error", job.result.error));
    if (job.result.qr_url) {
      const qrButton = element("button", "secondary compact", "打开二维码");
      qrButton.type = "button";
      qrButton.addEventListener("click", () => openQrModal(job.result.qr_url, job.email));
      resultActions.append(qrButton);
    }
    if (resultActions.childElementCount) details.append(resultActions);
    result.append(details);
    if (job.result.qr_url) {
      const image = element("img", "qr");
      image.src = `${job.result.qr_url}?v=${encodeURIComponent(job.finished_at || Date.now())}`;
      image.alt = "UPI 二维码";
      image.tabIndex = 0;
      image.setAttribute("role", "button");
      image.setAttribute("aria-label", "放大查看 UPI 二维码");
      image.addEventListener("click", () => openQrModal(job.result.qr_url, job.email));
      image.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") openQrModal(job.result.qr_url, job.email);
      });
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
  const cdkCheck = await verifyCdk();
  if (!cdkCheck.ok) {
    submitButton.disabled = false;
    return;
  }
  const body = {
    cdk: normalizeCdk(data.get("cdk")),
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
    await verifyCdk({ quiet: true });
  } catch (error) {
    showError(error);
  } finally {
    submitButton.disabled = false;
  }
});

refreshButton.addEventListener("click", () => loadJobs().catch(showError));
checkCdkButton.addEventListener("click", () => verifyCdk());
document.querySelectorAll("[data-modal-close], #qr-modal-close, #qr-modal-done").forEach((node) => {
  node.addEventListener("click", closeQrModal);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !qrModal.hidden) closeQrModal();
});

async function initialize() {
  cdkInput.value = localStorage.getItem("upi_cdk") || "";
  try {
    const data = await request("/api/health", { cache: "no-store" });
    health.textContent = `服务正常 · v${data.version} · 并发 ${data.max_concurrency}`;
    health.classList.add("ok");
  } catch (error) {
    health.textContent = "服务异常";
    showError(error);
  }
  await loadJobs().catch(showError);
  if (cdkInput.value) await verifyCdk({ quiet: true });
  window.setInterval(() => loadJobs().catch(() => {}), 1500);
  window.setInterval(() => {
    if (cdkInput.value && lastCdkCheck) verifyCdk({ quiet: true });
  }, 10000);
}

initialize();
