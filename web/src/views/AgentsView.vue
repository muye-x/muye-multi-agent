<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, type Agent } from '../api'
const agents = ref<Agent[]>([]); const error = ref('')
onMounted(async () => { try { agents.value = (await api.agents()).agents } catch (reason) { error.value = reason instanceof Error ? reason.message : '加载失败' } })
</script>
<template>
  <section>
    <h1>Agent 详情</h1>
    <p v-if="error" role="alert">{{ error }}</p>
    <article v-for="agent in agents" :key="agent.agent_id">
      <h2>{{ agent.display_name }}</h2>
      <p>{{ agent.description }}</p>
      <small>{{ agent.agent_id }} · {{ agent.status }}</small>
    </article>
  </section>
</template>
