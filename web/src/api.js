async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  })
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) {
    const error = new Error(data?.error || response.statusText)
    error.status = response.status
    throw error
  }
  return data
}

export const api = {
  status: () => request('/api/status'),
  settings: () => request('/api/settings'),
  saveSettings: (body) => request('/api/settings', { method: 'PUT', body: JSON.stringify(body) }),
  resetData: () => request('/api/settings/reset', { method: 'POST' }),
  updateStatus: () => request('/api/update'),
  applyUpdate: () => request('/api/update', { method: 'POST' }),
  runs: () => request('/api/runs'),
  run: (id) => request(`/api/runs/${id}`),
  start: (body) => request('/api/runs/start', { method: 'POST', body: JSON.stringify(body || {}) }),
  pause: () => request('/api/runs/pause', { method: 'POST' }),
  resume: () => request('/api/runs/resume', { method: 'POST' }),
  stop: () => request('/api/runs/stop', { method: 'POST' }),
  replaceHeater: (slot) => request(`/api/heaters/${slot}/replace`, { method: 'POST' }),
}
