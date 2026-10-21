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

export function formatRate(perMin) {
  if (perMin == null || !Number.isFinite(Number(perMin))) return '—'
  const rounded = Math.round(Number(perMin) * 10) / 10
  if (rounded === 0) return '0.0°F/min'
  return `${rounded > 0 ? '+' : ''}${rounded.toFixed(1)}°F/min`
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

export function kindBadge(kind) {
  if (kind === 'calibration') {
    return '<span class="status-badge status-calibration">Calibration</span> '
  }
  if (kind === 'test') {
    return '<span class="status-badge status-test">Test</span> '
  }
  return ''
}

function phaseClock(test) {
  const elapsed = Math.max(0, Math.round(Number(test.phase_seconds) || 0))
  const limit = Math.max(0, Math.round(Number(test.phase_limit) || 0))
  const show = (value) => {
    const minutes = Math.floor(value / 60)
    const seconds = value % 60
    return `${minutes}:${String(seconds).padStart(2, '0')}`
  }
  if (!limit) return show(elapsed)
  return `${show(elapsed)} of ${show(limit)}`
}

function recordedClock(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return null
  const total = Math.max(0, Math.round(Number(seconds)))
  const minutes = Math.floor(total / 60)
  const remain = total % 60
  return `${minutes}:${String(remain).padStart(2, '0')}`
}

export function testSummary(test) {
  if (!test) return ''
  if (!test.review) {
    const clock = phaseClock(test)
    if (test.phase === 'settle') return `Initial Heating. Settling ${clock} with the elements off.`
    if (test.phase === 'heating') return `Initial Heating. Heaters on, ${clock}.`
    if (test.phase === 'coast') return `Initial Heating. Heaters off, ${clock}.`
    return 'Initial Heating.'
  }
  const on = recordedClock(test.on_seconds)
  const off = recordedClock(test.off_seconds)
  if (!on) {
    return test.stopped
      ? 'Initial Heating stopped before the heaters came on.'
      : 'Initial Heating ended before the heaters came on.'
  }
  const span = off ? `${on} on and ${off} off` : `${on} on`
  if (test.stopped) return `Stopped early, after ${span}. The curve so far is in this run.`
  if (!test.found) return `Initial Heating ended early, after ${span}. The curve so far is in this run.`
  return `Recorded ${span}. The curve is in this run.`
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
