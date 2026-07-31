<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Agent } from '../api'
const agents = ref<Agent[]>([]); const error = ref('')
onMounted(async () => { try { agents.value = (await api.topology()).agents } catch (reason) { error.value = reason instanceof Error ? reason.message : '加载失败' } })
</script>
<template>
  <section>
    <h1>服务状态</h1>
    <p v-if="error" role="alert">{{ error }}</p>
    <table v-else>
      <thead>
        <tr>
          <th>Agent</th>
          <th>版本</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="agent in agents" :key="agent.agent_id">
          <td>{{ agent.display_name }}</td>
          <td>{{ agent.agent_version }}</td>
          <td>
            <span class="status">{{ agent.status }}</span>
          </td>
        </tr>
        <tr v-if="!agents.length">
          <td colspan="3">暂无已发现 Agent</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
