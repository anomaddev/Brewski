import { api } from '../api.js'
import { openPath } from '../router.js'
import { createTempChart, setChartPoints } from '../chart.js'
import {
  activeSlots,
  aboveBoiling,
  escapeHtml,
  formatDuration,
  formatHours,
  formatTarget,
  formatWhen,
  statusClass,
  statusLabel,
} from '../format.js'
import { setAppVersion, setSourceBadge } from '../layout.js'

const CHART_WINDOW_MS = 2 * 60 * 60 * 1000

function coastText(status) {
  const cutoff = Number(status.early_cutoff_f)
  const cutoffLabel = Number.isFinite(cutoff) ? `${cutoff}°F` : '—'
  if (status.run_kind === 'calibration' && (status.mode === 'running' || status.mode === 'paused')) {
    if (status.calibration_step === 'coasting') {
      return 'Elements are off. Watching the climb until it crests.'
    }
    if (status.calibration_step === 'falling') {
      return 'Peak saved. Measuring how fast the pot cools.'
    }
    return 'Heating a little past the target. Then Brewski watches the climb to its peak and how fast the pot cools.'
  }
  if (status.learned_coast_f == null) {
    return 'Calibrate with water in the pot. Runs stay locked until that measurement is saved.'
  }
  const learned = `${Number(status.learned_coast_f).toFixed(1)}°F`
  const gallons = status.charge_gallons == null ? '' : ` at ${Number(status.charge_gallons)} gal`
  if (status.learned_cooldown_f_per_min == null) {
    return `Learned coast ${learned}${gallons}. Cutoff is ${cutoffLabel}. Calibrate again to measure how fast the pot cools.`
  }
  const rate = `${Number(status.learned_cooldown_f_per_min).toFixed(1)}°F/min`
  return `Learned coast ${learned}${gallons}. Cutoff is ${cutoffLabel}. Cooling is ${rate}, so the element pulses back on about 1°F under the target.`
}

