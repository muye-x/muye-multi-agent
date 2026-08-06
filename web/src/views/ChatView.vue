<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { gatewayAuthorizationHeader } from '../api'

type StreamEvent = {
  event: string
  seq?: number
  timestamp?: number
  data: Record<string, unknown>
}

type Conversation = {
  id: string
  title: string
  sessionId: string
  messages: Message[]
}

type Message = {
  role: 'user' | 'assistant'
  content: string
  steps: string[]
  citations: { title: string; source: string }[]
  error?: string
}

const localDev = import.meta.env.DEV && import.meta.env.VITE_MUYE_LOCAL_DEV === 'true'
const canDebug = localDev
const conversations = ref<Conversation[]>([])
const activeId = ref('')
const input = ref('')
const running = ref(false)
const connected = ref(false)
const debugOpen = ref(false)
const debugEvents = ref<StreamEvent[]>([])
const controller = ref<AbortController | null>(null)

const active = computed(() => conversations.value.find((item) => item.id === activeId.value))

function newConversation(): void {
  const id = crypto.randomUUID()
  conversations.value.unshift({
    id,
    title: '新对话',
    sessionId: `console-${crypto.randomUUID()}`,
    messages: [],
  })
  activeId.value = id
  debugEvents.value = []
  persist()
}

function persist(): void {
  localStorage.setItem('muye-chat-conversations-v2', JSON.stringify(conversations.value))
}

function loadConversations(): void {
  try {
    const saved = JSON.parse(localStorage.getItem('muye-chat-conversations-v2') || '[]')
    if (Array.isArray(saved) && saved.length) {
      conversations.value = saved.filter((item): item is Conversation => Boolean(item && typeof item.id === 'string' && Array.isArray(item.messages)))
      activeId.value = conversations.value[0]?.id || ''
    }
  } catch {
    localStorage.removeItem('muye-chat-conversations-v2')
  }
  if (!activeId.value) newConversation()
}

function titleFor(value: string): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length > 18 ? `${normalized.slice(0, 18)}...` : normalized || '新对话'
}

function appendStep(message: Message, event: StreamEvent): void {
  const name = typeof event.data.name === 'string' ? event.data.name : '工具'
  const status = typeof event.data.status === 'string' ? event.data.status : 'running'
  const error = event.data.error
  const errorMessage = error && typeof error === 'object' && typeof (error as Record<string, unknown>).message === 'string'
    ? (error as Record<string, string>).message
    : ''
  const detailValue = typeof event.data.log === 'string' ? event.data.log : errorMessage
  const detail = detailValue ? `：${detailValue}` : ''
  message.steps.push(`${name} · ${status}${detail}`)
  const blocks = event.data.blocks
  if (Array.isArray(blocks)) {
    for (const block of blocks) {
      if (block && typeof block === 'object' && (block as Record<string, unknown>).type === 'citation') {
        const value = block as Record<string, unknown>
        if (typeof value.title === 'string' && typeof value.source === 'string') {
          message.citations.push({ title: value.title, source: value.source })
        }
      }
    }
  }
}

function parseFrames(buffer: string): { frames: StreamEvent[]; rest: string } {
  const chunks = buffer.replace(/\r\n/g, '\n').split('\n\n')
  const rest = chunks.pop() || ''
  const frames: StreamEvent[] = []
  for (const chunk of chunks) {
    const event = chunk.split('\n').find((line) => line.startsWith('event:'))?.slice(6).trim() || 'message'
    const data = chunk.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
    if (!data) continue
    try {
      const parsed = JSON.parse(data) as StreamEvent
      frames.push({ event: parsed.event || event, seq: parsed.seq, timestamp: parsed.timestamp, data: parsed.data || {} })
    } catch {
      frames.push({ event: 'error', data: { code: 'INVALID_SSE', message: '服务返回了无效的 SSE 数据。' } })
    }
  }
  return { frames, rest }
}

