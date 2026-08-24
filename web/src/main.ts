import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import App from './App.vue'
import './styles.css'

const localDev = import.meta.env.DEV && import.meta.env.VITE_MUYE_LOCAL_DEV === 'true'
const routes: RouteRecordRaw[] = [
  { path: '/login', component: () => import('./views/LoginView.vue') },
  localDev ? { path: '/', redirect: '/chat' } : { path: '/', component: () => import('./views/OverviewView.vue') },
  { path: '/chat', component: () => import('./views/ChatView.vue') },
  { path: '/agents', redirect: '/' },
  { path: '/grants', component: () => import('./views/GrantsView.vue') },
  { path: '/channels/wechat', component: () => import('./views/WeChatBindingView.vue') },
]
const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach((to) => (localDev || to.path === '/login' || sessionStorage.getItem('muye_access_token') ? true : '/login'))

createApp(App).use(createPinia()).use(router).mount('#app')
