import { escapeHtml, formatDuration, formatTarget, formatTemp } from './format.js'

export const PAGE_SIZES = [10, 25, 50, 100]

export const EVENT_FILTERS = [
  ['sample', 'Samples'],
  ['heat', 'Heat'],
  ['phase', 'Phase'],
  ['start', 'Start'],
  ['model', 'Model'],
  ['analysis', 'Analysis'],
  ['test', 'Test'],
]

const EVENT_LABELS = Object.fromEntries(EVENT_FILTERS)

export const PHASE_LABELS = {
  settle: 'Settle',
  initial: 'Initial heating',
  cold_start: 'Initial heating',
  linear: 'Linear heating',
  coast: 'Coasting',
  adjust: 'Adjusted',
  momentum: 'Adjusted',
  approach: 'Pulse',
  approach_coast: 'Pulse coast',
  pulse: 'Pulse',
  pulse_coast: 'Pulse coast',
  pulses: 'Pulse',
  climb: 'Climb',
  hold: 'Holding',
  heating: 'Initial Heating',
  run: 'Run',
  cutoff: 'Run',
  sustain: 'Run',
  model: 'Run',
  cooldown: 'Cooldown',
  pause: 'Paused',
  stop: 'Stopped',
  complete: 'Complete',
}

export function phaseFilterOptions() {
  const seen = new Set()
  const options = []
  for (const [key, label] of Object.entries(PHASE_LABELS)) {
    if (seen.has(label)) continue
    seen.add(label)
    options.push({ key, label })
  }
  return options
}

export function parseClock(value) {
  const text = String(value ?? '').trim()
  if (!text) return null
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text)
  const [clock, fraction = ''] = text.split('.')
  const parts = clock.split(':').map((part) => Number(part))
  if (parts.some((part) => !Number.isFinite(part) || part < 0)) return null
  if (fraction && !/^\d{1,2}$/.test(fraction)) return null
  let seconds = null
  if (parts.length === 2) seconds = parts[0] * 60 + parts[1]
  else if (parts.length === 3) seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (seconds == null) return null
  if (!fraction) return seconds
  return seconds + Number(`0.${fraction}`)
}

export function formatClock(totalSeconds) {
  const value = Math.max(0, Number(totalSeconds) || 0)
  const whole = Math.floor(value)
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const seconds = whole % 60
  const base = [hours, minutes, seconds].map((part) => String(part).padStart(2, '0')).join(':')
  const fraction = Math.round((value - whole) * 100)
  if (!fraction) return base
  return `${base}.${String(fraction).padStart(2, '0')}`
}

export function formatWindow(window) {
  if (!window) return 'No rows in this window'
  const parts = []
  if (window.from != null && window.to != null) {
    parts.push(`${formatDuration(window.from)} – ${formatDuration(window.to)}`)
  }
  if (window.cooldown_from != null && window.cooldown_to != null) {
    parts.push(`Cooldown ${formatDuration(window.cooldown_from)} – ${formatDuration(window.cooldown_to)}`)
  }
  return parts.length ? parts.join(' · ') : 'No rows in this window'
}

export function pagerHtml({ offset, total, limit, noun, latest = false }) {
  const pages = Math.max(1, Math.ceil(total / limit) || 1)
  const page = total ? Math.floor(offset / limit) + 1 : 1
  const range = !total ? `0 ${noun}` : `${offset + 1}–${Math.min(total, offset + limit)} of ${total}`
  if (pages < 2 && !latest) {
    return `<div class="review-pager"><span class="review-count">${range}</span></div>`
  }
  return `
    <div class="review-pager">
      <button type="button" class="chart-tool" data-move="prev" ${page <= 1 ? 'disabled' : ''} aria-label="Previous page">‹</button>
      <label class="review-jump">
        <span class="review-jump-label">Page</span>
        <input data-jump type="text" inputmode="numeric" value="${page}" aria-label="${escapeHtml(noun)} page">
        <span>of ${pages}</span>
      </label>
      <button type="button" class="chart-tool" data-move="next" ${page >= pages ? 'disabled' : ''} aria-label="Next page">›</button>
      ${latest ? '<button type="button" class="chart-now" data-move="latest">Latest</button>' : ''}
      <span class="review-count">${range}</span>
    </div>`
}

export function sampleHeadHtml(sensorSlots, heaterSlots) {
  return `
    <th>Elapsed</th>
    <th>Target</th>
    ${sensorSlots.map((slot) => `<th>${escapeHtml(slot.name)}</th>`).join('')}
    ${heaterSlots.map((slot) => `<th>${escapeHtml(slot.name)}</th>`).join('')}`
}

export function sampleBodyHtml(samples, sensorSlots, heaterSlots) {
  const columns = 2 + sensorSlots.length + heaterSlots.length
  if (!samples.length) {
    return `<tr><td colspan="${columns}" class="empty-cell">No samples in this window.</td></tr>`
  }
  const cell = (label, value) => `<td data-label="${escapeHtml(label)}">${value}</td>`
  return samples
    .map((sample) => {
      const temps = sensorSlots
        .map((slot) => cell(slot.name, formatTemp(sample.temps_f?.[slot.index])))
        .join('')
      const states = heaterSlots
        .map((slot) => cell(slot.name, sample.heaters_on?.[slot.index] ? 'On' : 'Off'))
        .join('')
      return `
        <tr>
          ${cell('Elapsed', formatDuration(sample.elapsed_seconds))}
          ${cell('Target', formatTarget(sample.target_temp_f))}
          ${temps}
          ${states}
        </tr>`
    })
    .join('')
}

