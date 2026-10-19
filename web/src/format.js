export function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

export function formatDuration(totalSeconds) {
  const seconds = Math.max(0, Math.floor(Number(totalSeconds) || 0))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const secs = seconds % 60
  return [hours, minutes, secs].map((part) => String(part).padStart(2, '0')).join(':')
}

export function formatHours(totalSeconds) {
  const hours = Math.max(0, Number(totalSeconds) || 0) / 3600
  if (hours >= 100) return `${hours.toFixed(1)} h`
  return `${hours.toFixed(2)} h`
}

export function formatTemp(value) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${Number(value).toFixed(1)}°F`
}

export function formatTarget(value) {
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${Math.round(Number(value))}°F`
}

export const BOILING_POINT_F = 212

export function aboveBoiling(value) {
  const temperature = Number(value)
  return Number.isFinite(temperature) && temperature > BOILING_POINT_F
}

export function formatWhen(iso) {
  if (!iso) return { date: '—', time: '' }
  const date = new Date(iso)
  return {
    date: date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }),
    time: date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true }),
  }
}

export function statusLabel(status) {
  if (status === 'running') return 'Running'
  if (status === 'paused') return 'Paused'
  if (status === 'cooling') return 'Cooling'
  if (status === 'stopped') return 'Stopped'
  return 'Idle'
}

export function formatCooldown(status, seconds) {
  if (status === 'complete' || status === 'open') return formatDuration(seconds)
  if (status === 'abandoned' || status === 'interrupted') return 'Incomplete'
  return '—'
}

export function activeSlots(record, kind) {
  const channels = record?.[kind]
  if (Array.isArray(channels) && channels.length) {
    return channels
      .map((channel, index) => ({
        name: channel.name,
        index,
        gpio: channel.gpio,
        wires: channel.wires,
        rtd: channel.rtd,
        enabled: channel.enabled !== false,
      }))
      .filter((slot) => slot.enabled)
  }
  const names = record?.[kind === 'heaters' ? 'heater_names' : 'sensor_names'] || []
  const countKey = kind === 'heaters' ? 'heater_count' : 'sensor_count'
  const count = Number(record?.[countKey] ?? names.length)
  return names.slice(0, count).map((name, index) => ({ name, index, gpio: null, enabled: true }))
}

export function statusClass(status) {
  if (status === 'running') return 'status-running'
  if (status === 'paused') return 'status-paused'
  if (status === 'cooling') return 'status-cooling'
  if (status === 'stopped') return 'status-stopped'
  return 'status-idle'
}
