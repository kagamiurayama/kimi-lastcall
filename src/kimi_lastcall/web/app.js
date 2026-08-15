const $ = (id) => document.getElementById(id);
const copy = {
  zh: {
    subtitle: "亲笔交接，人工确认，验证后换窗。", session: "当前会话", sessionId: "会话指纹",
    model: "模型", tmux: "受管终端", handoff: "交接文件", threshold: "落笔阈值",
    save: "保存阈值", switchTitle: "开始新窗口", switchCopy: "这里只发送一次固定的 /new。它不会替你写交接信，也不会在阈值到达时自动换窗。",
    preview: "预览", execute: "确认并换窗", confirmLabel: "请输入下方确认短语",
    localOnly: "仅监听本机回环地址；页面不会上传会话或交接正文。",
    ready: "可以换窗", blocked: "尚未就绪", online: "在线", offline: "离线", saved: "阈值已保存",
    unknown: "未知", filesReady: "已就绪", filesMissing: "未完成", loading: "正在读取本地状态…",
    switchDone: "新窗口已验证并接管。", switchWaiting: "已发送 /new，等待新窗口验证；写入面保持关闭。"
  },
  en: {
    subtitle: "Handwritten handoff, human confirmation, verified session switch.", session: "Current session",
    sessionId: "Session fingerprint", model: "Model", tmux: "Managed terminal", handoff: "Handoff files",
    threshold: "Writing threshold", save: "Save threshold", switchTitle: "Start a new window",
    switchCopy: "This sends the fixed /new command once. It never writes your handoff or switches automatically at the threshold.",
    preview: "Preview", execute: "Confirm and switch", confirmLabel: "Type the exact phrase below",
    localOnly: "Loopback only. Session and handoff content never leave this machine.", ready: "Ready",
    blocked: "Not ready", online: "Online", offline: "Offline", saved: "Threshold saved", unknown: "Unknown",
    filesReady: "Ready", filesMissing: "Incomplete", loading: "Reading local status…",
    switchDone: "The new window is verified and bound.", switchWaiting: "/new sent; waiting for verified SessionStart. Writes remain closed."
  }
};
let language = localStorage.getItem("kimi-lastcall-language") || "zh";
let current = null;
let phrase = "";
const t = (key) => copy[language][key] || key;
const fmt = (value) => Number(value || 0).toLocaleString("en-US");
const messages = {
  zh: {
    session_not_bound: "尚未绑定受管 Kimi 会话",
    managed_tmux_offline: "受管 tmux 座位离线",
    handoff_files_not_ready: "交接文件尚未写好",
    handoff_not_marked_done: "当前窗口尚未落下完成标记",
    session_adoption_pending: "新窗口仍在等待机械验证",
    switch_already_in_progress: "已有一轮换窗正在进行",
    settings_invalid_for_unknown_capacity: "容量未知，已回退到安全阈值",
    settings_unreadable: "阈值设置不可读，已安全回退",
    settings_trigger_invalid: "阈值必须是有效的 50k 档位",
    switch_confirmation_invalid: "确认短语不匹配",
    switch_not_ready: "当前还不满足换窗条件",
    failed_closed_terminal_send: "受管终端拒绝了 /new；未改变会话绑定",
    authentication_required: "本地登录已失效，请重新打开启动时打印的 URL"
  },
  en: {
    session_not_bound: "No managed Kimi session is bound",
    managed_tmux_offline: "The managed tmux seat is offline",
    handoff_files_not_ready: "Required handoff files are incomplete",
    handoff_not_marked_done: "This window is not marked complete",
    session_adoption_pending: "A new window is still being verified",
    switch_already_in_progress: "Another switch is already in progress",
    settings_invalid_for_unknown_capacity: "Capacity is unknown; using a safe threshold",
    settings_unreadable: "Threshold settings are unreadable; using a safe fallback",
    settings_trigger_invalid: "Choose a valid 50k threshold step",
    switch_confirmation_invalid: "The confirmation phrase does not match",
    switch_not_ready: "The switch prerequisites are not complete",
    failed_closed_terminal_send: "The managed terminal rejected /new; binding was not changed",
    authentication_required: "Local login expired; reopen the URL printed at startup"
  }
};
const humanize = (code) => messages[language][code] || code;

function translate() {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((node) => node.textContent = t(node.dataset.i18n));
  $("language").textContent = language === "zh" ? "EN" : "中文";
  if (current) render(current);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    body: options.body === undefined ? undefined : JSON.stringify(options.body)
  });
  const body = await response.json();
  if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body.result ?? body.status;
}

