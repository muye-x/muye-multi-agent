<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from "vue-router";
const router = useRouter();
function logout() {
  sessionStorage.removeItem("muye_access_token");
  router.push("/login");
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
