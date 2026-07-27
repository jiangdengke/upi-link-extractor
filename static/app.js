const form = document.querySelector("#job-form");
const jobsRoot = document.querySelector("#jobs");
const formMessage = document.querySelector("#form-message");
const submitButton = document.querySelector("#submit-button");
const refreshButton = document.querySelector("#refresh-button");
const health = document.querySelector("#health");
const cdkInput = document.querySelector("#cdk");
const cdkStatus = document.querySelector("#cdk-status");
const checkCdkButton = document.querySelector("#check-cdk");
const linkTypeInput = document.querySelector("#link-type");
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
    const hostname = url.hostname.toLowerCase();
    const trustedHost = hostname === "payments.stripe.com"
      || hostname === "nicepay.co.kr"
      || hostname.endsWith(".nicepay.co.kr")
      || hostname === "nicepay.com"
      || hostname.endsWith(".nicepay.com")
      || hostname === "kakao.com"
      || hostname.endsWith(".kakao.com")
      || hostname === "kakaopay.com"
      || hostname.endsWith(".kakaopay.com");
    return url.protocol === "https:" && trustedHost ? url.href : "";
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
    const capability = data.kind === "foarge" ? "提链 + 支付" : "仅提链";
    cdkStatus.textContent = `CDK 可用 · ${capability} · 剩余 ${data.remaining_uses}/${data.max_uses}${expiry}`;
    submitButton.textContent = data.kind === "foarge" ? "开始提链并支付" : "开始提链";
    if (data.kind === "foarge") linkTypeInput.value = "upi";
    linkTypeInput.disabled = data.kind === "foarge";
  } else {
    cdkStatus.textContent = data.message || "CDK 不可用";
    submitButton.textContent = "开始提链";
    linkTypeInput.disabled = false;
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

const PAYMENT_STAGES = [
  ["queued", "上游排队"],
  ["awaiting_checkout", "生成长链"],
  ["pending", "等待支付"],
  ["assigned", "支付处理中"],
  ["completed", "支付完成"],
];

function paymentStageIndex(status) {
  const aliases = {
    promoted: "awaiting_checkout",
    awaiting_refresh: "awaiting_checkout",
    refresh_required: "awaiting_checkout",
  };
  return PAYMENT_STAGES.findIndex(([value]) => value === (aliases[status] || status));
}

function paymentStatusLabel(status) {
  return ({
    queued: "上游排队中",
    awaiting_checkout: "正在生成长链",
    promoted: "正在生成长链",
    pending: "等待支付",
    assigned: "支付处理中",
    completed: "支付完成",
    cancelled: "支付已取消",
    canceled: "支付已取消",
    expired: "支付任务已过期",
    failed: "支付失败",
    rejected: "支付被拒绝",
    awaiting_refresh: "正在刷新长链",
    refresh_required: "正在刷新长链",
  })[status] || "状态同步中";
}

function renderPaymentProgress(payment) {
  const section = element("section", "payment-progress");
  const heading = element("div", "payment-progress-head");
  heading.append(element("strong", "", "支付进度"));
  heading.append(element("span", `payment-state ${payment.status || ""}`, paymentStatusLabel(payment.status)));
  section.append(heading);

  const track = element("div", "payment-track");
  const steps = element("ol", "payment-steps");
  const current = paymentStageIndex(payment.status);
  const failed = ["cancelled", "canceled", "expired", "failed", "rejected"].includes(payment.status);
  PAYMENT_STAGES.forEach(([, label], index) => {
    let className = "payment-step";
    if (current >= 0 && index < current) className += " complete";
    if (current === index) className += failed ? " failed" : " current";
    const item = element("li", className);
    item.append(element("span", "payment-dot"), element("span", "", label));
    steps.append(item);
  });
  track.append(steps);
  section.append(track);

  const meta = [];
  if (payment.queue_position !== null && payment.queue_position !== undefined) {
    meta.push(`队列位置 ${payment.queue_position}`);
  }
  if (payment.refresh_count) meta.push(`已刷新 ${payment.refresh_count} 次`);
  if (payment.synced_at) meta.push(`同步于 ${formatDateTime(payment.synced_at)}`);
  if (meta.length) section.append(element("p", "job-meta", meta.join(" · ")));
  if (payment.message) section.append(element("p", "payment-message", payment.message));
  return section;
}

function renderJob(job) {
  const card = element("article", "job");
  const head = element("div", "job-head");
  const identity = element("div");
  const linkType = (job.result && job.result.link_type) || job.link_type || "upi";
  identity.append(element("h3", "job-email", job.email || (linkType === "kakao" ? "Kakao 账号" : "未知账号")));
  identity.append(element(
    "div",
    "job-meta",
    `${linkType === "kakao" ? "韩国 Kakao" : "印度 UPI"} · ${job.id.slice(0, 10)} · ${job.created_at || ""}`,
  ));

  const controls = element("div", "job-head");
  const currentStatus = job.payment && job.status === "running"
    ? paymentStatusLabel(job.payment.status)
    : statusLabel(job.status);
  controls.append(element("span", `badge ${job.status}`, currentStatus));
  if (["queued", "running"].includes(job.status)) {
    const cancel = element("button", "cancel", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", () => cancelJob(job.id).catch(showError));
    controls.append(cancel);
  }
  head.append(identity, controls);
  card.append(head);

  if (job.payment) card.append(renderPaymentProgress(job.payment));

  if (job.result) {
    const result = element("div", "result");
    const details = element("div");
    const link = safePaymentLink(job.result.payment_link || "");
    const resultActions = element("div", "result-actions");
    if (link) {
      details.append(element(
        "p",
        "",
        linkType === "kakao" ? "Kakao Pay / Nicepay 跳转链接" : "Stripe UPI 支付链接",
      ));
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
      const qrVersion = job.result.generated_at || job.finished_at || job.created_at;
      const qrUrl = `${job.result.qr_url}?v=${encodeURIComponent(qrVersion)}`;
      const qrButton = element("button", "secondary compact", "打开二维码");
      qrButton.type = "button";
      qrButton.addEventListener("click", () => openQrModal(qrUrl, job.email));
      resultActions.append(qrButton);
    }
    if (resultActions.childElementCount) details.append(resultActions);
    result.append(details);
    if (job.result.qr_url) {
      const image = element("img", "qr");
      const qrVersion = job.result.generated_at || job.finished_at || job.created_at;
      image.src = `${job.result.qr_url}?v=${encodeURIComponent(qrVersion)}`;
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
    link_type: data.get("link_type") || linkTypeInput.value,
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
    health.textContent = `服务正常 · v${data.version}`;
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
