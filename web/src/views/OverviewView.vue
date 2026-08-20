<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, type Agent } from '../api'

const agents = ref<Agent[]>([])
const error = ref('')
const activeAgents = computed(() => agents.value.filter((agent) => agent.status === 'active').length)

onMounted(async () => { try { agents.value = (await api.topology()).agents } catch (reason) { error.value = reason instanceof Error ? reason.message : '加载失败' } })
</script>
<template>
  <section class="console-page overview-page" aria-labelledby="overview-title">
    <header class="page-heading">
      <p>CONTROL PLANE</p>
      <h1 id="overview-title">服务状态</h1>
      <span>查看当前已注册 Agent 的版本与运行状态。</span>
    </header>
    <div class="status-summary" aria-label="服务摘要">
      <div><span>已发现 Agent</span><strong>{{ agents.length }}</strong></div>
      <div><span>运行中</span><strong>{{ activeAgents }}</strong></div>
    </div>
    <div class="data-panel">
      <div class="data-panel-heading"><h2>服务目录</h2><span>{{ agents.length }} 个 Agent</span></div>
      <p v-if="error" class="inline-alert" role="alert">{{ error }}</p>
      <div v-else class="table-scroll">
        <table>
          <thead><tr><th>Agent</th><th>版本</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="agent in agents" :key="agent.agent_id">
              <td><strong>{{ agent.display_name }}</strong><small>{{ agent.agent_id }}</small></td>
              <td>{{ agent.agent_version }}</td>
              <td><span :class="['status', { inactive: agent.status !== 'active' }]">{{ agent.status }}</span></td>
            </tr>
            <tr v-if="!agents.length"><td colspan="3" class="empty-table">暂无已发现 Agent</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
