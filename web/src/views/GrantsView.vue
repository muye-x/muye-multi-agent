<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api, type Agent, type User } from '../api'
const users = ref<User[]>([])
const agents = ref<Agent[]>([])
const selected = ref('')
const grants = ref<string[]>([])
const error = ref('')
const saved = ref(false)
const selectedUser = computed(() => users.value.find((user) => user.user_id === selected.value))
async function loadGrants() { if (!selected.value) return; saved.value = false; grants.value = (await api.grants(selected.value)).agent_ids }
function toggle(agentId: string) { grants.value = grants.value.includes(agentId) ? grants.value.filter((id) => id !== agentId) : [...grants.value, agentId] }
async function save() { try { await api.replaceGrants(selected.value, grants.value); saved.value = true } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
watch(selected, () => { void loadGrants() })
onMounted(async () => { try { [users.value, agents.value] = [(await api.users()).users, (await api.agents()).agents]; selected.value = users.value[0]?.user_id || '' } catch (reason) { error.value = reason instanceof Error ? reason.message : '加载失败' } })
</script>
<template>
  <section class="console-page grants-page" aria-labelledby="grants-title">
    <header class="page-heading">
      <p>ACCESS CONTROL</p>
      <h1 id="grants-title">User-Agent 授权</h1>
      <span>为用户分配可使用的 Agent 服务。</span>
    </header>
    <p v-if="error" class="inline-alert" role="alert">{{ error }}</p>
    <div class="grants-layout">
      <div class="data-panel user-selector">
        <h2>选择用户</h2>
        <label for="grant-user">当前用户</label>
        <select id="grant-user" v-model="selected">
          <option v-for="user in users" :key="user.user_id" :value="user.user_id">{{ user.username }}</option>
        </select>
        <p v-if="selectedUser">正在管理 {{ selectedUser.username }} 的访问权限。</p>
      </div>
      <div class="data-panel grant-panel">
        <div class="data-panel-heading"><div><h2>可用 Agent</h2><span>已授权 {{ grants.length }} / {{ agents.length }}</span></div><p v-if="saved" class="save-confirmation" role="status">已保存</p></div>
        <fieldset :disabled="!selected">
          <legend class="sr-only">Agent 授权项</legend>
          <label v-for="agent in agents" :key="agent.agent_id" class="grant-row"><input type="checkbox" :checked="grants.includes(agent.agent_id)" @change="toggle(agent.agent_id)" /><span><strong>{{ agent.display_name }}</strong><small>{{ agent.agent_id }}</small></span></label>
          <p v-if="!agents.length" class="empty-state">暂无可授权 Agent</p>
        </fieldset>
        <div class="grant-actions"><button type="button" :disabled="!selected" @click="save">保存授权</button></div>
      </div>
    </div>
  </section>
</template>
