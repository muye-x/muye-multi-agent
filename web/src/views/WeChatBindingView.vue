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
  <section class="console-page wechat-page" aria-labelledby="wechat-title">
    <header class="page-heading"><p>CHANNELS</p><h1 id="wechat-title">微信接入</h1><span>管理 Control Console 与微信账号的连接状态。</span></header>
    <div class="wechat-layout">
      <div class="binding-status data-panel">
        <span :class="['binding-dot', status]" aria-hidden="true" />
        <div><span>当前状态</span><strong>{{ statusText }}</strong></div>
        <p v-if="status === 'active'">微信已连接，可以正常接收和发送消息。</p>
        <p v-else>完成扫码确认后，消息将通过已绑定的微信账号收发。</p>
      </div>
      <div class="data-panel binding-workspace">
        <div class="data-panel-heading"><h2>账号绑定</h2><span>微信</span></div>
        <p v-if="error" class="inline-alert" role="alert">{{ error }}</p>
        <div v-if="qrSvg && status !== 'active'" class="qr-panel"><img :src="qrSvg" width="240" height="240" alt="微信绑定二维码" /><p>{{ statusText }}</p></div>
        <form v-if="status === 'need_verifycode'" class="verify-form" @submit.prevent="submitVerify"><label for="wechat-verify-code">微信验证码</label><div><input id="wechat-verify-code" v-model="verifyCode" inputmode="numeric" maxlength="32" required /><button type="submit">确认</button></div></form>
        <div class="binding-actions">
          <button v-if="status === 'unbound' || status === 'expired' || status === 'error'" type="button" @click="createQrCode">获取二维码</button>
          <button v-if="status === 'active'" class="secondary-button" type="button" @click="unbind">解除绑定</button>
        </div>
      </div>
    </div>
  </section>
</template>
