<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from "vue-router";
import { api } from "./api";
const router = useRouter();
async function logout() {
  try {
    await api.logout();
  } finally {
    sessionStorage.removeItem("muye_access_token");
    await router.push("/login");
  }
}
</script>
<template>
  <main>
    <nav v-if="$route.path !== '/login'">
      <strong>Muye Control</strong><RouterLink to="/">状态</RouterLink
      ><RouterLink to="/agents">Agents</RouterLink
      ><RouterLink to="/grants">授权</RouterLink
      ><button title="退出登录" aria-label="退出登录" @click="logout">退出</button>
    </nav>
    <RouterView />
  </main>
</template>
