<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "../api";
const username = ref("");
const password = ref("");
const error = ref("");
const pending = ref(false);
const router = useRouter();
async function submit() {
  pending.value = true;
  error.value = "";
  try {
    const result = await api.login(username.value, password.value);
    sessionStorage.setItem("muye_access_token", result.access_token);
    await router.push("/");
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "登录失败";
  } finally {
    pending.value = false;
  }
}
</script>
<template>
  <section class="login" aria-labelledby="login-title">
    <form class="login-panel" @submit.prevent="submit">
      <header class="login-heading">
        <span class="login-mark" aria-hidden="true">M</span>
        <div>
          <p>Muye Multi-Agent</p>
          <h1 id="login-title">Control Console</h1>
        </div>
      </header>
      <div class="login-fields">
        <label for="login-username">
          用户名
          <input
            id="login-username"
            v-model.trim="username"
            autocomplete="username"
            required
            minlength="3"
          />
        </label>
        <label for="login-password">
          密码
          <input
            id="login-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
            minlength="12"
          />
        </label>
      </div>
      <p v-if="error" class="login-error" role="alert">{{ error }}</p>
      <button type="submit" :disabled="pending">{{ pending ? "登录中" : "登录" }}</button>
    </form>
  </section>
</template>
