import { api } from '../api.js'
import { createTempChart, setChartPoints } from '../chart.js'
import { escapeHtml, formatDuration, formatTemp, formatWhen, statusClass, statusLabel } from '../format.js'
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
    document.getElementById('run-title').textContent = `Run ${run.id}`
    const badge = document.getElementById('run-status')
    badge.textContent = statusLabel(run.status)
    badge.className = `status-badge ${statusClass(run.status)}`
    const start = formatWhen(run.started_at)
    const end = formatWhen(run.ended_at)
    const heaterList = run.heater_names.slice(0, run.heater_count).map(escapeHtml).join(', ')
    const sensorList = run.sensor_names.slice(0, run.sensor_count).map(escapeHtml).join(', ')
    document.getElementById('run-settings').innerHTML = `
      <div class="detail-item"><span>Started</span><strong>${escapeHtml(start.date)} ${escapeHtml(start.time)}</strong></div>
      <div class="detail-item"><span>Ended</span><strong>${run.ended_at ? `${escapeHtml(end.date)} ${escapeHtml(end.time)}` : '—'}</strong></div>
      <div class="detail-item"><span>Duration</span><strong>${formatDuration(run.elapsed_seconds)}</strong></div>
      <div class="detail-item"><span>Target at start</span><strong>${formatTemp(run.target_temp_f)}</strong></div>
      <div class="detail-item"><span>Heaters</span><strong>${heaterList}</strong></div>
      <div class="detail-item"><span>Sensors</span><strong>${sensorList}</strong></div>
    `

    const sensors = run.sensor_names.slice(0, run.sensor_count)
    const heaters = run.heater_names.slice(0, run.heater_count)
    document.getElementById('sample-head').innerHTML = `
      <th>Elapsed</th>
      <th>Target</th>
      ${sensors.map((name) => `<th>${escapeHtml(name)}</th>`).join('')}
      ${heaters.map((name) => `<th>${escapeHtml(name)}</th>`).join('')}
    `
    const body = document.getElementById('sample-body')
    if (!run.samples.length) {
      body.innerHTML = `<tr><td colspan="${2 + sensors.length + heaters.length}" class="empty-cell">No samples recorded.</td></tr>`
    } else {
      body.innerHTML = run.samples
        .map((sample) => {
          const temps = sensors.map((_, index) => `<td>${formatTemp(sample.temps_f[index])}</td>`).join('')
          const states = heaters
            .map((_, index) => `<td>${sample.heaters_on[index] ? 'On' : 'Off'}</td>`)
            .join('')
          return `
            <tr>
              <td>${formatDuration(sample.elapsed_seconds)}</td>
              <td>${formatTemp(sample.target_temp_f)}</td>
              ${temps}
              ${states}
            </tr>`
        })
        .join('')
    }
    setChartPoints(
      chart,
      run.samples.map((sample) => ({
        label: formatDuration(sample.elapsed_seconds),
        temps: sample.temps_f,
        target: sample.target_temp_f,
      })),
      sensors,
    )
  }

  async function refresh() {
    if (stopped) return
    const [run, status] = await Promise.all([api.run(params.id), api.status()])
    setSourceBadge(status.source)
    render(run)
    if (run.status !== 'running' && run.status !== 'paused') clearInterval(timer)
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
