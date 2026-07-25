const page = document.body.dataset.page;
const STORAGE_KEY = "muye-console-online-conversations-v1";
const USER_ID_STORAGE_KEY = "muye-console-online-user-id-v1";
const state = { report: null, request: null, conversations: [], activeConversationId: null };
const el = {
  statusDot: document.querySelector("#connection-dot"), summary: document.querySelector("#status-summary"), updateTime: document.querySelector("#service-update-time"), grid: document.querySelector("#service-grid"),
  history: document.querySelector("#conversation-history"), newChat: document.querySelector("#new-chat-wide"), historyToggle: document.querySelector("#history-toggle"),
  form: document.querySelector("#chat-form"), agent: document.querySelector("#agent-select"), userId: document.querySelector("#user-id"), sessionId: document.querySelector("#session-id"), input: document.querySelector("#chat-input"),
  output: document.querySelector("#chat-output"), messageShell: document.querySelector("#messages-shell"), send: document.querySelector("#send-button"), stop: document.querySelector("#stop-button"),
};

function createIcons() { window.lucide?.createIcons({ attrs: { "stroke-width": 1.8 } }); }
function headers(json = false) { return json ? { "Content-Type": "application/json" } : {}; }
function isLocalConsole() { return ["127.0.0.1", "localhost"].includes(window.location.hostname) && window.location.port === "9870"; }
function setConnectionState(status) { if (el.statusDot) { el.statusDot.classList.remove("online", "offline"); if (status) el.statusDot.classList.add(status); } }

function renderServices(report) {
  if (!el.grid) return;
  el.grid.replaceChildren();
  report.services.forEach((service) => {
    const card = document.createElement("article");
    card.className = `service-card ${service.online ? "online" : "offline"}`;
    const profiles = service.profiles.map((profile) => `<span class="profile">${profile}</span>`).join("");
    card.innerHTML = `<div class="service-head"><span class="service-name"></span><span class="badge ${service.online ? "online" : "offline"}">${service.online ? "在线" : "离线"}</span></div><div class="profiles">${profiles}</div><p class="service-meta"></p>`;
    card.querySelector(".service-name").textContent = service.name;
    card.querySelector(".service-meta").textContent = service.online ? `${service.latency_ms ?? "-"} ms${service.capability_available ? " · capabilities 已读取" : ""}` : service.message || "不可用";
    el.grid.append(card);
  });
}

async function refreshServices() {
  if (el.summary) el.summary.textContent = "正在读取服务状态";
  try {
    const response = await fetch("/console/api/services");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const report = await response.json(); state.report = report; renderServices(report);
    const online = report.services.filter((service) => service.online).length;
    const summary = `${online}/${report.services.length} 服务在线`;
    if (el.summary) el.summary.textContent = summary;
    if (el.updateTime) el.updateTime.textContent = `${summary} · ${new Date(report.generated_at).toLocaleTimeString("zh-CN")}`;
    setConnectionState(online ? "online" : "offline");
  } catch (error) {
    if (el.summary) el.summary.textContent = "服务状态不可用";
    if (el.updateTime) el.updateTime.textContent = `无法读取状态：${error.message}。请检查 dashboard-api。`;
    setConnectionState("offline");
  }
}

