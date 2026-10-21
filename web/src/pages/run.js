import { api } from '../api.js'
import { createTempChart, setChartPoints } from '../chart.js'
import { activeSlots, escapeHtml, formatCooldown, formatDuration, formatTarget, formatTemp, formatWhen, statusClass, statusLabel } from '../format.js'
import { setSourceBadge } from '../layout.js'

function modelDetail(run) {
  const model = run.thermal_model
  if (!model || model.linear_rate_f_per_sec == null) return ''
  const rate = `${Number(model.linear_rate_f_per_sec).toFixed(3)}°F/s`
  const coast = model.coast_rise_f == null ? '' : `, coast ${Number(model.coast_rise_f).toFixed(1)}°F`
  return `<div class="detail-item"><span>Model</span><strong>${rate}${coast}</strong></div>`
}

function markersForRun(samples, run) {
  if (!samples.length) return []
  const indexFor = (elapsed) => {
    let best = 0
    let bestGap = Infinity
    samples.forEach((sample, index) => {
      const gap = Math.abs(Number(sample.elapsed_seconds) - Number(elapsed))
      if (gap < bestGap) {
        bestGap = gap
        best = index
      }
    })
    return best
  }
  const markers = [{ at: indexFor(0), kind: 'start', label: 'Start' }]
  const marks = Array.isArray(run.marks) ? run.marks : []
  const hasCooldown = marks.some((mark) => mark.name === 'Cooldown')
  for (const mark of marks) {
    if (mark.name === 'Start') continue
    markers.push({ at: indexFor(mark.elapsed_seconds), kind: 'mark', label: mark.name })
  }
  if (run.ended_at && !hasCooldown) {
    markers.push({ at: indexFor(run.elapsed_seconds), kind: 'stop', label: 'Stop' })
  }
  return markers
}

