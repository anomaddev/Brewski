import { api } from '../api.js'
import { openPath } from '../router.js'
import { createTempChart, setChartPoints } from '../chart.js'
import {
  CONDENSE_AFTER_MS,
  DEFAULT_SPAN_INDEX,
  GAP_AFTER_MS,
  TIMELINE_SPANS,
  bindTimelinePan,
  clampEnd,
  formatOff,
  mergeGaps,
  toDisplay,
  toReal,
} from '../timeline.js'
import {
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

function forecastText(status) {
  const parts = []
  if (status.seconds_to_target != null) {
    parts.push(`About ${formatDuration(status.seconds_to_target)} to the target.`)
  }
  if (status.seconds_to_cooldown != null) {
    parts.push(`About ${formatDuration(status.seconds_to_cooldown)} to cool.`)
  }
  return parts.join(' ')
}

function coastText(status) {
  const eta = forecastText(status)
  const withEta = (text) => (eta ? `${text} ${eta}` : text)
  if (status.run_kind === 'calibration' && (status.mode === 'running' || status.mode === 'paused')) {
    const crest =
      status.predicted_crest_f == null ? '' : ` Predicted crest ${Number(status.predicted_crest_f).toFixed(1)}°F.`
    const steps = {
      settle: 'Settling with the elements off.',
      cold_start: 'Cold start. The temperature is still bending upward.',
      linear: 'Heating in a straight line.',
      coast: 'Elements are off. The climb is coasting on a downward curve.',
      momentum: 'Peak saved. Measuring how fast the pot cools.',
      approach: 'Heading for about 140°F so the crest can land under 150°F.',
      pulses: 'Short pulses, each one predicted to crest at or under 150°F.',
      hold: 'Holding 150°F.',
    }
    const line = steps[status.calibration_step] || 'Calibrating toward 150°F.'
    return withEta(`${line}${crest}`)
  }
  if (status.learned_coast_f == null) {
    return withEta('Calibrate with water in the pot. Runs stay locked until that measurement is saved.')
  }
  const learned = `${Number(status.learned_coast_f).toFixed(1)}°F`
  const gallons = status.charge_gallons == null ? '' : ` at ${Number(status.charge_gallons)} gal`
  if (status.learned_cooldown_f_per_min == null) {
    return withEta(`Learned coast ${learned}${gallons}. Another calibration fine-tunes this model.`)
  }
  const rate = `${Number(status.learned_cooldown_f_per_min).toFixed(1)}°F/min`
  const count = Number(status.calibration_count)
  const tuned = count > 1 ? ` Tuned across ${count} calibrations.` : ''
  return withEta(
    `Learned coast ${learned}${gallons}. Cooling is ${rate}.${tuned} Another calibration fine-tunes this model.`,
  )
}

function runBadge(run) {
  const status = `<span class="status-badge ${statusClass(run.status)}">${statusLabel(run.status)}</span>`
  if (run.kind !== 'calibration') return status
  return `<span class="status-badge status-calibration">Calibration</span> ${status}`
}

function runsInWindow(runs, windowStart, windowEnd) {
  const visible = []
  const wall = Date.now()
  for (const run of runs) {
    const start = Date.parse(run.started_at)
    if (!Number.isFinite(start)) continue
    const cooling = run.cooldown_status === 'open'
    const finishedAt = run.cooldown_ended_at
      ? Date.parse(run.cooldown_ended_at)
      : run.ended_at
        ? Date.parse(run.ended_at)
        : wall
    const last = cooling || !run.ended_at ? wall : finishedAt
    if (last < windowStart) break
    if (start <= windowEnd) visible.push(run)
  }
  return visible
}

function markersFor(runs, windowStart, windowEnd) {
  const markers = []
  for (const run of runs) {
    const start = Date.parse(run.started_at)
    if (start >= windowStart && start <= windowEnd) markers.push({ at: start, kind: 'start', label: 'Start' })
    const named = Array.isArray(run.marks) ? run.marks : []
    const hasCooldown = named.some((mark) => mark.name === 'Cooldown')
    for (const mark of named) {
      if (mark.name === 'Start') continue
      const at = start + Number(mark.elapsed_seconds) * 1000
      if (at >= windowStart && at <= windowEnd) markers.push({ at, kind: 'mark', label: mark.name })
    }
    if (!run.ended_at || hasCooldown) continue
    const end = Date.parse(run.ended_at)
    if (end >= windowStart && end <= windowEnd) markers.push({ at: end, kind: 'stop', label: 'Stop' })
  }
  return markers
}

export function mountDashboard(root) {
  root.innerHTML = `
    <div class="board">
      <p class="emergency-banner" id="emergency-banner" hidden></p>
      <div class="stage" id="stage" data-probes="1" data-heaters="2" data-cabinet="on">
      <section class="probe-grid" id="temp-rows" aria-label="Temperatures"></section>
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
        <section class="dashboard-card plant-card" id="plant-card" aria-label="Cabinet and heaters">
          <div class="plant-body">
            <div class="cabinet-block" id="cabinet-block">
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
            </div>
            <div class="heater-band" aria-label="Heaters">
              <span class="eyebrow plant-heaters-label">Heaters</span>
              <div class="heater-grid" id="heater-rows"></div>
            </div>
          </div>
        </section>
      </div>
      <section class="dashboard-card chart-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Temperature over time</h2>
          <div class="chart-toolbar" role="group" aria-label="Chart time range">
            <button type="button" class="chart-tool" id="chart-earlier" aria-label="Earlier">‹</button>
            <button type="button" class="chart-tool" id="chart-later" aria-label="Later">›</button>
            <button type="button" class="chart-tool" id="chart-shorter" aria-label="Show a shorter time span">−</button>
            <span class="chart-span" id="chart-span">20 min</span>
            <button type="button" class="chart-tool" id="chart-longer" aria-label="Show a longer time span">+</button>
            <button type="button" class="chart-now" id="chart-now" hidden>Now</button>
          </div>
        </div>
        <div class="chart-container chart-timeline">
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
  let spanIndex = DEFAULT_SPAN_INDEX
  let follow = true
  let viewEndReal = Date.now()
  let listedRuns = []
  let lastStatus = null
  let timelineMeta = { earliest: null, latest: null, gaps: [] }
  let timelineCache = null
  let loadToken = 0
  let loadTimer = null
  let lastTimelineFetch = 0
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

  async function onCleanCalibration() {
    if (busy) return
    const confirmed = window.confirm(
      'Clear the learned calibration? The heating model is erased and the next calibration starts from scratch. Past runs stay in the history.',
    )
    if (!confirmed) return
    busy = true
    try {
      await api.cleanCalibration()
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
      const body = { kind: 'calibration' }
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
        <button class="action-btn calibrate-btn" type="button" id="calibrate-btn">Calibrate</button>
        <button class="action-btn clean-btn" type="button" id="clean-btn">Clean Calibration</button>`
    } else {
      host.innerHTML = `
        <button class="action-btn start-btn" type="button" id="calibrate-btn">Calibrate</button>`
    }
    host.querySelector('#start-btn')?.addEventListener('click', onStart)
    host.querySelector('#calibrate-btn')?.addEventListener('click', onCalibrate)
    host.querySelector('#clean-btn')?.addEventListener('click', onCleanCalibration)
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
    setAppVersion(status)
    document.getElementById('elapsed').textContent = formatDuration(status.elapsed_seconds)
    const mode = document.getElementById('system-mode')
    mode.textContent = statusLabel(status.mode)
    mode.className = `status-badge ${statusClass(status.mode)}`
    renderHeaters(status)
    renderTemps(status)
    renderCabinet(status)
    const stage = document.getElementById('stage')
    const cabinetOn = status.cabinet_enabled !== false
    stage.dataset.probes = String(Math.max(1, status.sensor_names.length))
    stage.dataset.heaters = String(Math.max(1, status.heater_names.length))
    stage.dataset.cabinet = cabinetOn ? 'on' : 'off'
    document.getElementById('cabinet-block').hidden = !cabinetOn
    document.getElementById('plant-card').setAttribute(
      'aria-label',
      cabinetOn ? 'Cabinet and heaters' : 'Heaters',
    )
    const target = document.getElementById('target-input')
    // Status reports 150°F while calibrating. That is the aim, not the setpoint.
    if (status.run_kind !== 'calibration' && document.activeElement !== target) {
      target.value = String(Math.round(Number(status.target_temp_f)))
    }
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

    listedRuns = history.runs
    lastStatus = status
    paintChart()
    const refreshMs = TIMELINE_SPANS[spanIndex].ms > 6 * 60 * 60 * 1000 ? 30000 : 5000
    if (follow && Date.now() - lastTimelineFetch > refreshMs) scheduleTimeline()
  }

  function axisGaps(now) {
    const gaps = (timelineMeta.gaps || []).map((gap) => ({ from: gap.from, to: gap.to }))
    if (timelineMeta.latest != null && now - timelineMeta.latest > GAP_AFTER_MS) {
      gaps.push({ from: timelineMeta.latest, to: now })
    }
    return mergeGaps(gaps)
  }

  function neededRange(now) {
    const span = TIMELINE_SPANS[spanIndex].ms
    const gaps = axisGaps(now)
    const end = follow ? now : viewEndReal
    const endDisplay = toDisplay(end, gaps)
    const earliest = timelineMeta.earliest
    const lo = earliest == null ? end - span * 4 : Math.min(earliest, end)
    const from = toReal(endDisplay - span * 2, gaps, lo - span, now)
    const to = Math.min(now, toReal(endDisplay + span, gaps, lo, now))
    return { from, to, span }
  }

  function cacheCovers(range) {
    if (!timelineCache) return false
    if (timelineCache.from > range.from + 500) return false
    if (timelineCache.to < range.to - 500) return false
    if (timelineCache.span > range.span) return false
    return true
  }

  function scheduleTimeline() {
    clearTimeout(loadTimer)
    loadTimer = setTimeout(() => {
      loadTimeline().catch((error) => showError(error.message))
    }, 160)
  }

  async function loadTimeline() {
    if (stopped) return
    const token = ++loadToken
    const first = neededRange(Date.now())
    const cacheFresh = timelineCache && Date.now() - timelineCache.fetchedAt < 5000
    const coversLiveEdge = first.to >= Date.now() - 10000
    if (cacheCovers(first) && (!coversLiveEdge || cacheFresh)) {
      lastTimelineFetch = Date.now()
      return
    }
    let from = first.from
    let to = Math.max(first.to, from + 1)
    let data = await api.timeline(Math.floor(from), Math.ceil(to))
    if (token !== loadToken || stopped) return
    timelineMeta = {
      earliest: data.earliest,
      latest: data.latest,
      gaps: data.gaps || [],
    }
    const again = neededRange(Date.now())
    if (again.from < from - 1000) {
      from = again.from
      to = Math.max(again.to, from + 1)
      data = await api.timeline(Math.floor(from), Math.ceil(to))
      if (token !== loadToken || stopped) return
      timelineMeta = {
        earliest: data.earliest,
        latest: data.latest,
        gaps: data.gaps || [],
      }
    }
    timelineCache = {
      from,
      to,
      span: TIMELINE_SPANS[spanIndex].ms,
      decimated: Boolean(data.decimated),
      points: data.points || [],
      fetchedAt: Date.now(),
    }
    lastTimelineFetch = Date.now()
    paintChart()
  }

  function moveDisplay(displayDelta) {
    const now = Date.now()
    if (follow) viewEndReal = now
    const gaps = axisGaps(now)
    const span = TIMELINE_SPANS[spanIndex].ms
    const nextDisplay = toDisplay(viewEndReal, gaps) + displayDelta
    const lo = timelineMeta.earliest == null ? now - span : Math.min(timelineMeta.earliest, now)
    let nextReal = toReal(nextDisplay, gaps, lo - span, now)
    nextReal = clampEnd(nextReal, span, gaps, timelineMeta.earliest, now)
    if (now - nextReal <= 1500) {
      follow = true
      viewEndReal = now
    } else {
      follow = false
      viewEndReal = nextReal
    }
    paintChart()
    scheduleTimeline()
  }

  function setSpan(index) {
    spanIndex = Math.max(0, Math.min(TIMELINE_SPANS.length - 1, index))
    paintChart()
    scheduleTimeline()
  }

  function paintChart() {
    if (!lastStatus) return
    const now = Date.now()
    const span = TIMELINE_SPANS[spanIndex].ms
    if (follow) viewEndReal = now
    const gaps = axisGaps(now)
    viewEndReal = clampEnd(viewEndReal, span, gaps, timelineMeta.earliest, now)
    const endDisplay = toDisplay(viewEndReal, gaps)
    const startDisplay = endDisplay - span
    const lo = timelineMeta.earliest == null ? viewEndReal - span : Math.min(timelineMeta.earliest, viewEndReal)
    const startReal = toReal(startDisplay, gaps, lo - span, viewEndReal)
    const points = []
    for (const point of timelineCache?.points || []) {
      const at = toDisplay(point.at, gaps)
      if (at < startDisplay - 1000 || at > endDisplay + 1000) continue
      points.push({
        at,
        temps: point.temps_f,
        target: point.target_temp_f,
      })
    }
    if (viewEndReal >= now - 2000) {
      const liveAt = toDisplay(now, gaps)
      const previous = points[points.length - 1]
      if (!previous || liveAt > previous.at + 750) {
        points.push({
          at: liveAt,
          temps: lastStatus.temps_f.slice(),
          target: lastStatus.target_temp_f,
        })
      }
    }
    points.sort((left, right) => left.at - right.at)
    const offSpans = gaps
      .filter((gap) => gap.to > startReal && gap.from < viewEndReal && gap.to - gap.from > GAP_AFTER_MS)
      .map((gap) => ({
        from: toDisplay(Math.max(gap.from, startReal), gaps),
        to: toDisplay(Math.min(gap.to, viewEndReal), gaps),
        label: gap.to - gap.from > CONDENSE_AFTER_MS ? formatOff(gap.to - gap.from) : 'Off',
      }))
    const markers = markersFor(runsInWindow(listedRuns, startReal, viewEndReal), startReal, viewEndReal).map(
      (marker) => ({ ...marker, at: toDisplay(marker.at, gaps) }),
    )
    setChartPoints(chart, points, lastStatus.sensor_names, markers, {
      min: startDisplay,
      max: endDisplay,
      spans: offSpans,
      spanMs: span,
      connect: true,
      toReal: (display) => toReal(display, gaps, lo - span, now),
      hideTick: (real) =>
        gaps.some((gap) => gap.to - gap.from > CONDENSE_AFTER_MS && real > gap.from && real < gap.to),
    })
    const spanLabel = document.getElementById('chart-span')
    if (spanLabel) spanLabel.textContent = TIMELINE_SPANS[spanIndex].label
    const nowButton = document.getElementById('chart-now')
    if (nowButton) nowButton.hidden = follow
    const later = document.getElementById('chart-later')
    if (later) later.disabled = follow
    const shorter = document.getElementById('chart-shorter')
    const longer = document.getElementById('chart-longer')
    if (shorter) shorter.disabled = spanIndex === 0
    if (longer) longer.disabled = spanIndex === TIMELINE_SPANS.length - 1
  }

  document.getElementById('chart-earlier').addEventListener('click', () => {
    moveDisplay(-TIMELINE_SPANS[spanIndex].ms / 2)
  })
  document.getElementById('chart-later').addEventListener('click', () => {
    moveDisplay(TIMELINE_SPANS[spanIndex].ms / 2)
  })
  document.getElementById('chart-shorter').addEventListener('click', () => setSpan(spanIndex - 1))
  document.getElementById('chart-longer').addEventListener('click', () => setSpan(spanIndex + 1))
  document.getElementById('chart-now').addEventListener('click', () => {
    follow = true
    viewEndReal = Date.now()
    paintChart()
    scheduleTimeline()
  })
  const unbindPan = bindTimelinePan(chart.canvas, (pixels) => {
    const area = chart.chartArea
    const width = area ? area.right - area.left : chart.canvas.clientWidth
    if (!width) return
    moveDisplay((-pixels / width) * TIMELINE_SPANS[spanIndex].ms)
  })

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
    clearTimeout(loadTimer)
    window.removeEventListener('brewski-refresh', onSetup)
    unbindPan()
    chart.destroy()
  }
}
