import { api } from '../api.js'
import { openPath } from '../router.js'
import { createTempChart, setChartPoints } from '../chart.js'
import {
  activeSlots,
  aboveBoiling,
  escapeHtml,
  formatDuration,
  formatTarget,
  formatWhen,
  statusClass,
  statusLabel,
} from '../format.js'
import { setSourceBadge } from '../layout.js'

const CHART_WINDOW_MS = 2 * 60 * 60 * 1000

function clockLabel(at) {
  return new Date(at).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
}

function runsInWindow(runs, now, cutoff) {
  const visible = []
  for (const run of runs) {
    const start = Date.parse(run.started_at)
    const cooling = run.cooldown_status === 'open'
    const finishedAt = run.cooldown_ended_at
      ? Date.parse(run.cooldown_ended_at)
      : run.ended_at
        ? Date.parse(run.ended_at)
        : now
    const last = cooling || !run.ended_at ? now : finishedAt
    if (last < cutoff) break
    if (start <= now) visible.push(run)
  }
  return visible
}

function markersFor(runs, cutoff, now) {
  const markers = []
  for (const run of runs) {
    const start = Date.parse(run.started_at)
    if (start >= cutoff && start <= now) markers.push({ at: start, kind: 'start' })
    if (!run.ended_at) continue
    const end = Date.parse(run.ended_at)
    if (end >= cutoff && end <= now) markers.push({ at: end, kind: 'stop' })
  }
  return markers
}

function tempsFor(sample, run, sensorNames) {
  const slots = activeSlots(run, 'sensors')
  return sensorNames.map((name) => {
    const slot = slots.find((item) => item.name === name)
    if (!slot) return null
    const value = sample.temps_f[slot.index]
    return value == null || Number.isNaN(Number(value)) ? null : Number(value)
  })
}

