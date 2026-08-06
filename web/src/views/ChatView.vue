<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { gatewayAuthorizationHeader } from "../api";
import MarkdownContent from "../components/MarkdownContent.vue";

type StreamEvent = {
  event: string;
  sessionId?: string;
  streamId?: string;
  userId?: string;
  seq?: number;
  timestamp?: number;
  data: Record<string, unknown>;
};

type ThinkingStep = {
  id: string;
  name: string;
  status: string;
  detail?: string;
  duration?: number;
};

type DebugSession = {
  id: string;
  question: string;
  events: StreamEvent[];
  ended: boolean;
  blockCount: number;
};

type Conversation = {
  id: string;
  title: string;
  sessionId: string;
  messages: Message[];
  debugEvents: StreamEvent[];
};

type Message = {
  role: "user" | "assistant";
  content: string;
  thinking: ThinkingStep[];
  thinkingOpen: boolean;
  citations: { title: string; source: string }[];
  error?: string;
};

const localDev = import.meta.env.DEV && import.meta.env.VITE_MUYE_LOCAL_DEV === "true";
const canDebug = localDev;
const conversations = ref<Conversation[]>([]);
const activeId = ref("");
const input = ref("");
const running = ref(false);
const connected = ref(false);
const debugOpen = ref(false);
const debugSessionOpen = ref<Record<string, boolean>>({});
const controller = ref<AbortController | null>(null);
const threadElement = ref<HTMLElement | null>(null);
const composerInput = ref<HTMLTextAreaElement | null>(null);
let persistTimer: ReturnType<typeof setTimeout> | null = null;

const active = computed(() =>
  conversations.value.find((item) => item.id === activeId.value)
);
const debugEvents = computed(() => active.value?.debugEvents || []);
const debugSessions = computed<DebugSession[]>(() => {
  const sessions: DebugSession[] = [];
  const questions =
    active.value?.messages
      .filter((message) => message.role === "user")
      .map((message) => message.content) || [];
  let current: DebugSession | undefined;

  for (const event of debugEvents.value) {
    if (event.event === "session_start") {
      current = {
        id: event.streamId || `stream-${event.seq ?? sessions.length}`,
        question: questionTitle(
          questions[sessions.length] || `问答 ${sessions.length + 1}`
        ),
        events: [],
        ended: false,
        blockCount: 0,
      };
      sessions.push(current);
    }
    if (!current) {
      current = {
        id: event.streamId || `orphan-${event.seq ?? sessions.length}`,
        question: questionTitle(
          questions[sessions.length] || `问答 ${sessions.length + 1}`
        ),
        events: [],
        ended: false,
        blockCount: 0,
      };
      sessions.push(current);
    }
    current.events.push(event);
    if (event.event === "block") current.blockCount += 1;
    if (event.event === "session_end") {
      current.ended = true;
      current = undefined;
    }
  }
  return sessions;
});

function newConversation(): void {
  const id = crypto.randomUUID();
  conversations.value.unshift({
    id,
    title: "新对话",
    sessionId: `console-${crypto.randomUUID()}`,
    messages: [],
    debugEvents: [],
  });
  activeId.value = id;
  persist();
}

function persist(): void {
  localStorage.setItem("muye-chat-conversations-v2", JSON.stringify(conversations.value));
}

function schedulePersist(): void {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    persistTimer = null;
    persist();
  }, 200);
}

function flushPersist(): void {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = null;
  persist();
}

async function renderStreamFrame(): Promise<void> {
  await nextTick();
  if (threadElement.value)
    threadElement.value.scrollTop = threadElement.value.scrollHeight;
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
}

function resizeComposer(): void {
  const element = composerInput.value;
  if (!element) return;
  const styles = window.getComputedStyle(element);
  const lineHeight = Number.parseFloat(styles.lineHeight);
  const verticalPadding =
    Number.parseFloat(styles.paddingTop) + Number.parseFloat(styles.paddingBottom);
  const verticalBorder =
    Number.parseFloat(styles.borderTopWidth) +
    Number.parseFloat(styles.borderBottomWidth);
  const maxHeight = lineHeight * 4 + verticalPadding + verticalBorder;
  element.style.height = "auto";
  element.style.height = `${Math.min(element.scrollHeight, maxHeight)}px`;
  element.style.overflowY = element.scrollHeight > maxHeight ? "auto" : "hidden";
}