export function mountRun(root, params) {
  root.innerHTML = `
    <div class="history-page">
      <p class="back-link"><a href="/history">All runs</a></p>
      <section class="dashboard-card">
        <div class="card-title-with-timer">
          <h2 class="card-title" id="run-title">Run</h2>
          <span class="status-badge" id="run-status"></span>
        </div>
        <div class="detail-grid" id="run-settings"></div>
      </section>
      <section class="dashboard-card chart-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Readings</h2>
        </div>
        <div class="chart-container">
          <canvas id="run-chart"></canvas>
        </div>
      </section>
      <section class="dashboard-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Control log</h2>
          <a class="export-link" id="export-log" href="/api/runs/${encodeURIComponent(params.id)}/log.csv">Export</a>
        </div>
        <p class="log-note">Temperature, heater state, and the calculation, saved about once a second. Export downloads the full log.</p>
        <div class="history-table-container sample-log" id="control-log">
          <table class="history-table">
            <thead>
              <tr>
                <th>Elapsed</th>
                <th>Event</th>
                <th>Phase</th>
                <th>Temp</th>
                <th>Heaters</th>
                <th>Slope</th>
                <th>Crest</th>
                <th>Calculation</th>
              </tr>
            </thead>
            <tbody id="control-body"></tbody>
          </table>
        </div>
      </section>
      <section class="dashboard-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Sample Log</h2>
        </div>
        <p class="form-error" id="run-error" hidden></p>
        <div class="history-table-container sample-log">
          <table class="history-table">
            <thead><tr id="sample-head"></tr></thead>
            <tbody id="sample-body"></tbody>
          </table>
        </div>
      </section>
    </div>
  `

  const chart = createTempChart(document.getElementById('run-chart'))
  let stopped = false
  let lastLogId = 0
  let logReady = false
  const errorEl = document.getElementById('run-error')

  function render(run) {
    document.title = `Brewski — Run ${run.id}`
    const titles = { calibration: 'Calibration', test: 'Test' }
    document.getElementById('run-title').textContent = `${titles[run.kind] || 'Run'} ${run.id}`
    const badge = document.getElementById('run-status')
    badge.textContent = statusLabel(run.status)
    badge.className = `status-badge ${statusClass(run.status)}`
    const start = formatWhen(run.started_at)
    const end = formatWhen(run.ended_at)
    const heaterSlots = activeSlots(run, 'heaters')
    const sensorSlots = activeSlots(run, 'sensors')
    const heaterList = heaterSlots.map((slot) => deviceLabel(slot, 'heaters')).join(', ')
    const sensorList = sensorSlots.map((slot) => deviceLabel(slot, 'sensors')).join(', ')
    document.getElementById('run-settings').innerHTML = `
      <div class="detail-item"><span>Started</span><strong>${escapeHtml(start.date)} ${escapeHtml(start.time)}</strong></div>
      <div class="detail-item"><span>Ended</span><strong>${run.ended_at ? `${escapeHtml(end.date)} ${escapeHtml(end.time)}` : '—'}</strong></div>
      <div class="detail-item"><span>Duration</span><strong>${formatDuration(run.elapsed_seconds)}</strong></div>
      <div class="detail-item"><span>Cooldown</span><strong>${formatCooldown(run.cooldown_status, run.cooldown_seconds)}</strong></div>
      <div class="detail-item"><span>Target at start</span><strong>${formatTarget(run.target_temp_f)}</strong></div>
      ${run.kind === 'calibration' ? `
      <div class="detail-item"><span>Still size</span><strong>${run.charge_gallons == null ? '—' : `${run.charge_gallons} gal`}</strong></div>
      <div class="detail-item"><span>Coast</span><strong>${run.coast_f == null ? '—' : `${Number(run.coast_f).toFixed(1)}°F over ${Math.round(Number(run.coast_seconds) || 0)}s`}</strong></div>
      <div class="detail-item"><span>Cooling</span><strong>${run.cooldown_f_per_min == null ? '—' : `${Number(run.cooldown_f_per_min).toFixed(1)}°F/min`}</strong></div>
      ${modelDetail(run)}
      <div class="detail-item"><span>Reached target</span><strong>${run.calibration_settled ? 'Yes' : 'No'}</strong></div>
      ` : ''}
      <div class="detail-item"><span>Heaters</span><strong>${heaterList}</strong></div>
      <div class="detail-item"><span>Sensors</span><strong>${sensorList}</strong></div>
    `

    document.getElementById('sample-head').innerHTML = `
      <th>Elapsed</th>
      <th>Target</th>
      ${sensorSlots.map((slot) => `<th>${escapeHtml(slot.name)}</th>`).join('')}
      ${heaterSlots.map((slot) => `<th>${escapeHtml(slot.name)}</th>`).join('')}
    `
    const readings = run.samples.filter((sample) => sample.phase !== 'cooldown')
    const body = document.getElementById('sample-body')
    if (!readings.length) {
      body.innerHTML = `<tr><td colspan="${2 + sensorSlots.length + heaterSlots.length}" class="empty-cell">No samples recorded.</td></tr>`
    } else {
      const cell = (label, value) => `<td data-label="${escapeHtml(label)}">${value}</td>`
      body.innerHTML = readings
        .map((sample) => {
          const temps = sensorSlots
            .map((slot) => cell(slot.name, formatTemp(sample.temps_f[slot.index])))
            .join('')
          const states = heaterSlots
            .map((slot) => cell(slot.name, sample.heaters_on[slot.index] ? 'On' : 'Off'))
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
    setChartPoints(
      chart,
      readings.map((sample) => ({
        label: formatDuration(sample.elapsed_seconds),
        temps: sensorSlots.map((slot) => sample.temps_f[slot.index]),
        target: sample.target_temp_f,
      })),
      sensorSlots.map((slot) => slot.name),
      markersForRun(readings, run),
    )
  }

  function appendControlLog(logs, heaterSlots, followLive) {
    const body = document.getElementById('control-body')
    const scroller = document.getElementById('control-log')
    if (!body) return
    if (!logs.length) {
      if (!logReady) {
        body.innerHTML = '<tr><td colspan="8" class="empty-cell">No control log for this run.</td></tr>'
        logReady = true
      }
      return
    }
    const nearBottom = scroller
      ? scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48
      : false
    if (!logReady || body.querySelector('.empty-cell')) body.innerHTML = ''
    body.insertAdjacentHTML('beforeend', controlLogRows(logs, heaterSlots))
    lastLogId = logs[logs.length - 1].id
    logReady = true
    if (scroller && (followLive || nearBottom)) scroller.scrollTop = scroller.scrollHeight
  }

  async function refresh() {
    if (stopped) return
    const [run, status, page] = await Promise.all([
      api.run(params.id, { logs: false }),
      api.status(),
      api.runLogs(params.id, lastLogId),
    ])
    setSourceBadge(status.source)
    render(run)
    const open = run.status === 'running' || run.status === 'paused' || run.cooldown_status === 'open'
    appendControlLog(page.logs || [], activeSlots(run, 'heaters'), open && !logReady)
    if (!open) clearInterval(timer)
  }

  const timer = setInterval(() => {
    refresh().catch((error) => {
      errorEl.hidden = false
      errorEl.textContent = error.message
    })
  }, 2000)
  refresh().catch((error) => {
    errorEl.hidden = false
    errorEl.textContent = error.message
  })

  return () => {
    stopped = true
    clearInterval(timer)
    chart.destroy()
  }
}

const EVENT_LABELS = {
  start: 'Start',
  sample: 'Sample',
  heat: 'Heat',
  phase: 'Phase',
  model: 'Model',
  analysis: 'Analysis',
  pause: 'Pause',
  resume: 'Resume',
  stop: 'Stop',
}

const PHASE_LABELS = {
  settle: 'Settle',
  cold_start: 'Cold Start',
  linear: 'Heating',
  coast: 'Coasting',
  momentum: 'Momentum',
  approach: 'Stage II',
  approach_coast: 'Stage II coast',
  pulses: 'Pulses',
  hold: 'Stage III',
  run: 'Run',
  cutoff: 'Run',
  sustain: 'Run',
  model: 'Run',
  cooldown: 'Cooldown',
  pause: 'Paused',
  stop: 'Stopped',
}

function controlLogRows(logs, heaterSlots) {
  const cell = (label, value) => `<td data-label="${escapeHtml(label)}">${value}</td>`
  return logs
    .map((entry) => {
      const detail = entry.detail || {}
      return `
        <tr>
          ${cell('Elapsed', formatDuration(entry.elapsed_seconds))}
          ${cell('Event', escapeHtml(EVENT_LABELS[entry.event] || entry.event || '—'))}
          ${cell('Phase', escapeHtml(phaseLabel(detail)))}
          ${cell('Temp', formatTemp(detail.temp_f))}
          ${cell('Heaters', escapeHtml(heaterText(detail, heaterSlots)))}
          ${cell('Slope', formatSlope(detail))}
          ${cell('Crest', formatTemp(detail.predicted_crest_f))}
          ${cell('Calculation', `<span class="control-note">${escapeHtml(entry.message || '')}</span>`)}
        </tr>`
    })
    .join('')
}

function phaseLabel(detail) {
  const path = detail.path
  if (path && PHASE_LABELS[path]) return PHASE_LABELS[path]
  if (detail.phase && PHASE_LABELS[detail.phase]) return PHASE_LABELS[detail.phase]
  return detail.phase || '—'
}

function formatSlope(detail) {
  const value = detail.live_slope_f_per_sec ?? detail.slope_f_per_sec ?? detail.sheath_slope_f_per_sec
  if (value == null || Number.isNaN(Number(value))) return '—'
  return `${Number(value).toFixed(3)}°F/s`
}

function heaterText(detail, heaterSlots) {
  const states = detail.heaters_on
  if (!Array.isArray(states)) return '—'
  const slots = heaterSlots.length ? heaterSlots : states.map((_, index) => ({ name: `Heater ${index + 1}`, index }))
  const named = slots.map((slot) => `${slot.name} ${states[slot.index] ? 'on' : 'off'}`)
  if (named.every((line) => line.endsWith(' on'))) return 'On'
  if (named.every((line) => line.endsWith(' off'))) return 'Off'
  return named.join(', ')
}

function deviceLabel(slot, kind) {
  const name = escapeHtml(slot.name)
  if (slot.gpio == null) return name
  if (kind === 'sensors' && slot.rtd) {
    const rtd = slot.rtd === 'pt1000' ? 'PT1000' : 'PT100'
    const wires = slot.wires ? ` · ${slot.wires}-wire` : ''
    return `${name} · CS ${slot.gpio} · ${rtd}${wires}`
  }
  return `${name} · GPIO ${slot.gpio}`
}
