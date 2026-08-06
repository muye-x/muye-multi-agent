import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const localDev = process.env.VITE_MUYE_LOCAL_DEV === 'true'
const mainUrl = process.env.MUYE_DEV_GATEWAY_MAIN_URL
const callerToken = process.env.MUYE_DEV_GATEWAY_CALLER_TOKEN
const userId = process.env.MUYE_DEV_GATEWAY_USER_ID

if (localDev && (!mainUrl || !callerToken || !userId)) {
  throw new Error('local-dev Gateway requires MUYE_DEV_GATEWAY_MAIN_URL, caller token, and user id')
}

export default defineConfig({
  plugins: [vue()],
  base: '/',
  server: localDev ? {
    host: '127.0.0.1',
    proxy: {
      '/agentMain': {
        target: mainUrl,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/agentMain(?=\/|$)/, ''),
        configure: (proxy) => {
          proxy.on('proxyReq', (request) => {
            request.setHeader('Authorization', `Bearer ${callerToken}`)
            request.setHeader('X-Muye-User-Id', userId)
          })
        },
      },
    },
  } : undefined,
})