function sessionId() { return `console-${crypto.randomUUID()}`; }
function activeConversation() { return state.conversations.find((item) => item.id === state.activeConversationId) || null; }
function titleFor(text) { const normalized = text.replace(/\s+/g, " ").trim(); return normalized.length > 12 ? `${normalized.slice(0, 12)}...` : normalized || "新对话"; }
function saveConversations() { try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ conversations: state.conversations, activeConversationId: state.activeConversationId })); } catch (_) { /* 浏览器存储不可用时仍允许当前对话继续。 */ } }
function loadConversations() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (!Array.isArray(saved?.conversations)) return false;
    state.conversations = saved.conversations.filter((conversation) => conversation && typeof conversation.id === "string" && typeof conversation.sessionId === "string" && Array.isArray(conversation.messages));
    state.activeConversationId = state.conversations.some((item) => item.id === saved.activeConversationId) ? saved.activeConversationId : state.conversations[0]?.id || null;
    return Boolean(state.conversations.length);
  } catch (_) { return false; }
}
function updateSessionHeader() { const conversation = activeConversation(); if (el.sessionId) el.sessionId.value = conversation?.sessionId || ""; }
function createConversation() {
  const conversation = { id: crypto.randomUUID(), title: "新对话", sessionId: sessionId(), agent: el.agent?.value || "main", messages: [], createdAt: new Date().toISOString() };
  state.conversations.unshift(conversation); state.activeConversationId = conversation.id;
  saveConversations(); updateSessionHeader(); renderHistory(); renderConversation(); return conversation;
}
function deleteConversation(conversationId) {
  if (state.request) return;
  const index = state.conversations.findIndex((conversation) => conversation.id === conversationId);
  if (index < 0) return;
  const wasActive = state.activeConversationId === conversationId;
  state.conversations.splice(index, 1);
  if (wasActive) state.activeConversationId = state.conversations[0]?.id || null;
  if (!state.activeConversationId) { createConversation(); return; }
  const conversation = activeConversation(); el.agent.value = conversation.agent || "main";
  saveConversations(); updateSessionHeader(); renderHistory(); renderConversation();
}

function renderHistory() {
  if (!el.history) return;
  el.history.replaceChildren();
  if (!state.conversations.length) {
    const empty = document.createElement("p"); empty.className = "history-empty"; empty.textContent = "暂无历史对话。"; el.history.append(empty); return;
  }
  state.conversations.forEach((conversation) => {
    const card = document.createElement("div"); card.className = `history-card ${conversation.id === state.activeConversationId ? "active" : ""}`;
    const item = document.createElement("button"); item.type = "button"; item.className = "history-item"; item.title = conversation.title;
    item.innerHTML = '<i data-lucide="message-square"></i><span></span>'; item.querySelector("span").textContent = conversation.title;
    item.addEventListener("click", () => { if (state.request) return; state.activeConversationId = conversation.id; el.agent.value = conversation.agent || "main"; saveConversations(); updateSessionHeader(); renderHistory(); renderConversation(); });
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "history-delete-button"; remove.title = "删除对话"; remove.setAttribute("aria-label", `删除对话：${conversation.title}`); remove.textContent = "×";
    remove.addEventListener("click", () => deleteConversation(conversation.id)); card.append(item, remove); el.history.append(card);
  });
  createIcons();
}

