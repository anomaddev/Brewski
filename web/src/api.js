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
  updateStatus: ({ fresh = false } = {}) => request(fresh ? '/api/update?fresh=1' : '/api/update'),
  applyUpdate: () => request('/api/update', { method: 'POST' }),
  reboot: () => request('/api/reboot', { method: 'POST' }),
  timeline: (from, to, max = 1600) => request(`/api/timeline?from=${from}&to=${to}&max=${max}`),
  runs: () => request('/api/runs'),
  run: (id, { logs = true } = {}) => request(`/api/runs/${id}${logs ? '' : '?logs=0'}`),
  runLogs: (id, after = 0) => request(`/api/runs/${id}/logs?after=${after}`),
  start: (body) => request('/api/runs/start', { method: 'POST', body: JSON.stringify(body || {}) }),
  cleanCalibration: () => request('/api/calibration/clean', { method: 'POST' }),
  pause: () => request('/api/runs/pause', { method: 'POST' }),
  resume: () => request('/api/runs/resume', { method: 'POST' }),
  stop: () => request('/api/runs/stop', { method: 'POST' }),
  replaceHeater: (slot) => request(`/api/heaters/${slot}/replace`, { method: 'POST' }),
}
