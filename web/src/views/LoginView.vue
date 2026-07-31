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
  <section class="login">
    <form @submit.prevent="submit">
      <h1>Muye Control</h1>
      <label
        >用户名<input
          v-model.trim="username"
          autocomplete="username"
          required
          minlength="3" /></label
      ><label
        >密码<input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          minlength="12"
      /></label>
      <p v-if="error" role="alert">{{ error }}</p>
      <button :disabled="pending">{{ pending ? "登录中" : "登录" }}</button>
    </form>
  </section>
</template>