function emptyNode() {
  const empty = document.createElement("div"); empty.className = "chat-empty";
  empty.innerHTML = '<span class="empty-mark"><i data-lucide="sparkles"></i></span><h2>开始一次新的对话</h2><p>输入旅行规划、订单流程或通用任务，实时查看 Agent 的处理过程。</p><div class="suggestions"><button class="suggestion" type="button" data-prompt="规划一次西安三日游">规划一次西安三日游</button><button class="suggestion" type="button" data-prompt="我需要一个西安本周末旅行建议">生成周末旅行建议</button><button class="suggestion" type="button" data-prompt="演示一个订单流程">演示订单流程</button></div>';
  empty.querySelectorAll(".suggestion").forEach((button) => button.addEventListener("click", () => { el.input.value = button.dataset.prompt || ""; resizeInput(); el.input.focus(); }));
  return empty;
}
function renderMarkdown(node, markdown) {
  if (!window.marked || !window.DOMPurify) { node.textContent = markdown; return; }
  // Agent 的每个 Markdown block 使用单个换行分隔展示行，需显式转换为 <br>。
  node.innerHTML = window.DOMPurify.sanitize(window.marked.parse(markdown, { breaks: true, gfm: true }));
}
function renderMessageBlocks(container, blocks, legacyContent = "") {
  const storedBlocks = Array.isArray(blocks) ? blocks.filter((block) => block && typeof block.content === "string" && block.content) : [];
  const visibleBlocks = storedBlocks.length ? storedBlocks : legacyContent ? [{ id: "legacy", type: "markdown", content: legacyContent }] : [];
  container.replaceChildren();
  visibleBlocks.forEach((block) => {
    const node = document.createElement("div"); node.className = "message-block"; node.dataset.blockId = block.id || "default"; node.dataset.blockType = block.type || "markdown";
    renderMarkdown(node, block.content); container.append(node);
  });
}
function scrollMessagesToBottom() { window.scrollTo(0, document.documentElement.scrollHeight); }
function messageNode(message) {
  const node = document.createElement("article"); node.className = `message ${message.role}`;
  if (message.role === "agent") {
    if (message.steps?.length) node.append(progressNode(message.steps, message.progressOpen !== false));
    const content = document.createElement("div"); content.className = "message-content"; renderMessageBlocks(content, message.blocks, message.content || ""); node.append(content);
  } else node.textContent = message.content;
  return node;
}
function progressIcon(status) { return { start: "🔧", running: "⏳", result: "📦", complete: "✅", error: "❌", thinking: "💭" }[status] || "•"; }
function progressNode(steps, isOpen = true) {
  const panel = document.createElement("section"); panel.className = "progress-panel"; panel.dataset.open = String(isOpen);
  const header = document.createElement("button"); header.type = "button"; header.className = "progress-panel-header"; header.setAttribute("aria-expanded", String(isOpen));
  const icon = document.createElement("span"); icon.textContent = "🔧"; const title = document.createElement("span"); title.className = "progress-title-text"; title.textContent = `执行过程（${steps.length} 步）`; const chevron = document.createElement("span"); chevron.className = "chevron"; chevron.textContent = "▼";
  header.append(icon, title, chevron); header.addEventListener("click", () => { const open = panel.dataset.open !== "true"; panel.dataset.open = String(open); header.setAttribute("aria-expanded", String(open)); });
  const body = document.createElement("div"); body.className = "progress-panel-body";
  steps.forEach((step) => { const row = document.createElement("div"); row.className = `progress-step ${step.status || ""}`; const stepIcon = document.createElement("span"); stepIcon.className = "progress-step-icon"; stepIcon.textContent = progressIcon(step.status); const info = document.createElement("div"); info.className = "progress-step-info"; const label = document.createElement("div"); label.className = "progress-step-label"; label.textContent = step.label; info.append(label); if (step.meta) { const meta = document.createElement("div"); meta.className = "progress-step-meta"; meta.textContent = step.meta; info.append(meta); } row.append(stepIcon, info); if (step.duration) { const duration = document.createElement("span"); duration.className = "progress-step-elapsed"; duration.textContent = `${step.duration}ms`; row.append(duration); } body.append(row); });
  panel.append(header, body); return panel;
}
function renderConversation() { const conversation = activeConversation(); el.output.replaceChildren(); if (!conversation?.messages.length) { el.output.append(emptyNode()); createIcons(); return; } conversation.messages.forEach((message) => el.output.append(messageNode(message))); scrollMessagesToBottom(); }
function addMessage(message, persist = true) { const node = messageNode(message); el.output.querySelector(".chat-empty")?.remove(); el.output.append(node); if (persist) { const conversation = activeConversation(); conversation.messages.push(message); if (message.role === "user" && conversation.title === "新对话") conversation.title = titleFor(message.content); saveConversations(); renderHistory(); } scrollMessagesToBottom(); return node; }