async function send(): Promise<void> {
  const question = input.value.trim()
  const conversation = active.value
  if (!question || !conversation || running.value) return
  running.value = true
  debugEvents.value = []
  input.value = ''
  const user: Message = { role: 'user', content: question, steps: [], citations: [] }
  const answer: Message = { role: 'assistant', content: '', steps: [], citations: [] }
  conversation.messages.push(user, answer)
  if (conversation.title === '新对话') conversation.title = titleFor(question)
  persist()
  const abortController = new AbortController()
  controller.value = abortController
  try {
    const response = await fetch('/agentMain/api/v1/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...gatewayAuthorizationHeader() },
      body: JSON.stringify({ user_input: question, session_id: conversation.sessionId }),
      signal: abortController.signal,
    })
    if (!response.ok || !response.body) throw new Error(`请求失败（HTTP ${response.status}）`)
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let pending = ''
    let ended = false
    while (!ended) {
      const result = await reader.read()
      ended = result.done
      pending += decoder.decode(result.value || new Uint8Array(), { stream: !ended })
      const parsed = parseFrames(pending)
      pending = parsed.rest
      for (const event of parsed.frames) {
        debugEvents.value.push(event)
        if (event.event === 'block') {
          const delta = typeof event.data.delta === 'string' ? event.data.delta : typeof event.data.content === 'string' ? event.data.content : ''
          answer.content += delta
        } else if (event.event === 'tool') {
          appendStep(answer, event)
        } else if (event.event === 'error') {
          answer.error = typeof event.data.message === 'string' ? event.data.message : 'Agent 返回错误。'
        }
      }
    }
    if (!answer.content && !answer.error) answer.error = '服务结束，但未返回可展示内容。'
  } catch (reason) {
    answer.error = reason instanceof DOMException && reason.name === 'AbortError' ? '请求已终止。' : reason instanceof Error ? reason.message : '请求失败。'
  } finally {
    running.value = false
    controller.value = null
    persist()
  }
}

function stop(): void {
  controller.value?.abort()
}

async function checkHealth(): Promise<void> {
  try {
    const response = await fetch('/agentMain/health')
    connected.value = response.ok
  } catch {
    connected.value = false
  }
}

onMounted(() => { loadConversations(); void checkHealth() })
onBeforeUnmount(() => controller.value?.abort())
</script>

<template>
  <section class="chat-page">
    <aside class="chat-sidebar" aria-label="会话历史">
      <div class="chat-brand"><strong>Muye</strong><span>在线体验</span></div>
      <button type="button" class="new-chat" :disabled="running" @click="newConversation">新建对话</button>
      <p class="connection"><span :class="['connection-dot', { connected }]" />{{ connected ? '连接正常' : '连接不可用' }}</p>
      <div class="conversation-list">
        <button v-for="conversation in conversations" :key="conversation.id" type="button" :class="{ active: conversation.id === activeId }" :disabled="running" @click="activeId = conversation.id">
          {{ conversation.title }}
        </button>
      </div>
      <p class="dev-agent">{{ localDev ? 'local-dev · agent-main 编排' : 'agent-main · 主编排' }}</p>
    </aside>
    <main class="chat-workspace">
      <header class="chat-header">
        <div><strong>LIVE CONVERSATION</strong><span>Session ID: {{ active?.sessionId }}</span></div>
        <button v-if="canDebug" type="button" class="debug-toggle" :aria-expanded="debugOpen" @click="debugOpen = !debugOpen">调试</button>
      </header>
      <div class="chat-layout">
        <div class="chat-thread" aria-live="polite">
          <p v-if="!active?.messages.length" class="chat-empty">开始一次新的对话，验证 Main 如何选择当前本地 Agent。</p>
          <article v-for="(message, index) in active?.messages" :key="index" :class="['chat-message', message.role, { error: message.error }]">
            <p class="message-role">{{ message.role === 'user' ? '你' : 'Muye' }}</p>
            <div class="message-content">{{ message.content }}</div>
            <p v-if="message.error" role="alert">{{ message.error }}</p>
            <details v-if="message.steps.length" class="execution"><summary>执行过程（{{ message.steps.length }}）</summary><ul><li v-for="(step, stepIndex) in message.steps" :key="stepIndex">{{ step }}</li></ul></details>
            <ul v-if="message.citations.length" class="citations"><li v-for="citation in message.citations" :key="`${citation.title}-${citation.source}`">{{ citation.title }} · {{ citation.source }}</li></ul>
          </article>
        </div>
        <aside v-if="debugOpen" class="debug-panel" aria-label="调试事件">
          <h2>SSE 调试</h2>
          <p v-if="!debugEvents.length">等待事件</p>
          <ol><li v-for="(event, index) in debugEvents" :key="index"><code>#{{ event.seq ?? '-' }} {{ event.event }}</code><pre>{{ JSON.stringify(event.data, null, 2) }}</pre></li></ol>
        </aside>
      </div>
      <form class="composer" @submit.prevent="send">
        <textarea v-model="input" rows="3" maxlength="4000" :disabled="running" placeholder="输入问题，Enter 发送，Shift + Enter 换行" @keydown.enter.exact.prevent="send" />
        <button v-if="running" type="button" @click="stop">终止</button><button v-else type="submit" :disabled="!input.trim()">发送</button>
      </form>
    </main>
  </section>
</template>
