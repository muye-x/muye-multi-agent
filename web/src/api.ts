export type Agent = { agent_id: string; agent_version: string; display_name: string; description: string; supported_intents: string[]; status: string }
export type User = { user_id: string; username: string; is_admin: boolean }

const token = () => sessionStorage.getItem('muye_access_token')
export const gatewayAuthorizationHeader = (): Record<string, string> => token() ? { Authorization: `Bearer ${token()}` } : {}
let refreshInFlight: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = fetch('/api/v2/auth/refresh', { method: 'POST', credentials: 'same-origin' })
    .then(async (response) => {
      if (!response.ok) return null
      const payload = await response.json() as { access_token?: unknown }
      if (typeof payload.access_token !== 'string' || !payload.access_token) return null
      sessionStorage.setItem('muye_access_token', payload.access_token)
      return payload.access_token
    })
    .catch(() => null)
    .finally(() => { refreshInFlight = null })
  return refreshInFlight
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const send = () => fetch(`/api/v2${path}`, { ...init, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...(token() ? { Authorization: `Bearer ${token()}` } : {}), ...init.headers } })
  let response = await send()
  if (response.status === 401 && path !== '/auth/login' && path !== '/auth/refresh') {
    const refreshed = await refreshAccessToken()
    if (refreshed) response = await send()
  }
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail?.message || '请求失败')
  return response.status === 204 ? (undefined as T) : response.json() as Promise<T>
}
export const api = {
  login: (username: string, password: string) => request<{ access_token: string }>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
  me: () => request<User>('/me'),
  topology: () => request<{ catalog_revision: string; agents: Agent[] }>('/topology'),
  agents: () => request<{ agents: Agent[] }>('/agents'),
  users: () => request<{ users: User[] }>('/users'),
  grants: (userId: string) => request<{ agent_ids: string[] }>(`/users/${encodeURIComponent(userId)}/agent-grants`),
  replaceGrants: (userId: string, agentIds: string[]) => request(`/users/${encodeURIComponent(userId)}/agent-grants`, { method: 'PUT', body: JSON.stringify({ agent_ids: agentIds }) }),
}
