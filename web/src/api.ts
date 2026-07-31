export type Agent = { agent_id: string; agent_version: string; display_name: string; description: string; supported_intents: string[]; status: string }
export type User = { user_id: string; username: string; is_admin: boolean }

const token = () => sessionStorage.getItem('muye_access_token')
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v2${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...(token() ? { Authorization: `Bearer ${token()}` } : {}), ...init.headers } })
  if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail?.message || '请求失败')
  return response.status === 204 ? (undefined as T) : response.json() as Promise<T>
}
export const api = {
  login: (username: string, password: string) => request<{ access_token: string }>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  me: () => request<User>('/me'),
  topology: () => request<{ catalog_revision: string; agents: Agent[] }>('/topology'),
  agents: () => request<{ agents: Agent[] }>('/agents'),
  users: () => request<{ users: User[] }>('/users'),
  grants: (userId: string) => request<{ agent_ids: string[] }>(`/users/${encodeURIComponent(userId)}/agent-grants`),
  replaceGrants: (userId: string, agentIds: string[]) => request(`/users/${encodeURIComponent(userId)}/agent-grants`, { method: 'PUT', body: JSON.stringify({ agent_ids: agentIds }) }),
}
