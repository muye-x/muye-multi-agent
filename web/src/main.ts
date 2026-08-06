import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'

const routes = [
  { path: '/login', component: () => import('./views/LoginView.vue') },
  { path: '/', component: () => import('./views/OverviewView.vue') },
  { path: '/chat', component: () => import('./views/ChatView.vue') },
  { path: '/agents', component: () => import('./views/AgentsView.vue') },
  { path: '/grants', component: () => import('./views/GrantsView.vue') },
]
const router = createRouter({ history: createWebHistory(), routes })
const localDev = import.meta.env.DEV && import.meta.env.VITE_MUYE_LOCAL_DEV === 'true'
router.beforeEach((to) => (localDev || to.path === '/login' || sessionStorage.getItem('muye_access_token') ? true : '/login'))

createApp(App).use(createPinia()).use(router).mount('#app')
