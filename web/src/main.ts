import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'

const routes = [
  { path: '/login', component: () => import('./views/LoginView.vue') },
  { path: '/', component: () => import('./views/OverviewView.vue') },
  { path: '/agents', component: () => import('./views/AgentsView.vue') },
  { path: '/grants', component: () => import('./views/GrantsView.vue') },
]
const router = createRouter({ history: createWebHistory(), routes })
router.beforeEach((to) => (to.path !== '/login' && !sessionStorage.getItem('muye_access_token') ? '/login' : true))

createApp(App).use(createPinia()).use(router).mount('#app')
