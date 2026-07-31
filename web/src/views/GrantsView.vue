<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, type Agent, type User } from '../api'
const users = ref<User[]>([]); const agents = ref<Agent[]>([]); const selected = ref(''); const grants = ref<string[]>([]); const error = ref(''); const saved = ref(false)
async function loadGrants() { if (!selected.value) return; grants.value = (await api.grants(selected.value)).agent_ids }
function toggle(agentId: string) { grants.value = grants.value.includes(agentId) ? grants.value.filter((id) => id !== agentId) : [...grants.value, agentId] }
async function save() { try { await api.replaceGrants(selected.value, grants.value); saved.value = true } catch (reason) { error.value = reason instanceof Error ? reason.message : '保存失败' } }
watch(selected, () => { void loadGrants() })
onMounted(async () => { try { [users.value, agents.value] = [(await api.users()).users, (await api.agents()).agents]; selected.value = users.value[0]?.user_id || '' } catch (reason) { error.value = reason instanceof Error ? reason.message : '加载失败' } })
</script>
<template>
  <section>
    <h1>User-Agent 授权</h1>
    <p v-if="error" role="alert">{{ error }}</p>
    <label
      >用户<select v-model="selected">
        <option v-for="user in users" :key="user.user_id" :value="user.user_id">
          {{ user.username }}
        </option>
      </select></label
    ><label v-for="agent in agents" :key="agent.agent_id" class="grant"
      ><input
        type="checkbox"
        :checked="grants.includes(agent.agent_id)"
        @change="toggle(agent.agent_id)"
      />{{ agent.display_name }}</label
    ><button :disabled="!selected" @click="save">保存授权</button>
    <p v-if="saved">已保存</p>
  </section>
</template>