function loadConversations(): void {
  try {
    const saved = JSON.parse(localStorage.getItem("muye-chat-conversations-v2") || "[]");
    if (Array.isArray(saved) && saved.length) {
      conversations.value = saved
        .filter((item): item is Record<string, unknown> =>
          Boolean(
            item &&
              typeof item === "object" &&
              typeof item.id === "string" &&
              Array.isArray(item.messages)
          )
        )
        .map((item) => ({
          id: item.id as string,
          title: typeof item.title === "string" ? item.title : "新对话",
          sessionId:
            typeof item.sessionId === "string"
              ? item.sessionId
              : `console-${crypto.randomUUID()}`,
          messages: (item.messages as unknown[])
            .filter((message): message is Record<string, unknown> =>
              Boolean(message && typeof message === "object")
            )
            .map((message) => ({
              role: message.role === "user" ? "user" : "assistant",
              content: typeof message.content === "string" ? message.content : "",
              thinking: Array.isArray(message.thinking)
                ? message.thinking.filter((step): step is ThinkingStep =>
                    Boolean(
                      step &&
                        typeof step === "object" &&
                        typeof (step as ThinkingStep).id === "string" &&
                        typeof (step as ThinkingStep).name === "string"
                    )
                  )
                : Array.isArray(message.steps)
                ? message.steps
                    .filter((step): step is string => typeof step === "string")
                    .map((step, index) => ({
                      id: `legacy-${index}`,
                      name: step,
                      status: "complete",
                    }))
                : [],
              thinkingOpen:
                typeof message.thinkingOpen === "boolean" ? message.thinkingOpen : false,
              citations: Array.isArray(message.citations)
                ? message.citations.filter((citation): citation is {
                    title: string;
                    source: string;
                  } =>
                    Boolean(
                      citation &&
                        typeof citation === "object" &&
                        typeof (citation as { title?: unknown }).title === "string" &&
                        typeof (citation as { source?: unknown }).source === "string"
                    )
                  )
                : [],
              error: typeof message.error === "string" ? message.error : undefined,
            })),
          debugEvents: Array.isArray(item.debugEvents)
            ? item.debugEvents.filter(isStreamEvent)
            : [],
        }));
      activeId.value = conversations.value[0]?.id || "";
    }
  } catch {
    localStorage.removeItem("muye-chat-conversations-v2");
  }
  if (!activeId.value) newConversation();
}

function isStreamEvent(value: unknown): value is StreamEvent {
  return Boolean(
    value &&
      typeof value === "object" &&
      typeof (value as StreamEvent).event === "string" &&
      typeof (value as StreamEvent).data === "object"
  );
}

function titleFor(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > 18
    ? `${normalized.slice(0, 18)}...`
    : normalized || "新对话";
}

function questionTitle(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > 24
    ? `${normalized.slice(0, 24)}...`
    : normalized || "未记录问题";
}

function formatToolInput(value: unknown): string | undefined {
  if (!value || typeof value !== "object") return undefined;
  const input = value as Record<string, unknown>;
  if (typeof input.task === "string") return `任务：${input.task}`;
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return undefined;
  }
}

function toolResultDetail(blocks: unknown): string | undefined {
  if (!Array.isArray(blocks)) return undefined;
  const details: string[] = [];
  for (const block of blocks) {
    if (!block || typeof block !== "object") continue;
    const value = block as Record<string, unknown>;
    if (
      value.type === "citation" &&
      typeof value.title === "string" &&
      typeof value.source === "string"
    ) {
      details.push(`${value.title} · ${value.source}`);
    } else if (value.type === "json") {
      details.push("已返回结构化检索结果");
    } else if (typeof value.content === "string") {
      details.push(value.content);
    } else if (typeof value.type === "string") {
      details.push(`已返回 ${value.type} 结果`);
    }
  }
  return details.join("\n") || undefined;
}

function appendToolThinking(message: Message, event: StreamEvent): void {
  const toolName = typeof event.data.name === "string" ? event.data.name : "工具调用";
  const status = typeof event.data.status === "string" ? event.data.status : "running";
  const labels: Record<string, string> = {
    start: `启动 ${toolName}`,
    running: toolName,
    result: `${toolName} 返回结果`,
    complete: `${toolName} 执行完成`,
    error: `${toolName} 执行失败`,
  };
  const error = event.data.error;
  const errorMessage =
    error &&
    typeof error === "object" &&
    typeof (error as Record<string, unknown>).message === "string"
      ? (error as Record<string, string>).message
      : undefined;
  const detail =
    status === "start"
      ? formatToolInput(event.data.input)
      : status === "result"
      ? toolResultDetail(event.data.blocks)
      : typeof event.data.log === "string"
      ? event.data.log
      : errorMessage;
  message.thinking.push({
    id: `tool-${event.data.id || "unknown"}-${event.seq ?? message.thinking.length}`,
    name: labels[status] || toolName,
    status,
    detail,
    duration: typeof event.data.duration === "number" ? event.data.duration : undefined,
  });
}