function parseFrame(frame) {
  const type = frame.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim();
  const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
  if (!data) return null;
  try { const payload = JSON.parse(data); return { event: payload.event || type || "message", data: payload.data ?? payload }; } catch (_) { return { event: type || "message", data: { content: data } }; }
}
function blockMarkdown(data) { if (!data || typeof data !== "object") return ""; if (typeof data.delta === "string") return data.delta; if (data.type === "table" && typeof data.content?.markdown === "string") return data.content.markdown; return typeof data.content === "string" ? data.content : typeof data.markdown === "string" ? data.markdown : ""; }
function updateMessageBlock(message, data, fallbackId = "default") {
  const markdown = blockMarkdown(data); if (!markdown) return false;
  if (!Array.isArray(message.blocks)) message.blocks = [];
  const candidateId = typeof data?.id === "string" ? data.id.trim() : ""; const blockId = candidateId || fallbackId;
  let block = message.blocks.find((item) => item.id === blockId);
  if (!block) { block = { id: blockId, type: data?.type || "markdown", content: "" }; message.blocks.push(block); }
  block.type = data?.type || block.type || "markdown";
  if (typeof data?.delta === "string") block.content += markdown; else block.content = markdown;
  message.content = message.blocks.map((item) => item.content).filter(Boolean).join("\n\n");
  return true;
}
function endpoint() { if (isLocalConsole()) return el.agent.value === "travel" ? "http://127.0.0.1:8011/api/v1/travel/invoke/stream" : "http://127.0.0.1:9860/api/v1/chat/stream"; return el.agent.value === "travel" ? "/api/v1/travel/invoke/stream" : "/agentMain/api/v1/chat/stream"; }
function requestPayload(message) { const base = { user_input: message, user_id: el.userId.value.trim(), session_id: activeConversation().sessionId }; return el.agent.value === "travel" ? { ...base, city: "目的地", days: 3 } : base; }
function resizeInput() { el.input.style.height = "auto"; el.input.style.height = `${Math.min(el.input.scrollHeight, 160)}px`; }
function setRequestState(isRunning) { el.form.classList.toggle("is-running", isRunning); el.send.disabled = isRunning; el.stop.disabled = !isRunning; }
function textPreview(value, limit = 140) { const text = String(value || "").replace(/\s+/g, " ").trim(); return text.length > limit ? `${text.slice(0, limit)}...` : text; }
function toolStep(data, startedAt) {
  const name = data.name || "工具"; const status = data.status || "running";
  if (status === "start") return { label: `调用 ${name}`, meta: textPreview(Object.keys(data.input || {}).length ? JSON.stringify(data.input) : ""), status };
  if (status === "complete") return { label: `${name} 已完成`, duration: data.duration || (startedAt[data.id || name] ? Date.now() - startedAt[data.id || name] : null), status };
  if (status === "error") return { label: `${name} 执行失败`, meta: textPreview(data.error?.message || "未知错误"), status };
  if (status === "result") return { label: `${name} 返回结果`, meta: textPreview(data.blocks?.map((block) => block.type).join("、") || "已收到结果"), status };
  return { label: `${name} 执行中`, meta: textPreview(data.log || (data.progress == null ? "" : `${data.progress}%`)), status };
}
function appendStep(answer, assistantMessage, step) { assistantMessage.steps.push(step); const existing = answer.querySelector(".progress-panel"); const isOpen = existing?.dataset.open !== "false"; if (existing) existing.replaceWith(progressNode(assistantMessage.steps, isOpen)); else answer.prepend(progressNode(assistantMessage.steps)); }
function collapseProgress(answer, assistantMessage) { assistantMessage.progressOpen = false; const panel = answer.querySelector(".progress-panel"); const header = panel?.querySelector(".progress-panel-header"); if (panel) panel.dataset.open = "false"; header?.setAttribute("aria-expanded", "false"); }

