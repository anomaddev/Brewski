import { api } from '../api.js'
import { openPath } from '../router.js'
import { createTempChart, setChartPoints } from '../chart.js'
import {
  escapeHtml,
  formatDuration,
  formatTemp,
  formatWhen,
  statusClass,
  statusLabel,
} from '../format.js'
import { setSourceBadge } from '../layout.js'

export function mountDashboard(root) {
  root.innerHTML = `
    <div class="dashboard-grid">
      <div class="dashboard-column">
        <section class="dashboard-card system-status-card">
          <div class="card-title-with-timer">
            <h2 class="card-title">System Status</h2>
            <div class="timer-display" id="elapsed">00:00:00</div>
          </div>
          <div class="status-details">
            <div class="status-item">
              <span class="status-label">System Status</span>
              <span class="status-value status-idle" id="system-mode">Idle</span>
            </div>
            <div id="heater-rows"></div>
          </div>
          <p class="form-error" id="action-error" hidden></p>
          <div class="status-actions" id="actions"></div>
        </section>
        <section class="dashboard-card current-temps-card">
          <div class="card-title-with-timer">
            <h2 class="card-title">Temperatures</h2>
          </div>
          <div class="status-details">
            <div class="status-item target-item">
              <label class="status-label" for="target-input">Target</label>
              <input id="target-input" type="number" min="40" max="212" step="0.1" inputmode="decimal" />
            </div>
            <div id="temp-rows"></div>
          </div>
          <p class="help">The controller follows the first sensor. Heaters turn on 2°F below the target and off when it is reached.</p>
        </section>
      </div>
      <div class="dashboard-column">
        <section class="dashboard-card chart-card">
          <div class="card-title-with-timer">
            <h2 class="card-title">Temperature Over Time</h2>
          </div>
          <div class="chart-container">
            <canvas id="temp-chart"></canvas>
          </div>
        </section>
        <section class="dashboard-card previous-runs-card">
          <div class="card-title-with-timer">
            <h2 class="card-title">Previous Runs</h2>
            <a class="view-all-btn" href="/history">View all</a>
          </div>
          <div class="runs-table-container">
            <table class="runs-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Duration</th>
                  <th>Target</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody id="previous-runs"></tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  `

  const chart = createTempChart(document.getElementById('temp-chart'))
  const idlePoints = []
  let plottedRunId = null
  let renderedMode = null
  let busy = false
  let stopped = false

  const errorEl = document.getElementById('action-error')
  const showError = (message) => {
    errorEl.hidden = false
    errorEl.textContent = message
  }
  const clearError = () => {
    errorEl.hidden = true
    errorEl.textContent = ''
  }

  document.getElementById('target-input').addEventListener('change', async (event) => {
    try {
      await api.saveSettings({ target_temp_f: Number(event.target.value) })
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    }
  })

  async function onStart() {
    if (busy) return
    busy = true
    try {
      const target = Number(document.getElementById('target-input').value)
      await api.start({ target_temp_f: target })
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    } finally {
      busy = false
    }
  }

  async function onPause() {
    if (busy) return
    busy = true
    try {
      await api.pause()
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    } finally {
      busy = false
    }
  }

  async function onResume() {
    if (busy) return
    busy = true
    try {
      await api.resume()
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    } finally {
      busy = false
    }
  }

  async function onStop() {
    if (busy) return
    if (!window.confirm('Stop this run? Heaters turn off and the run is saved.')) return
    busy = true
    try {
      await api.stop()
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    } finally {
      busy = false
    }
  }

  function renderActions(mode) {
    const host = document.getElementById('actions')
    if (mode === 'running') {
      host.innerHTML = `
        <button class="action-btn pause-btn" type="button" id="pause-btn">Pause</button>
        <button class="action-btn stop-btn" type="button" id="stop-btn">Stop</button>`
    } else if (mode === 'paused') {
      host.innerHTML = `
        <button class="action-btn resume-btn" type="button" id="resume-btn">Resume</button>
        <button class="action-btn stop-btn" type="button" id="stop-btn">Stop</button>`
    } else {
      host.innerHTML = `<button class="action-btn start-btn" type="button" id="start-btn">Start</button>`
    }
    host.querySelector('#start-btn')?.addEventListener('click', onStart)
    host.querySelector('#pause-btn')?.addEventListener('click', onPause)
    host.querySelector('#resume-btn')?.addEventListener('click', onResume)
    host.querySelector('#stop-btn')?.addEventListener('click', onStop)
  }

  function renderHeaters(status) {
    const host = document.getElementById('heater-rows')
    const key = JSON.stringify(status.heater_names)
    if (host.dataset.key !== key) {
      host.dataset.key = key
      host.innerHTML = status.heater_names
        .map(
          (name, index) => `
            <div class="status-item">
              <span class="status-label">${escapeHtml(name)}</span>
              <span class="status-value" data-heater="${index}"></span>
            </div>`,
        )
        .join('')
    }
    status.heater_names.forEach((_, index) => {
      const on = Boolean(status.heaters_on[index])
      const el = host.querySelector(`[data-heater="${index}"]`)
      el.innerHTML = `<span class="status-indicator ${on ? 'indicator-on' : 'indicator-off'}"></span>${on ? 'On' : 'Off'}`
    })
  }

  function renderTemps(status) {
    const host = document.getElementById('temp-rows')
    const key = JSON.stringify(status.sensor_names)
    if (host.dataset.key !== key) {
      host.dataset.key = key
      host.innerHTML = status.sensor_names
        .map(
          (name, index) => `
            <div class="status-item">
              <span class="status-label">${escapeHtml(name)}</span>
              <span class="status-value" data-temp="${index}">—</span>
            </div>`,
        )
        .join('')
    }
    status.sensor_names.forEach((_, index) => {
      host.querySelector(`[data-temp="${index}"]`).textContent = formatTemp(status.temps_f[index])
    })
  }

  function renderRuns(runs) {
    const body = document.getElementById('previous-runs')
    if (!runs.length) {
      body.innerHTML = `<tr><td colspan="4" class="empty-cell">No runs yet. Start one to begin a log.</td></tr>`
      return
    }
    body.innerHTML = runs
      .map((run) => {
        const when = formatWhen(run.started_at)
        return `
          <tr data-href="/history/${run.id}">
            <td><a href="/history/${run.id}">${escapeHtml(when.date)}<br><span class="time-text">${escapeHtml(when.time)}</span></a></td>
            <td>${formatDuration(run.elapsed_seconds)}</td>
            <td>${formatTemp(run.target_temp_f)}</td>
            <td><span class="status-badge ${statusClass(run.status)}">${statusLabel(run.status)}</span></td>
          </tr>`
      })
      .join('')
  }

  document.getElementById('previous-runs').addEventListener('click', (event) => {
    if (event.target.closest('a')) return
    const row = event.target.closest('tr[data-href]')
    if (row) openPath(row.dataset.href)
  })

  async function refresh() {
    if (stopped) return
    const status = await api.status()
    setSourceBadge(status.source)
    document.getElementById('elapsed').textContent = formatDuration(status.elapsed_seconds)
    const mode = document.getElementById('system-mode')
    mode.textContent = statusLabel(status.mode)
    mode.className = `status-value ${statusClass(status.mode)}`
    renderHeaters(status)
    renderTemps(status)
    const target = document.getElementById('target-input')
    if (document.activeElement !== target) target.value = String(status.target_temp_f)
    if (renderedMode !== status.mode) {
      renderedMode = status.mode
      renderActions(status.mode)
    }

    const history = await api.runs()
    renderRuns(history.runs.slice(0, 8))

    if (status.run_id) {
      const run = await api.run(status.run_id)
      plottedRunId = status.run_id
      setChartPoints(
        chart,
        run.samples.map((sample) => ({
          label: formatDuration(sample.elapsed_seconds),
          temps: sample.temps_f,
          target: sample.target_temp_f,
        })),
        run.sensor_names.slice(0, run.sensor_count),
      )
    } else {
      if (plottedRunId !== null) {
        idlePoints.length = 0
        plottedRunId = null
      }
      idlePoints.push({
        label: new Date().toLocaleTimeString('en-US', {
          hour: 'numeric',
          minute: '2-digit',
          second: '2-digit',
        }),
        temps: status.temps_f,
        target: status.target_temp_f,
      })
      if (idlePoints.length > 180) idlePoints.shift()
      setChartPoints(chart, idlePoints, status.sensor_names)
    }
  }

  const timer = setInterval(() => {
    refresh().catch((error) => showError(error.message))
  }, 1000)
  refresh().catch((error) => showError(error.message))

  return () => {
    stopped = true
    clearInterval(timer)
    chart.destroy()
  }
}
