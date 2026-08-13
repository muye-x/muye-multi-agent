<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { api } from '../api'

const status = ref<'loading' | 'unbound' | 'wait' | 'scaned' | 'need_verifycode' | 'active' | 'expired' | 'error'>('loading')
const sessionId = ref('')
const qrSvg = ref('')
const verifyCode = ref('')
const error = ref('')
let poller: ReturnType<typeof setTimeout> | undefined

const statusText = computed(() => ({ loading: '正在加载', unbound: '未绑定', wait: '请使用微信扫码', scaned: '已扫码，请在手机确认', need_verifycode: '请输入微信验证码', active: '已绑定', expired: '二维码已过期', error: '操作失败' }[status.value]))

function clearPoller() { if (poller) window.clearTimeout(poller); poller = undefined }
async function load() { try { status.value = (await api.wechatStatus()).status } catch { status.value = 'error'; error.value = '无法读取微信绑定状态' } }
async function poll() {
  if (!sessionId.value) return
  try {
    const response = await api.wechatQrStatus(sessionId.value)
    if (response.status === 'confirmed') { status.value = 'active'; clearPoller(); return }
    if (response.status === 'need_verifycode') { status.value = 'need_verifycode'; clearPoller(); return }
    if (response.status === 'expired' || response.status === 'verify_code_blocked') { status.value = 'expired'; clearPoller(); return }
    status.value = response.status === 'scaned' ? 'scaned' : 'wait'
    poller = window.setTimeout(poll, 1500)
  } catch { status.value = 'error'; error.value = '二维码状态请求失败'; clearPoller() }
}
async function createQrCode() { clearPoller(); error.value = ''; try { const response = await api.wechatQrCode(); sessionId.value = response.session_id; qrSvg.value = response.qr_svg; status.value = 'wait'; await poll() } catch { status.value = 'error'; error.value = '无法创建二维码' } }
async function submitVerify() { if (!verifyCode.value.trim() || !sessionId.value) return; try { await api.wechatVerify(sessionId.value, verifyCode.value.trim()); verifyCode.value = ''; status.value = 'wait'; await poll() } catch { error.value = '验证码提交失败' } }
async function unbind() { clearPoller(); try { await api.wechatUnbind(); sessionId.value = ''; qrSvg.value = ''; status.value = 'unbound' } catch { error.value = '解绑失败' } }
onBeforeUnmount(clearPoller)
void load()
</script>

<template>
  <section class="page-panel" aria-labelledby="wechat-title">
    <h1 id="wechat-title">微信接入</h1>
    <p>{{ statusText }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <button v-if="status === 'unbound' || status === 'expired' || status === 'error'" type="button" @click="createQrCode">获取二维码</button>
    <img v-if="qrSvg && status !== 'active'" :src="qrSvg" width="240" height="240" alt="微信绑定二维码" />
    <form v-if="status === 'need_verifycode'" @submit.prevent="submitVerify">
      <label>验证码 <input v-model="verifyCode" inputmode="numeric" maxlength="32" required /></label>
      <button type="submit">确认</button>
    </form>
    <button v-if="status === 'active'" type="button" @click="unbind">解除绑定</button>
  </section>
</template>