function appendThinkingEvent(message: Message, event: StreamEvent): void {
  message.thinking.push({
    id: `thinking-${event.data.id || event.seq || message.thinking.length}`,
    name: "正在思考",
    status: "running",
    detail: typeof event.data.content === "string" ? event.data.content : undefined,
  });
}

function appendThinkingBlock(message: Message, blockId: string, delta: string): void {
  const id = `block-${blockId}`;
  const existing = message.thinking.find((step) => step.id === id);
  if (existing) existing.detail = `${existing.detail || ""}${delta}`;
  else message.thinking.push({ id, name: "分析请求", status: "running", detail: delta });
}

function deleteConversation(id: string): void {
  if (running.value) return;
  const index = conversations.value.findIndex((conversation) => conversation.id === id);
  if (index < 0) return;
  conversations.value.splice(index, 1);
  if (activeId.value === id) activeId.value = conversations.value[0]?.id || "";
  if (!activeId.value) newConversation();
  persist();
}

function updateThinkingOpen(message: Message, event: Event): void {
  message.thinkingOpen = (event.currentTarget as HTMLDetailsElement).open;
  persist();
}

function isDebugSessionOpen(session: DebugSession): boolean {
  return debugSessionOpen.value[session.id] ?? !session.ended;
}

function updateDebugSessionOpen(session: DebugSession, event: Event): void {
  debugSessionOpen.value[session.id] = (event.currentTarget as HTMLDetailsElement).open;
}

function parseFrames(buffer: string): { frames: StreamEvent[]; rest: string } {
  const chunks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = chunks.pop() || "";
  const frames: StreamEvent[] = [];
  for (const chunk of chunks) {
    const event =
      chunk
        .split("\n")
        .find((line) => line.startsWith("event:"))
        ?.slice(6)
        .trim() || "message";
    const data = chunk
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim())
      .join("\n");
    if (!data) continue;
    try {
      const parsed = JSON.parse(data) as StreamEvent;
      frames.push({
        event: parsed.event || event,
        sessionId: parsed.sessionId,
        streamId: parsed.streamId,
        userId: parsed.userId,
        seq: parsed.seq,
        timestamp: parsed.timestamp,
        data: parsed.data || {},
      });
    } catch {
      frames.push({
        event: "error",
        data: { code: "INVALID_SSE", message: "服务返回了无效的 SSE 数据。" },
      });
    }
  }
  return { frames, rest };
}

