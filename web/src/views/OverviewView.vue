<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, type Agent } from '../api'

const agents = ref<Agent[]>([])
const error = ref('')
const runningCount = computed(() => agents.value.filter((agent) => agent.status === 'ACTIVE').length)
const startingCount = computed(() => agents.value.filter((agent) => agent.status === 'STARTING').length)
const unhealthyCount = computed(() => agents.value.filter((agent) => !['ACTIVE', 'STARTING'].includes(agent.status)).length)

function statusLabel(status: string): string {
  return ({ ACTIVE: '运行中', STARTING: '启动中', UNHEALTHY: '异常', STOPPED: '已停止' }[status] ?? status)
}

onMounted(async () => {
  try {
    agents.value = (await api.topology()).agents
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '加载失败'
  }
})
</script>

<template>
  <section class="overview-page" aria-labelledby="overview-title">
    <header class="page-heading">
      <div>
        <p class="eyebrow">CONTROL PLANE</p>
        <h1 id="overview-title">服务状态</h1>
        <p>查看已登记 Agent 的当前可用状态与能力信息。</p>
      </div>
    </header>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-else>
      <dl class="agent-metrics" aria-label="Agent 状态概览">
        <div>
          <dt>已发现</dt>
          <dd>{{ agents.length }}</dd>
        </div>
        <div class="metric-running">
          <dt>运行中</dt>
          <dd>{{ runningCount }}</dd>
        </div>
        <div class="metric-starting">
          <dt>启动中</dt>
          <dd>{{ startingCount }}</dd>
        </div>
        <div v-if="unhealthyCount" class="metric-unhealthy">
          <dt>需处理</dt>
          <dd>{{ unhealthyCount }}</dd>
        </div>
      </dl>

      <div class="agent-list" aria-live="polite">
        <article v-for="agent in agents" :key="agent.agent_id" class="agent-card">
          <header class="agent-card-header">
            <div>
              <h2>{{ agent.display_name }}</h2>
              <p class="agent-id">{{ agent.agent_id }}</p>
            </div>
            <span class="agent-status" :class="`agent-status-${agent.status.toLowerCase()}`">
              {{ statusLabel(agent.status) }}
            </span>
          </header>
          <p class="agent-description">{{ agent.description }}</p>
          <dl class="agent-details">
            <div>
              <dt>版本</dt>
              <dd>{{ agent.agent_version }}</dd>
            </div>
            <div>
              <dt>支持能力</dt>
              <dd>{{ agent.supported_intents.length }} 项</dd>
            </div>
          </dl>
          <ul v-if="agent.supported_intents.length" class="intent-list" aria-label="支持能力">
            <li v-for="intent in agent.supported_intents" :key="intent">{{ intent }}</li>
          </ul>
        </article>
        <p v-if="!agents.length" class="empty-state">暂无已发现 Agent</p>
      </div>
    </template>
  </section>
</template>