function render(status) {
  current = status;
  const ready = Boolean(status.ready_to_switch);
  $("readyBadge").textContent = ready ? t("ready") : t("blocked");
  $("readyBadge").className = `badge ${ready ? "good" : "bad"}`;
  $("banner").textContent = ready ? t("ready") : status.blockers.map(humanize).join(" · ");
  $("banner").className = `banner ${ready ? "good" : "bad"}`;
  $("sessionDigest").textContent = status.current_session?.digest || t("unknown");
  $("model").textContent = status.usage.model || t("unknown");
  $("tmux").textContent = status.tmux.online ? t("online") : t("offline");
  $("handoff").textContent = status.handoff.all_files_ready ? t("filesReady") : t("filesMissing");
  const used = status.usage.used_tokens || 0;
  const limit = status.usage.context_limit || 0;
  $("usageBar").style.width = limit ? `${Math.min(100, used / limit * 100)}%` : "0%";
  $("usageText").textContent = limit ? `${fmt(used)} / ${fmt(limit)} tokens` : t("unknown");
  const slider = $("threshold");
  slider.min = status.usage.slider_min;
  slider.max = status.usage.slider_max;
  slider.step = status.usage.slider_step;
  slider.value = Math.min(status.usage.trigger_tokens, status.usage.slider_max);
  $("thresholdMax").textContent = `${Math.round(status.usage.slider_max / 1000)}k`;
  updateThresholdLabel();
  $("thresholdWarning").textContent = status.usage.trigger_warning ? humanize(status.usage.trigger_warning) : "";
  $("blockers").innerHTML = status.blockers.map((item) => `<li>${escapeHtml(humanize(item))}</li>`).join("");
  $("execute").disabled = !phrase || !ready || $("confirmation").value.trim() !== phrase;
}

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = value;
  return span.innerHTML;
}

function updateThresholdLabel() {
  const value = Number($("threshold").value);
  $("thresholdValue").textContent = `${fmt(value)} tokens`;
  const limit = current?.usage.context_limit || 0;
  $("headroom").textContent = limit
    ? (language === "zh" ? `留给落笔：${fmt(Math.max(0, limit - value))} tokens` : `Writing room: ${fmt(Math.max(0, limit - value))} tokens`)
    : (language === "zh" ? "容量未知：服务端采用 250k 安全上界。" : "Capacity unknown: the server uses a safe 250k ceiling.");
}

async function refresh() {
  try { render(await api("/api/v1/status")); }
  catch (error) { $("banner").textContent = humanize(error.message); $("banner").className = "banner bad"; }
}

$("language").addEventListener("click", () => { language = language === "zh" ? "en" : "zh"; localStorage.setItem("kimi-lastcall-language", language); translate(); });
$("threshold").addEventListener("input", updateThresholdLabel);
$("saveThreshold").addEventListener("click", async () => {
  try { render(await api("/api/v1/settings", {method: "POST", body: {trigger_tokens: Number($("threshold").value)}})); $("banner").textContent = t("saved"); }
  catch (error) { $("banner").textContent = humanize(error.message); $("banner").className = "banner bad"; }
});
$("preview").addEventListener("click", async () => {
  try {
    const result = await api("/api/v1/switch/preview", {method: "POST", body: {}});
    phrase = result.confirmation_phrase || "";
    $("phrase").textContent = phrase;
    $("confirmPanel").hidden = !phrase;
    $("confirmation").value = "";
    $("execute").disabled = true;
  } catch (error) { $("banner").textContent = humanize(error.message); $("banner").className = "banner bad"; }
});
$("confirmation").addEventListener("input", () => { $("execute").disabled = !current?.ready_to_switch || $("confirmation").value.trim() !== phrase; });
$("execute").addEventListener("click", async () => {
  $("execute").disabled = true;
  try {
    const result = await api("/api/v1/switch/confirm", {method: "POST", body: {confirmation: $("confirmation").value.trim()}});
    $("banner").textContent = result.status === "completed" ? t("switchDone") : t("switchWaiting");
    $("banner").className = `banner ${result.status === "completed" ? "good" : "bad"}`;
    await refresh();
  } catch (error) { $("banner").textContent = humanize(error.message); $("banner").className = "banner bad"; }
});

translate();
refresh();
setInterval(refresh, 5000);
