import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const localDev = process.env.VITE_MUYE_LOCAL_DEV === 'true'
const mainUrl = process.env.MUYE_DEV_GATEWAY_MAIN_URL
const callerToken = process.env.MUYE_DEV_GATEWAY_CALLER_TOKEN
const userId = process.env.MUYE_DEV_GATEWAY_USER_ID
const channelsUrl = process.env.MUYE_DEV_CHANNELS_URL
const channelsToken = process.env.MUYE_DEV_CHANNELS_CALLER_TOKEN

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
      ...(channelsUrl && channelsToken ? {
        '/api/v2/channels': {
          target: channelsUrl,
          changeOrigin: true,
          rewrite: (path: string) => path.replace(/^\/api\/v2\/channels(?=\/|$)/, '/api/v1'),
          configure: (proxy: { on: (name: string, handler: (request: { setHeader: (name: string, value: string) => void }) => void) => void }) => {
            proxy.on('proxyReq', (request) => {
              request.setHeader('Authorization', `Bearer ${channelsToken}`)
              request.setHeader('X-Muye-User-Id', userId)
            })
          },
        },
      } : {}),
    },
  } : undefined,
})