async function sendChat(event) {
  event.preventDefault(); const message = el.input.value.trim(); const conversation = activeConversation();
  if (!message || state.request || !conversation) return;
  if (!el.userId.value.trim()) { el.userId.focus(); return; }
  conversation.agent = el.agent.value; state.request = new AbortController(); setRequestState(true);
  addMessage({ role: "user", content: message, createdAt: new Date().toISOString() });
  const assistantMessage = { role: "agent", content: "", blocks: [], steps: [], createdAt: new Date().toISOString() };
  const answer = addMessage(assistantMessage, false); conversation.messages.push(assistantMessage); saveConversations();
  const content = answer.querySelector(".message-content"); const startedAt = {}; let renderFrame = 0;
  const renderAnswer = () => renderMessageBlocks(content, assistantMessage.blocks, assistantMessage.content);
  const scheduleAnswerRender = () => { if (renderFrame) return; renderFrame = requestAnimationFrame(() => { renderFrame = 0; renderAnswer(); scrollMessagesToBottom(); }); };
  el.input.value = ""; resizeInput();
  try {
    const response = await fetch(endpoint(), { method: "POST", headers: headers(true), body: JSON.stringify(requestPayload(message)), signal: state.request.signal });
    if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true }); const frames = buffer.split("\n\n"); buffer = frames.pop() || "";
      frames.forEach((frame) => {
        const streamed = parseFrame(frame); if (!streamed) return;
        if (streamed.event === "tool") { const key = streamed.data.id || streamed.data.name; if (streamed.data.status === "start") startedAt[key] = Date.now(); appendStep(answer, assistantMessage, toolStep(streamed.data, startedAt)); return; }
        if (streamed.event === "thinking" && streamed.data?.content) { appendStep(answer, assistantMessage, { label: "思考中", meta: textPreview(streamed.data.content), status: "thinking" }); return; }
        if (["block", "message"].includes(streamed.event)) { if (updateMessageBlock(assistantMessage, streamed.data)) scheduleAnswerRender(); return; }
        if (["done", "session_end"].includes(streamed.event)) { collapseProgress(answer, assistantMessage); return; }
        if (streamed.event === "error") { answer.classList.add("error"); updateMessageBlock(assistantMessage, { id: "console-error", type: "markdown", content: `**请求失败：** ${streamed.data?.message || "Agent 返回错误。"}` }); renderAnswer(); }
      });
      saveConversations(); scrollMessagesToBottom();
    }
    if (!assistantMessage.blocks.length) updateMessageBlock(assistantMessage, { id: "console-status", type: "markdown", content: "服务已结束响应，但未返回可展示文本。" });
    if (renderFrame) { cancelAnimationFrame(renderFrame); renderFrame = 0; } renderAnswer();
  } catch (error) {
    if (error.name === "AbortError") { if (!assistantMessage.blocks.length) updateMessageBlock(assistantMessage, { id: "console-status", type: "markdown", content: "请求已终止。" }); }
    else { answer.classList.add("error"); updateMessageBlock(assistantMessage, { id: "console-error", type: "markdown", content: `**请求失败：** ${error.message}` }); }
    if (renderFrame) { cancelAnimationFrame(renderFrame); renderFrame = 0; } renderAnswer();
  } finally {
    collapseProgress(answer, assistantMessage);
    saveConversations(); renderHistory(); state.request = null; setRequestState(false); scrollMessagesToBottom();
  }
}

function initOverview() { document.querySelectorAll("button.tab[data-tab]").forEach((tab) => tab.addEventListener("click", () => { document.querySelectorAll("button.tab[data-tab]").forEach((item) => item.classList.toggle("active", item === tab)); document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === tab.dataset.tab)); })); refreshServices(); setInterval(refreshServices, 15000); }
function initOnline() {
  if (!loadConversations()) createConversation(); else { updateSessionHeader(); renderHistory(); renderConversation(); }
  try { el.userId.value = localStorage.getItem(USER_ID_STORAGE_KEY) || el.userId.value; } catch (_) { /* 忽略私密浏览模式的存储限制。 */ }
  el.userId.addEventListener("change", () => { try { localStorage.setItem(USER_ID_STORAGE_KEY, el.userId.value.trim()); } catch (_) { /* 忽略存储限制。 */ } });
  el.historyToggle?.addEventListener("click", () => { const isOpen = document.body.classList.toggle("sidebar-open"); el.historyToggle.setAttribute("aria-expanded", String(isOpen)); el.historyToggle.title = isOpen ? "隐藏历史对话" : "显示历史对话"; el.historyToggle.setAttribute("aria-label", el.historyToggle.title); });
  el.newChat.addEventListener("click", () => { if (state.request) state.request.abort(); createConversation(); el.input.focus(); });
  el.agent.addEventListener("change", () => { const conversation = activeConversation(); if (conversation) { conversation.agent = el.agent.value; saveConversations(); } });
  el.form.addEventListener("submit", sendChat); el.stop.addEventListener("click", () => state.request?.abort()); el.input.addEventListener("input", resizeInput);
  el.input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); el.form.requestSubmit(); } });
  window.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); el.newChat.click(); } });
  refreshServices(); setInterval(refreshServices, 15000); resizeInput();
}

createIcons(); if (page === "online") initOnline(); else initOverview();