async function send(): Promise<void> {
  const question = input.value.trim();
  const conversation = active.value;
  if (!question || !conversation || running.value) return;
  running.value = true;
  input.value = "";
  await nextTick();
  resizeComposer();
  const user: Message = {
    role: "user",
    content: question,
    thinking: [],
    thinkingOpen: false,
    citations: [],
  };
  const answerDraft: Message = {
    role: "assistant",
    content: "",
    thinking: [],
    thinkingOpen: true,
    citations: [],
  };
  conversation.messages.push(user, answerDraft);
  const answer = conversation.messages[conversation.messages.length - 1];
  if (conversation.title === "新对话") conversation.title = titleFor(question);
  persist();
  void renderStreamFrame();
  const abortController = new AbortController();
  controller.value = abortController;
  try {
    const response = await fetch("/agentMain/api/v1/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...gatewayAuthorizationHeader() },
      body: JSON.stringify({ user_input: question, session_id: conversation.sessionId }),
      signal: abortController.signal,
    });
    if (!response.ok || !response.body)
      throw new Error(`请求失败（HTTP ${response.status}）`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    let ended = false;
    let toolSeen = false;
    let toolCompleted = false;
    while (!ended) {
      const result = await reader.read();
      ended = result.done;
      pending += decoder.decode(result.value || new Uint8Array(), { stream: !ended });
      const parsed = parseFrames(pending);
      pending = parsed.rest;
      for (const event of parsed.frames) {
        conversation.debugEvents.push(event);
        if (conversation.debugEvents.length > 500)
          conversation.debugEvents.splice(0, conversation.debugEvents.length - 500);
        if (event.event === "block") {
          const delta =
            typeof event.data.delta === "string"
              ? event.data.delta
              : typeof event.data.content === "string"
              ? event.data.content
              : "";
          const blockId =
            typeof event.data.id === "string"
              ? event.data.id
              : `block-${event.seq ?? "unknown"}`;
          if (toolSeen && toolCompleted) {
            answer.content += delta;
            if (delta) answer.thinkingOpen = false;
          } else if (delta) {
            appendThinkingBlock(answer, blockId, delta);
          }
        } else if (event.event === "tool") {
          toolSeen = true;
          const status =
            typeof event.data.status === "string" ? event.data.status : "running";
          if (status === "start") toolCompleted = false;
          appendToolThinking(answer, event);
          if (status === "complete" || status === "error") {
            toolCompleted = true;
            for (const step of answer.thinking) {
              if (step.status === "running") step.status = "complete";
            }
          }
        } else if (event.event === "thinking") {
          appendThinkingEvent(answer, event);
        } else if (event.event === "error") {
          answer.error =
            typeof event.data.message === "string"
              ? event.data.message
              : "Agent 返回错误。";
        } else if (event.event === "done" && !toolSeen && !answer.content) {
          answer.content = answer.thinking
            .filter((step) => step.id.startsWith("block-"))
            .map((step) => step.detail || "")
            .join("");
          answer.thinking = answer.thinking.filter(
            (step) => !step.id.startsWith("block-")
          );
          answer.thinkingOpen = false;
        }
        schedulePersist();
        await renderStreamFrame();
      }
    }
    if (!answer.content && !answer.error) answer.error = "服务结束，但未返回可展示内容。";
  } catch (reason) {
    answer.error =
      reason instanceof DOMException && reason.name === "AbortError"
        ? "请求已终止。"
        : reason instanceof Error
        ? reason.message
        : "请求失败。";
  } finally {
    running.value = false;
    controller.value = null;
    flushPersist();
  }
}

function stop(): void {
  controller.value?.abort();
}