export function controlBodyHtml(logs, heaterSlots, openGroups) {
  if (!logs.length) {
    return '<tr><td colspan="8" class="empty-cell">No control lines in this window.</td></tr>'
  }
  return groupControlLogs(logs)
    .map((group) => {
      if (group.kind === 'row') return controlRow(group.row, heaterSlots)
      const open = openGroups.has(String(group.id))
      const head = groupHeader(group, heaterSlots, open)
      if (!open) return head
      return head + group.rows.map((row) => controlRow(row, heaterSlots, true)).join('')
    })
    .join('')
}

function groupControlLogs(logs) {
  const groups = []
  let bucket = null
  const flush = () => {
    if (!bucket) return
    if (bucket.rows.length < 2) groups.push({ kind: 'row', row: bucket.rows[0] })
    else groups.push({ kind: 'group', id: bucket.rows[0].id, rows: bucket.rows })
    bucket = null
  }
  for (const row of logs) {
    if (row.event !== 'sample') {
      flush()
      groups.push({ kind: 'row', row })
      continue
    }
    const signature = groupSignature(row)
    if (bucket && bucket.signature === signature) bucket.rows.push(row)
    else {
      flush()
      bucket = { signature, rows: [row] }
    }
  }
  flush()
  return groups
}

function groupSignature(row) {
  const detail = row.detail || {}
  const phase = detail.path || detail.phase || ''
  const heaters = Array.isArray(detail.heaters_on) ? detail.heaters_on.map((on) => (on ? '1' : '0')).join('') : ''
  return `${phase}|${heaters}`
}

function groupHeader(group, heaterSlots, open) {
  const rows = group.rows
  const first = rows[0]
  const last = rows[rows.length - 1]
  const detail = first.detail || {}
  const span = `${formatDuration(first.elapsed_seconds)} – ${formatDuration(last.elapsed_seconds)}`
  const count = `${rows.length} readings`
  const action = open ? 'Hide' : 'Show'
  const cell = (label, value) => `<td data-label="${escapeHtml(label)}">${value}</td>`
  return `
    <tr class="log-group-head" data-group="${group.id}">
      ${cell(
        'Elapsed',
        `<button type="button" class="log-group-toggle" aria-expanded="${open ? 'true' : 'false'}" aria-label="${escapeHtml(`${action} ${count}, ${span}`)}"><span aria-hidden="true">${open ? '▾' : '▸'}</span> ${escapeHtml(span)}</button>`,
      )}
      ${cell('Event', 'Samples')}
      ${cell('Phase', escapeHtml(phaseLabel(detail)))}
      ${cell('Temp', escapeHtml(numberSpan(rows, (item) => item.temp_f, formatTemp)))}
      ${cell('Heaters', escapeHtml(heaterText(detail, heaterSlots)))}
      ${cell('Slope', escapeHtml(numberSpan(rows, slopeValue, formatSlopeValue, 0.0005)))}
      ${cell('Crest', escapeHtml(numberSpan(rows, (item) => item.predicted_crest_f, formatTemp)))}
      ${cell('Calculation', `<span class="control-note">${count}</span>`)}
    </tr>`
}

function controlRow(entry, heaterSlots, child = false) {
  const detail = entry.detail || {}
  const cell = (label, value) => `<td data-label="${escapeHtml(label)}">${value}</td>`
  return `
    <tr class="${child ? 'log-group-child' : ''}">
      ${cell('Elapsed', formatDuration(entry.elapsed_seconds))}
      ${cell('Event', escapeHtml(EVENT_LABELS[entry.event] || entry.event || '—'))}
      ${cell('Phase', escapeHtml(phaseLabel(detail)))}
      ${cell('Temp', formatTemp(detail.temp_f))}
      ${cell('Heaters', escapeHtml(heaterText(detail, heaterSlots)))}
      ${cell('Slope', escapeHtml(formatSlope(detail)))}
      ${cell('Crest', formatTemp(detail.predicted_crest_f))}
      ${cell('Calculation', `<span class="control-note">${escapeHtml(entry.message || '')}</span>`)}
    </tr>`
}

function phaseLabel(detail) {
  const path = detail.path
  if (path && PHASE_LABELS[path]) return PHASE_LABELS[path]
  if (detail.phase && PHASE_LABELS[detail.phase]) return PHASE_LABELS[detail.phase]
  return detail.phase || '—'
}

function slopeValue(detail) {
  const value = detail.live_slope_f_per_sec ?? detail.slope_f_per_sec ?? detail.sheath_slope_f_per_sec
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function formatSlope(detail) {
  const value = slopeValue(detail)
  if (value == null) return '—'
  return formatSlopeValue(value)
}

function formatSlopeValue(value) {
  return `${Number(value).toFixed(3)}°F/s`
}

function numberSpan(rows, read, format, epsilon = 0.05) {
  const values = []
  for (const row of rows) {
    const value = read(row.detail || {})
    if (value != null && Number.isFinite(Number(value))) values.push(Number(value))
  }
  if (!values.length) return '—'
  const low = Math.min(...values)
  const high = Math.max(...values)
  if (Math.abs(high - low) < epsilon) return format(low)
  return `${format(low)} – ${format(high)}`
}

function heaterText(detail, heaterSlots) {
  const states = detail.heaters_on
  if (!Array.isArray(states)) return '—'
  const slots = heaterSlots.length
    ? heaterSlots
    : states.map((_, index) => ({ name: `Heater ${index + 1}`, index }))
  const named = slots.map((slot) => `${slot.name} ${states[slot.index] ? 'on' : 'off'}`)
  if (named.every((line) => line.endsWith(' on'))) return 'On'
  if (named.every((line) => line.endsWith(' off'))) return 'Off'
  return named.join(', ')
}