export function mountDashboard(root) {
  root.innerHTML = `
    <div class="board">
      <header class="board-head">
        <div>
          <p class="eyebrow">Live still</p>
          <h1 class="board-title">Dashboard</h1>
        </div>
        <div class="board-meta">
          <span class="status-badge status-idle" id="system-mode">Idle</span>
          <div class="cooldown-pill" id="avg-cooldown">
            <span>Avg cooldown</span>
            <strong>—</strong>
          </div>
          <div class="timer-wrap">
            <span class="timer-caption" id="timer-caption"></span>
            <div class="timer-display" id="elapsed">00:00:00</div>
          </div>
        </div>
      </header>
      <section class="probe-grid" id="temp-rows" aria-label="Temperatures"></section>
      <div class="control-row">
        <section class="dashboard-card target-card">
          <label class="eyebrow" for="target-input">Target</label>
          <div class="target-field">
            <input id="target-input" type="number" min="40" max="250" step="1" inputmode="numeric" />
            <div class="target-stepper">
              <button type="button" class="step-btn" data-delta="1" aria-label="Increase target">
                <svg viewBox="0 0 12 8" aria-hidden="true"><path d="M1 6.5 6 1.5l5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
              <button type="button" class="step-btn" data-delta="-1" aria-label="Decrease target">
                <svg viewBox="0 0 12 8" aria-hidden="true"><path d="M1 1.5 6 6.5l5-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </button>
            </div>
            <span>°F</span>
          </div>
          <p class="boil-warning" id="target-warning" hidden>Above water's boiling point (212°F).</p>
          <p class="help">The first enabled probe is the control probe. Heaters turn on 2°F below this target and off when it is reached.</p>
        </section>
        <section class="dashboard-card heater-card">
          <h2 class="card-title">Heaters</h2>
          <div class="heater-grid" id="heater-rows"></div>
          <p class="form-error" id="action-error" hidden></p>
          <div class="status-actions" id="actions"></div>
        </section>
      </div>
      <section class="dashboard-card chart-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Temperature over time</h2>
        </div>
        <div class="chart-container">
          <canvas id="temp-chart"></canvas>
        </div>
      </section>
      <section class="dashboard-card previous-runs-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Previous runs</h2>
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
  `

  const chart = createTempChart(document.getElementById('temp-chart'), { timed: true })
  const points = []
  const seenSamples = new Set()
  const settledRuns = new Set()
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

  const targetInput = document.getElementById('target-input')
  const syncTargetWarning = () => {
    document.getElementById('target-warning').hidden = !aboveBoiling(targetInput.value)
  }
  targetInput.addEventListener('input', syncTargetWarning)
  targetInput.addEventListener('change', async (event) => {
    syncTargetWarning()
    try {
      const target = Math.round(Number(event.target.value))
      event.target.value = String(target)
      await api.saveSettings({ target_temp_f: target })
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    }
  })
  document.querySelector('.target-stepper').addEventListener('click', (event) => {
    const button = event.target.closest('[data-delta]')
    if (!button) return
    const current = Number(targetInput.value)
    if (!Number.isFinite(current)) return
    const next = Math.round(current + Number(button.dataset.delta))
    const min = Number(targetInput.min)
    const max = Number(targetInput.max)
    targetInput.value = String(Math.min(max, Math.max(min, next)))
    targetInput.dispatchEvent(new Event('change'))
  })

  async function onStart() {
    if (busy) return
    busy = true
    try {
      const target = Math.round(Number(document.getElementById('target-input').value))
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
            <article class="heater" data-heater-card="${index}">
              <span class="heater-name">${escapeHtml(name)}</span>
              <span class="heater-state" data-heater="${index}">Off</span>
            </article>`,
        )
        .join('')
    }
    status.heater_names.forEach((_, index) => {
      const on = Boolean(status.heaters_on[index])
      const card = host.querySelector(`[data-heater-card="${index}"]`)
      card.classList.toggle('is-on', on)
      host.querySelector(`[data-heater="${index}"]`).textContent = on ? 'On' : 'Off'
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
            <article class="probe ${index === 0 ? 'probe-primary' : ''}" data-probe="${index}">
              <span class="probe-kicker">${escapeHtml(name)}${index === 0 ? ' · control' : ''}</span>
              <p class="probe-readout" data-temp="${index}"><span class="probe-num">—</span></p>
              <p class="boil-warning" data-boil="${index}" hidden>Above water's boiling point (212°F).</p>
            </article>`,
        )
        .join('')
    }
    status.sensor_names.forEach((_, index) => {
      const value = status.temps_f[index]
      const text = value == null || Number.isNaN(Number(value)) ? '—' : Number(value).toFixed(1)
      host.querySelector(`[data-temp="${index}"]`).innerHTML =
        `<span class="probe-num">${text}</span><span class="probe-unit">°F</span>`
      const hot = aboveBoiling(value)
      host.querySelector(`[data-probe="${index}"]`).classList.toggle('is-boiling', hot)
      host.querySelector(`[data-boil="${index}"]`).hidden = !hot
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
            <td data-label="Date"><a href="/history/${run.id}">${escapeHtml(when.date)}<br><span class="time-text">${escapeHtml(when.time)}</span></a></td>
            <td data-label="Duration">${formatDuration(run.elapsed_seconds)}</td>
            <td data-label="Target">${formatTarget(run.target_temp_f)}</td>
            <td data-label="Status"><span class="status-badge ${statusClass(run.status)}">${statusLabel(run.status)}</span></td>
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
    mode.className = `status-badge ${statusClass(status.mode)}`
    renderHeaters(status)
    renderTemps(status)
    const target = document.getElementById('target-input')
    if (document.activeElement !== target) target.value = String(Math.round(Number(status.target_temp_f)))
    syncTargetWarning()
    if (renderedMode !== status.mode) {
      renderedMode = status.mode
      renderActions(status.mode)
    }

    const history = await api.runs()
    renderRuns(history.runs.slice(0, 8))
    const average = history.stats.average_cooldown_seconds
    document.querySelector('#avg-cooldown strong').textContent =
      average == null ? '—' : formatDuration(average)
    const caption = document.getElementById('timer-caption')
    caption.textContent =
      status.mode === 'cooling' ? 'Cooldown' : status.mode === 'running' || status.mode === 'paused' ? 'Run' : ''

    const now = Date.now()
    const cutoff = now - CHART_WINDOW_MS
    const visible = runsInWindow(history.runs, now, cutoff)
    for (const summary of visible) {
      const live = summary.id === status.run_id || summary.id === status.cooldown_run_id
      if (!live && settledRuns.has(summary.id)) continue
      const run = await api.run(summary.id)
      mergeSamples(run, status.sensor_names)
      const stillOpen = run.status === 'running' || run.status === 'paused' || run.cooldown_status === 'open'
      if (!live && !stillOpen) settledRuns.add(summary.id)
    }
    addPoint({
      at: now,
      label: clockLabel(now),
      temps: status.temps_f.slice(),
      target: status.target_temp_f,
    })
    while (points.length && points[0].at < cutoff) points.shift()
    setChartPoints(chart, points, status.sensor_names, markersFor(visible, cutoff, now))
  }

  function addPoint(point) {
    if (!Number.isFinite(point.at)) return
    let lo = 0
    let hi = points.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (points[mid].at < point.at) lo = mid + 1
      else hi = mid
    }
    const prev = points[lo - 1]
    const next = points[lo]
    if (prev && point.at - prev.at < 750) return
    if (next && next.at - point.at < 750) return
    points.splice(lo, 0, point)
  }

  function mergeSamples(run, sensorNames) {
    for (const sample of run.samples) {
      if (seenSamples.has(sample.id)) continue
      seenSamples.add(sample.id)
      const at = Date.parse(sample.recorded_at)
      addPoint({
        at,
        label: clockLabel(at),
        temps: tempsFor(sample, run, sensorNames),
        target: sample.target_temp_f,
      })
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