function formatEventTime(timestamp?: number): string {
  if (!timestamp) return "--:--:--";
  const date = new Date(timestamp < 10_000_000_000 ? timestamp * 1000 : timestamp);
  return Number.isNaN(date.getTime())
    ? "--:--:--"
    : date.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDebugPayload(event: StreamEvent): string {
  return JSON.stringify(
    {
      sessionId: event.sessionId,
      streamId: event.streamId,
      data: event.data,
    },
    null,
    2
  );
}

async function checkHealth(): Promise<void> {
  try {
    const response = await fetch("/agentMain/health");
    connected.value = response.ok;
  } catch {
    connected.value = false;
  }
}

onMounted(() => {
  loadConversations();
  void checkHealth();
  void nextTick().then(resizeComposer);
});
onBeforeUnmount(() => {
  controller.value?.abort();
  flushPersist();
});
</script>

<template>
  <section class="chat-page">
    <aside class="chat-sidebar" aria-label="会话历史">
      <div class="chat-brand">
        <div class="brand-identity">
          <span class="brand-mark">M</span>
          <div><strong>Muye</strong><span>在线体验</span></div>
        </div>
        <p
          class="connection"
          :title="connected ? 'Main Agent 连接正常' : 'Main Agent 连接不可用'"
        >
          <span>连接状态</span><i :class="['connection-dot', { connected }]" />
        </p>
      </div>
      <button type="button" class="new-chat" :disabled="running" @click="newConversation">
        <img
          src="https://unpkg.com/lucide-static@0.468.0/icons/plus.svg"
          alt=""
          aria-hidden="true"
        />新建对话
      </button>
      <p class="conversation-heading">历史对话</p>
      <div class="conversation-list">
        <div
          v-for="conversation in conversations"
          :key="conversation.id"
          :class="['conversation-item', { active: conversation.id === activeId }]"
        >
          <button
            type="button"
            class="conversation-select"
            :disabled="running"
            @click="activeId = conversation.id"
          >
            <img
              src="https://unpkg.com/lucide-static@0.468.0/icons/message-square.svg"
              alt=""
              aria-hidden="true"
            /><span>{{ conversation.title }}</span>
          </button>
          <button
            type="button"
            class="delete-conversation"
            :disabled="running"
            :aria-label="`删除对话：${conversation.title}`"
            title="删除对话"
            @click="deleteConversation(conversation.id)"
          >
            <img
              src="https://unpkg.com/lucide-static@0.468.0/icons/x.svg"
              alt=""
              aria-hidden="true"
            />
          </button>
        </div>
      </div>
      <p class="dev-agent">
        {{ localDev ? "local-dev · agent-main 编排" : "agent-main · 主编排" }}
      </p>
    </aside>
    <main class="chat-workspace">
      <header class="chat-header">
        <div>
          <strong>LIVE CONVERSATION</strong
          ><span>Session ID: {{ active?.sessionId }}</span>
        </div>
        <button
          v-if="canDebug"
          type="button"
          class="debug-toggle"
          :aria-expanded="debugOpen"
          title="显示 SSE 调试事件"
          @click="debugOpen = !debugOpen"
        >
          <img
            src="https://unpkg.com/lucide-static@0.468.0/icons/bug.svg"
            alt=""
            aria-hidden="true"
          />调试
        </button>
      </header>
      <div :class="['chat-layout', { 'with-debug': debugOpen }]">
        <div ref="threadElement" class="chat-thread" aria-live="polite">
          <p v-if="!active?.messages.length" class="chat-empty">
            开始一次新的对话，验证 Main 如何选择当前本地 Agent。
          </p>
          <article
            v-for="(message, index) in active?.messages"
            :key="index"
            :class="['chat-message', message.role, { error: message.error }]"
          >
            <p class="message-role" v-if="message.role !== 'user'">Muye</p>
            <details
              v-if="message.role === 'assistant' && message.thinking.length"
              class="thinking"
              :open="message.thinkingOpen"
              @toggle="updateThinkingOpen(message, $event)"
            >
              <summary>
                <img
                  src="https://unpkg.com/lucide-static@0.468.0/icons/brain-circuit.svg"
                  alt=""
                  aria-hidden="true"
                />思考过程（{{ message.thinking.length }}）
              </summary>
              <ol>
                <li v-for="step in message.thinking" :key="step.id">
                  <span :class="['thinking-status', step.status]" />
                  <div>
                    <strong>{{ step.name }}</strong>
                    <p v-if="step.detail">{{ step.detail }}</p>
                  </div>
                  <small v-if="step.duration !== undefined">{{ step.duration }}ms</small>
                </li>
              </ol>
            </details>
            <MarkdownContent class="message-content" :content="message.content" />
            <p v-if="message.error" role="alert">{{ message.error }}</p>
          </article>
        </div>
        <aside v-if="debugOpen" class="debug-panel" aria-label="调试事件">
          <header class="debug-header">
            <div>
              <img
                src="https://unpkg.com/lucide-static@0.468.0/icons/radio.svg"
                alt=""
                aria-hidden="true"
              />
              <h2>SSE 调试</h2>
            </div>
            <span>{{ debugSessions.length }} 问答</span>
          </header>
          <p v-if="!debugEvents.length" class="debug-empty">等待流式事件</p>
          <ol v-else class="debug-sessions">
            <li v-for="session in debugSessions" :key="session.id">
              <details
                class="debug-session"
                :open="isDebugSessionOpen(session)"
                @toggle="updateDebugSessionOpen(session, $event)"
              >
                <summary>
                  <span :title="session.question">{{ session.question }}</span
                  ><small
                    >{{ session.blockCount }} blocks ·
                    {{ session.events.length }} events</small
                  >
                </summary>
                <ol class="debug-events">
                  <li
                    v-for="event in session.events"
                    :key="`${session.id}-${event.seq ?? event.event}`"
                  >
                    <div class="debug-event-meta">
                      <code>{{ event.event }}</code
                      ><span
                        >#{{ event.seq ?? "-" }} ·
                        {{ formatEventTime(event.timestamp) }}</span
                      >
                    </div>
                    <pre>{{ formatDebugPayload(event) }}</pre>
                  </li>
                </ol>
              </details>
            </li>
          </ol>
        </aside>
      </div>
      <form class="composer" @submit.prevent="send">
        <textarea
          ref="composerInput"
          v-model="input"
          rows="1"
          maxlength="4000"
          :disabled="running"
          placeholder="输入问题，Enter 发送，Shift + Enter 换行"
          @input="resizeComposer"
          @keydown.enter.exact.prevent="send"
        />
        <button v-if="running" type="button" class="stop-button" @click="stop">
          <span>终止</span
          ><img
            src="https://api.iconify.design/lucide:square.svg?color=%23ffffff"
            alt=""
            aria-hidden="true"
          /></button
        ><button v-else type="submit" :disabled="!input.trim()">
          <span>发送</span
          ><img
            src="https://api.iconify.design/lucide:arrow-up.svg?color=%23ffffff"
            alt=""
            aria-hidden="true"
          />
        </button>
      </form>
    </main>
  </section>
</template>
