import { api } from '../api.js'
import { createTempChart, setChartPoints } from '../chart.js'
import { activeSlots, escapeHtml, formatCooldown, formatDuration, formatTarget, formatTemp, formatWhen, statusClass, statusLabel } from '../format.js'
import { setSourceBadge } from '../layout.js'

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
  const errorEl = document.getElementById('run-error')

  function render(run) {
    document.title = `Brewski — Run ${run.id}`
    document.getElementById('run-title').textContent =
      run.kind === 'calibration' ? `Calibration ${run.id}` : `Run ${run.id}`
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
    )
  }

  async function refresh() {
    if (stopped) return
    const [run, status] = await Promise.all([api.run(params.id), api.status()])
    setSourceBadge(status.source)
    render(run)
    const open = run.status === 'running' || run.status === 'paused' || run.cooldown_status === 'open'
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