function runBadge(run) {
  const status = `<span class="status-badge ${statusClass(run.status)}">${statusLabel(run.status)}</span>`
  if (run.kind !== 'calibration') return status
  return `<span class="status-badge status-calibration">Calibration</span> ${status}`
}

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
      <section class="probe-grid" id="temp-rows" aria-label="Temperatures"></section>
      <section class="dashboard-card cabinet-card" id="cabinet-card" aria-label="Cabinet safety">
        <p class="emergency-banner" id="emergency-banner" hidden></p>
        <div class="cabinet-readout">
          <div>
            <span class="eyebrow">Cabinet</span>
            <p class="cabinet-value" id="cabinet-temp">—</p>
          </div>
          <div>
            <span class="eyebrow">Humidity</span>
            <p class="cabinet-value" id="cabinet-humidity">—</p>
          </div>
          <div>
            <span class="eyebrow">Limit</span>
            <p class="cabinet-value" id="cabinet-limit">—</p>
          </div>
        </div>
      </section>
      <section class="dashboard-card console">
        <div class="console-main">
          <div class="run-identity">
            <span class="status-badge status-idle" id="system-mode">Idle</span>
            <span class="status-badge status-calibration" id="run-kind" hidden>Calibration</span>
            <div class="timer-wrap">
              <span class="timer-caption" id="timer-caption"></span>
              <div class="timer-display" id="elapsed">00:00:00</div>
            </div>
          </div>
          <p class="coast-readout" id="coast-readout"></p>
          <div class="console-launch">
            <div class="status-actions" id="actions"></div>
            <div class="run-setup" id="run-setup">
              <label class="gallons-field" for="charge-gallons" title="The still size from setup. Change it when this calibration is for a different size.">
                <span>Still size</span>
                <span class="gallons-input">
                  <input id="charge-gallons" type="number" min="0.25" max="100" step="0.25" inputmode="decimal" placeholder="—" />
                  <span>gal</span>
                </span>
              </label>
            </div>
          </div>
          <p class="form-error" id="action-error" hidden></p>
        </div>
        <div class="console-target target-card">
          <label class="eyebrow" for="target-input">Target</label>
          <div class="target-field">
            <input id="target-input" type="number" min="90" max="240" step="1" inputmode="numeric" />
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
        </div>
      </section>
      <section class="heater-band" aria-label="Heaters">
        <div class="heater-grid" id="heater-rows"></div>
      </section>
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
          <div class="run-meters">
            <div class="cooldown-pill" id="avg-cooldown">
              <span>Avg cooldown</span>
              <strong>—</strong>
            </div>
            <a class="view-all-btn" href="/history">View all</a>
          </div>
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
  let calibrated = false
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

  function targetValue() {
    return Math.round(Number(document.getElementById('target-input').value))
  }

  async function onStart() {
    if (busy) return
    busy = true
    try {
      await api.start({ target_temp_f: targetValue() })
      clearError()
      await refresh()
    } catch (error) {
      showError(error.message)
    } finally {
      busy = false
    }
  }

  async function onCalibrate() {
    if (busy) return
    busy = true
    try {
      const body = { target_temp_f: targetValue(), kind: 'calibration' }
      const raw = document.getElementById('charge-gallons').value.trim()
      if (raw !== '') body.charge_gallons = Number(raw)
      await api.start(body)
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

  function renderCabinet(status) {
    const temp = status.cabinet_temp_f
    const humidity = status.cabinet_humidity_pct
    document.getElementById('cabinet-temp').textContent =
      temp == null || Number.isNaN(Number(temp)) ? '—' : `${Number(temp).toFixed(1)}°F`
    document.getElementById('cabinet-humidity').textContent =
      humidity == null || Number.isNaN(Number(humidity)) ? '—' : `${Math.round(Number(humidity))}%`
    const limit = status.cabinet_limit_f
    document.getElementById('cabinet-limit').textContent =
      limit == null || Number.isNaN(Number(limit)) ? '—' : `${Math.round(Number(limit))}°F`
    const banner = document.getElementById('emergency-banner')
    const codes = status.emergency || []
    banner.hidden = codes.length === 0
    banner.textContent = codes.map((item) => `${item.code}: ${item.summary}`).join(' ')
  }

  function renderActions(mode, ready, emergency) {
    const host = document.getElementById('actions')
    if (emergency) {
      host.innerHTML = ''
      return
    }
    if (mode === 'running') {
      host.innerHTML = `
        <button class="action-btn pause-btn" type="button" id="pause-btn">Pause</button>
        <button class="action-btn stop-btn" type="button" id="stop-btn">Stop</button>`
    } else if (mode === 'paused') {
      host.innerHTML = `
        <button class="action-btn resume-btn" type="button" id="resume-btn">Resume</button>
        <button class="action-btn stop-btn" type="button" id="stop-btn">Stop</button>`
    } else if (ready) {
      host.innerHTML = `
        <button class="action-btn start-btn" type="button" id="start-btn">Start</button>
        <button class="action-btn calibrate-btn" type="button" id="calibrate-btn">Calibrate</button>`
    } else {
      host.innerHTML = `
        <button class="action-btn start-btn" type="button" id="calibrate-btn">Calibrate</button>`
    }
    host.querySelector('#start-btn')?.addEventListener('click', onStart)
    host.querySelector('#calibrate-btn')?.addEventListener('click', onCalibrate)
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
              <span class="heater-lamp" aria-hidden="true"></span>
              <span class="heater-copy">
                <span class="heater-name">${escapeHtml(name)}</span>
                <span class="heater-life" data-heater-life="${index}">0.00 h</span>
              </span>
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
      const slot = (status.heater_indexes || [])[index] ?? index
      const seconds = (status.heater_life_seconds || [])[slot] || 0
      const life = host.querySelector(`[data-heater-life="${index}"]`)
      life.textContent = formatHours(seconds)
      life.title = `${formatDuration(seconds)} powered, including calibration`
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
              <p class="probe-delta" data-delta="${index}"></p>
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
      const delta = host.querySelector(`[data-delta="${index}"]`)
      const target = Number(status.target_temp_f)
      const gap = Number(value) - target
      if (index === 0 && text !== '—' && Number.isFinite(gap)) {
        delta.textContent =
          Math.abs(gap) < 0.05 ? 'On target' : gap < 0 ? `${Math.abs(gap).toFixed(1)}° below target` : `${gap.toFixed(1)}° above target`
      } else {
        delta.textContent = ''
      }
      const hot = aboveBoiling(value)
      host.querySelector(`[data-probe="${index}"]`).classList.toggle('is-boiling', hot)
      host.querySelector(`[data-boil="${index}"]`).hidden = !hot
    })
  }

  function renderRuns(runs) {
    const body = document.getElementById('previous-runs')
    if (!runs.length) {
      const message = calibrated
        ? 'No runs yet. Start one to begin a log.'
        : 'No runs yet. Calibrate with water in the pot to begin.'
      body.innerHTML = `<tr><td colspan="4" class="empty-cell">${message}</td></tr>`
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
            <td data-label="Status">${runBadge(run)}</td>
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
    setAppVersion(status.version)
    document.getElementById('elapsed').textContent = formatDuration(status.elapsed_seconds)
    const mode = document.getElementById('system-mode')
    mode.textContent = statusLabel(status.mode)
    mode.className = `status-badge ${statusClass(status.mode)}`
    renderHeaters(status)
    renderTemps(status)
    renderCabinet(status)
    const target = document.getElementById('target-input')
    if (document.activeElement !== target) target.value = String(Math.round(Number(status.target_temp_f)))
    syncTargetWarning()
    calibrated = status.learned_coast_f != null
    const emergency = (status.emergency || []).map((item) => item.code).join(',')
    const actionKey = `${status.mode}:${calibrated ? 'ready' : 'raw'}:${emergency}`
    if (renderedMode !== actionKey) {
      renderedMode = actionKey
      renderActions(status.mode, calibrated, Boolean(emergency))
    }
    const charge = document.getElementById('charge-gallons')
    if (!charge.dataset.ready && status.charge_gallons != null && document.activeElement !== charge) {
      charge.value = String(status.charge_gallons)
      charge.dataset.ready = '1'
    }
    const calibrating = status.run_kind === 'calibration'
    document.getElementById('run-kind').hidden = !calibrating
    document.getElementById('run-setup').hidden = status.mode === 'running' || status.mode === 'paused'
    document.getElementById('coast-readout').textContent = coastText(status)

    const history = await api.runs()
    renderRuns(history.runs.slice(0, 8))
    const average = history.stats.average_cooldown_seconds
    document.querySelector('#avg-cooldown strong').textContent =
      average == null ? '—' : formatDuration(average)
    const caption = document.getElementById('timer-caption')
    caption.textContent =
      status.mode === 'cooling'
        ? 'Cooldown'
        : status.mode === 'running' || status.mode === 'paused'
          ? status.run_kind === 'calibration'
            ? 'Calibration'
            : 'Run'
          : ''

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
  const onSetup = () => {
    refresh().catch((error) => showError(error.message))
  }
  window.addEventListener('brewski-refresh', onSetup)
  refresh().catch((error) => showError(error.message))

  return () => {
    stopped = true
    clearInterval(timer)
    window.removeEventListener('brewski-refresh', onSetup)
    chart.destroy()
  }
}
