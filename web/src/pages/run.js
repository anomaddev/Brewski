import { api } from '../api.js'
import { bindChartWindowSelect, createTempChart, setChartPoints, setChartWindow } from '../chart.js'
import { activeSlots, escapeHtml, formatCooldown, formatDuration, formatTarget, formatTemp, formatWhen, programEndLabel, programSummary, runStatusClass, runStatusLabel } from '../format.js'
import { setSourceBadge } from '../layout.js'
import {
  EVENT_FILTERS,
  PAGE_SIZES,
  controlBodyHtml,
  formatClock,
  formatWindow,
  pagerHtml,
  parseClock,
  phaseFilterOptions,
  sampleBodyHtml,
  sampleHeadHtml,
} from '../review.js'

const CHANGE_EVENTS = EVENT_FILTERS.map(([key]) => key).filter((key) => key !== 'sample')

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
  const phaseOptions = phaseFilterOptions()
    .map((option) => `<option value="${escapeHtml(option.key)}">${escapeHtml(option.label)}</option>`)
    .join('')
  const eventChips = EVENT_FILTERS
    .map(([key, label]) => `<button type="button" class="filter-chip" data-event="${key}" aria-pressed="false">${label}</button>`)
    .join('')
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
          <p class="window-caption is-empty" id="window-caption">Choose a page or a custom window</p>
        </div>
        <p class="chart-hint">Drag across the chart to review that stretch.</p>
        <div class="chart-container chart-selectable">
          <canvas id="run-chart"></canvas>
        </div>
      </section>
      <section class="dashboard-card review-card">
        <div class="review-bar">
          <div class="choice-row" role="radiogroup" aria-label="How to choose the window">
            <label><input type="radio" name="review-mode" value="page" checked> Page</label>
            <label><input type="radio" name="review-mode" value="custom"> Custom</label>
          </div>
          <div class="page-sizes" role="group" aria-label="Lines per page">
            ${PAGE_SIZES.map((size) => `<button type="button" class="page-size${size === 25 ? ' is-on' : ''}" data-size="${size}" aria-pressed="${size === 25 ? 'true' : 'false'}">${size}</button>`).join('')}
          </div>
          <div id="log-pager"></div>
          <p class="window-caption is-empty" id="window-label">Choose a page or a custom window</p>
        </div>
        <form id="custom-window" class="custom-window" hidden>
          <label>From <input class="review-time" id="window-from" inputmode="numeric" autocomplete="off" spellcheck="false" aria-label="Window start"></label>
          <label>To <input class="review-time" id="window-to" inputmode="numeric" autocomplete="off" spellcheck="false" aria-label="Window end"></label>
          <label class="review-clock">Clock
            <select id="window-clock" aria-label="Which clock the custom window uses">
              <option value="run">Run</option>
              <option value="cooldown">Cooldown</option>
              <option value="both">Both</option>
            </select>
          </label>
          <button type="submit" class="chart-now">Apply</button>
          <p class="form-error" id="window-error" hidden></p>
        </form>
        <div class="review-log">
          <div class="card-title-with-timer">
            <h2 class="card-title">Control log</h2>
            <a class="export-link" id="export-log" href="/api/runs/${encodeURIComponent(params.id)}/export.zip" download>Export</a>
          </div>
          <div class="log-filters">
            <div class="filter-chips" id="event-filters" role="group" aria-label="Control log events">
              <button type="button" class="filter-chip is-on" data-event="all" aria-pressed="true">All</button>
              <button type="button" class="filter-chip" data-event="changes" aria-pressed="false">Changes</button>
              ${eventChips}
            </div>
            <label class="review-clock">Phase
              <select id="phase-filter" aria-label="Phase">
                <option value="">Any</option>
                ${phaseOptions}
              </select>
            </label>
          </div>
          <div class="history-table-container">
            <table class="history-table review-table">
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
              <tbody id="control-body"><tr><td colspan="8" class="empty-cell">Loading…</td></tr></tbody>
            </table>
          </div>
        </div>
        <div class="review-log">
          <h2 class="card-title">Sample log</h2>
          <p class="form-error" id="run-error" hidden></p>
          <div class="history-table-container">
            <table class="history-table review-table">
              <thead><tr id="sample-head"></tr></thead>
              <tbody id="sample-body"></tbody>
            </table>
          </div>
          <div id="sample-pager"></div>
        </div>
      </section>
    </div>
  `

  const chart = createTempChart(document.getElementById('run-chart'))
  const errorEl = document.getElementById('run-error')
  const view = {
    limit: 25,
    mode: 'page',
    clock: 'run',
    logOffset: 0,
    sampleOffset: 0,
    events: new Set(),
    phase: '',
    from: null,
    to: null,
  }
  const openGroups = new Set()
  const markup = {}
  let stopped = false
  let primed = false
  let follow = false
  let open = false
  let reviewTicket = 0
  let lastChartId = 0
  let chartKey = ''
  let heaterSlots = []
  let sensorSlots = []
  let chartSamples = []
  let lastPage = null
  let lastRun = null
  let logTotal = 0
  let sampleTotal = 0

  function showError(error) {
    errorEl.hidden = false
    errorEl.textContent = error.message
  }

  function setHtml(slot, element, html) {
    if (!element || markup[slot] === html) return
    markup[slot] = html
    element.innerHTML = html
  }

  function paintTables() {
    const logs = lastPage?.logs?.rows || []
    const samples = lastPage?.samples?.rows || []
    setHtml('control', document.getElementById('control-body'), controlBodyHtml(logs, heaterSlots, openGroups))
    setHtml('sample-head', document.getElementById('sample-head'), sampleHeadHtml(sensorSlots, heaterSlots))
    setHtml('sample', document.getElementById('sample-body'), sampleBodyHtml(samples, sensorSlots, heaterSlots))
  }

  function paintChrome() {
    document.querySelectorAll('input[name="review-mode"]').forEach((input) => {
      input.checked = input.value === view.mode
    })
    document.querySelectorAll('.page-size').forEach((button) => {
      const on = Number(button.dataset.size) === view.limit
      button.classList.toggle('is-on', on)
      button.setAttribute('aria-pressed', on ? 'true' : 'false')
    })
    document.getElementById('custom-window').hidden = view.mode !== 'custom'
    const from = document.getElementById('window-from')
    const to = document.getElementById('window-to')
    if (document.activeElement !== from && view.from != null) from.value = formatClock(view.from)
    if (document.activeElement !== to && view.to != null) to.value = formatClock(view.to)
    const bounds = lastPage?.bounds
    if (bounds?.from != null) from.placeholder = formatClock(bounds.from)
    if (bounds?.to != null) to.placeholder = formatClock(bounds.to)
    const clock = document.getElementById('window-clock')
    if (document.activeElement !== clock) clock.value = view.clock
    const phase = document.getElementById('phase-filter')
    if (phase.value !== view.phase) phase.value = view.phase
    document.querySelectorAll('#event-filters .filter-chip').forEach((button) => {
      const name = button.dataset.event
      let on = false
      if (name === 'all') on = view.events.size === 0
      else if (name === 'changes') on = isChanges(view.events)
      else on = view.events.has(name)
      button.classList.toggle('is-on', on)
      button.setAttribute('aria-pressed', on ? 'true' : 'false')
    })
    paintPager(document.getElementById('log-pager'), {
      offset: view.logOffset,
      total: logTotal,
      limit: view.limit,
      noun: 'lines',
      latest: true,
    })
    paintPager(document.getElementById('sample-pager'), {
      offset: view.sampleOffset,
      total: sampleTotal,
      limit: view.limit,
      noun: 'samples',
    })
    const caption = lastPage ? formatWindow(lastPage.window) : 'Choose a page or a custom window'
    const empty = !lastPage?.window || (lastPage.window.from == null && lastPage.window.cooldown_from == null)
    for (const id of ['window-caption', 'window-label']) {
      const node = document.getElementById(id)
      node.textContent = caption
      node.classList.toggle('is-empty', empty)
    }
  }

  function paintPager(element, state) {
    const jump = element.querySelector('[data-jump]')
    const editing = jump && document.activeElement === jump
    const draft = editing ? jump.value : ''
    element.innerHTML = pagerHtml(state)
    if (!editing) return
    const next = element.querySelector('[data-jump]')
    if (!next) return
    next.value = draft
    next.focus()
  }

  function renderMeta(run) {
    document.title = `Brewski — Run ${run.id}`
    const titles = { calibration: 'Calibration', test: 'Test' }
    document.getElementById('run-title').textContent = `${titles[run.kind] || 'Run'} ${run.id}`
    const badge = document.getElementById('run-status')
    badge.textContent = runStatusLabel(run)
    badge.className = `status-badge ${runStatusClass(run)}`
    const start = formatWhen(run.started_at)
    const end = formatWhen(run.ended_at)
    const heaterList = heaterSlots.map((slot) => deviceLabel(slot, 'heaters')).join(', ')
    const sensorList = sensorSlots.map((slot) => deviceLabel(slot, 'sensors')).join(', ')
    document.getElementById('run-settings').innerHTML = `
      <div class="detail-item"><span>Started</span><strong>${escapeHtml(start.date)} ${escapeHtml(start.time)}</strong></div>
      <div class="detail-item"><span>Ended</span><strong>${run.ended_at ? `${escapeHtml(end.date)} ${escapeHtml(end.time)}` : '—'}</strong></div>
      <div class="detail-item"><span>Duration</span><strong>${formatDuration(run.elapsed_seconds)}</strong></div>
      <div class="detail-item"><span>Cooldown</span><strong>${formatCooldown(run.cooldown_status, run.cooldown_seconds)}</strong></div>
      <div class="detail-item"><span>Target at start</span><strong>${formatTarget(run.target_temp_f)}</strong></div>
      ${programSummary(run) ? `<div class="detail-item"><span>Program</span><strong>${escapeHtml(programSummary(run))}</strong></div>` : ''}
      ${programEndLabel(run) ? `<div class="detail-item"><span>Ended by</span><strong>${escapeHtml(programEndLabel(run))}</strong></div>` : ''}
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
  }

  function drawChart() {
    if (!lastRun) return
    const names = sensorSlots.map((slot) => slot.name).join('\0')
    const key = `${lastChartId}:${names}`
    if (key === chartKey && chartSamples.length) return
    chartKey = key
    const points = chartSamples.map((sample) => ({
      label: formatDuration(sample.elapsed_seconds),
      elapsed: Number(sample.elapsed_seconds),
      temps: sensorSlots.map((slot) => sample.temps_f?.[slot.index]),
      target: sample.target_temp_f,
    }))
    setChartPoints(
      chart,
      points,
      sensorSlots.map((slot) => slot.name),
      markersForRun(chartSamples, lastRun),
    )
    const span = lastPage?.window
    if (span?.from != null && span?.to != null) setChartWindow(chart, span)
  }

  function addChartSamples(samples) {
    let added = false
    for (const sample of samples || []) {
      if (sample.id <= lastChartId) continue
      chartSamples.push(sample)
      lastChartId = sample.id
      added = true
    }
    return added
  }

  function reviewQuery(tail) {
    const query = {
      limit: String(view.limit),
      log_offset: String(view.logOffset),
      sample_offset: String(view.sampleOffset),
    }
    if (tail) query.tail = '1'
    if (view.events.size) query.events = [...view.events].join(',')
    if (view.phase) query.phase = view.phase
    if (view.mode === 'custom' && view.from != null && view.to != null) {
      query.from = String(view.from)
      query.to = String(view.to)
      query.clock = view.clock
    }
    return query
  }

  async function loadReview({ tail = false } = {}) {
    const ticket = ++reviewTicket
    const page = await api.review(params.id, reviewQuery(tail))
    if (ticket !== reviewTicket || stopped) return
    errorEl.hidden = true
    lastPage = page
    logTotal = page.logs.total
    sampleTotal = page.samples.total
    view.logOffset = page.logs.offset
    view.sampleOffset = page.samples.offset
    const span = page.window
    setChartWindow(chart, span?.from != null && span?.to != null ? span : null)
    paintTables()
    paintChrome()
  }

  function resetPages() {
    view.logOffset = 0
    view.sampleOffset = 0
    follow = false
  }

  function setEvents(next) {
    const all = EVENT_FILTERS.map(([key]) => key)
    view.events = all.every((key) => next.has(key)) ? new Set() : next
    resetPages()
    paintChrome()
    loadReview().catch(showError)
  }

  function adoptVisibleWindow() {
    const span = lastPage?.window
    const bounds = lastPage?.bounds
    if (span?.from != null && span?.to != null) {
      view.from = span.from
      view.to = span.to
      view.clock = span.cooldown_from != null ? 'both' : 'run'
      return
    }
    if (span?.cooldown_from != null && span?.cooldown_to != null) {
      view.from = span.cooldown_from
      view.to = span.cooldown_to
      view.clock = 'cooldown'
      return
    }
    if (bounds?.from != null && bounds?.to != null) {
      view.from = bounds.from
      view.to = bounds.to
    }
  }

  function enterCustom(span) {
    view.mode = 'custom'
    follow = false
    if (span) {
      view.from = round2(span.from)
      view.to = round2(span.to)
      view.clock = 'run'
      setChartWindow(chart, { from: view.from, to: view.to })
    } else {
      adoptVisibleWindow()
    }
    view.logOffset = 0
    view.sampleOffset = 0
    document.getElementById('window-error').hidden = true
    paintChrome()
    if (view.from == null || view.to == null) return
    loadReview().catch(showError)
  }

  function enterPage() {
    view.mode = 'page'
    view.logOffset = 0
    view.sampleOffset = 0
    follow = open
    paintChrome()
    loadReview({ tail: follow }).catch(showError)
  }

  function movePage(which, move) {
    if (move === 'latest') {
      follow = open && view.mode === 'page' && which === 'logOffset'
      view[which] = 0
      if (which === 'logOffset') view.sampleOffset = 0
      loadReview({ tail: true }).catch(showError)
      return
    }
    const total = which === 'logOffset' ? logTotal : sampleTotal
    const pages = Math.max(1, Math.ceil(total / view.limit) || 1)
    const page = total ? Math.floor(view[which] / view.limit) + 1 : 1
    const next = move === 'next' ? Math.min(pages, page + 1) : Math.max(1, page - 1)
    if (next === page) return
    follow = false
    view[which] = (next - 1) * view.limit
    if (which === 'logOffset') view.sampleOffset = 0
    loadReview().catch(showError)
  }

  function commitJump(which, raw) {
    const page = Number(String(raw).trim())
    if (!Number.isInteger(page) || page < 1) return
    const total = which === 'logOffset' ? logTotal : sampleTotal
    const pages = Math.max(1, Math.ceil(total / view.limit) || 1)
    follow = false
    view[which] = (Math.min(pages, page) - 1) * view.limit
    if (which === 'logOffset') view.sampleOffset = 0
    loadReview().catch(showError)
  }

  function bindPager(id, which) {
    const element = document.getElementById(id)
    element.addEventListener('click', (event) => {
      const button = event.target.closest('[data-move]')
      if (!button || button.disabled) return
      movePage(which, button.dataset.move)
    })
    element.addEventListener('change', (event) => {
      const input = event.target.closest('[data-jump]')
      if (input) commitJump(which, input.value)
    })
    element.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return
      const input = event.target.closest('[data-jump]')
      if (!input) return
      event.preventDefault()
      commitJump(which, input.value)
    })
  }

  document.querySelectorAll('input[name="review-mode"]').forEach((input) => {
    input.addEventListener('change', () => {
      if (!input.checked) return
      if (input.value === 'custom') enterCustom()
      else enterPage()
    })
  })
  document.querySelectorAll('.page-size').forEach((button) => {
    button.addEventListener('click', () => {
      const size = Number(button.dataset.size)
      if (size === view.limit) return
      view.limit = size
      view.logOffset = 0
      view.sampleOffset = 0
      paintChrome()
      loadReview({ tail: follow && view.mode === 'page' }).catch(showError)
    })
  })
  document.getElementById('event-filters').addEventListener('click', (event) => {
    const button = event.target.closest('[data-event]')
    if (!button) return
    const name = button.dataset.event
    if (name === 'all') setEvents(new Set())
    else if (name === 'changes') setEvents(new Set(CHANGE_EVENTS))
    else {
      const next = new Set(view.events)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      setEvents(next)
    }
  })
  document.getElementById('phase-filter').addEventListener('change', (event) => {
    view.phase = event.target.value
    resetPages()
    paintChrome()
    loadReview().catch(showError)
  })
  document.getElementById('custom-window').addEventListener('submit', (event) => {
    event.preventDefault()
    const from = parseClock(document.getElementById('window-from').value)
    const to = parseClock(document.getElementById('window-to').value)
    const error = document.getElementById('window-error')
    if (from == null || to == null || from < 0 || to < from) {
      error.hidden = false
      error.textContent = 'Enter a start and end, as 00:10:00.'
      return
    }
    error.hidden = true
    view.mode = 'custom'
    view.clock = document.getElementById('window-clock').value
    view.from = round2(from)
    view.to = round2(to)
    follow = false
    view.logOffset = 0
    view.sampleOffset = 0
    setChartWindow(chart, view.clock === 'cooldown' ? null : { from: view.from, to: view.to })
    loadReview().catch(showError)
  })
  document.getElementById('control-body').addEventListener('click', (event) => {
    const row = event.target.closest('tr[data-group]')
    if (!row) return
    const id = row.dataset.group
    if (openGroups.has(id)) openGroups.delete(id)
    else openGroups.add(id)
    markup.control = ''
    paintTables()
  })
  bindPager('log-pager', 'logOffset')
  bindPager('sample-pager', 'sampleOffset')
  const unbindSelect = bindChartWindowSelect(chart, (span) => enterCustom(span))
  paintChrome()

  async function refresh() {
    if (stopped) return
    const [run, chartPage, status] = await Promise.all([
      api.run(params.id, { logs: false, samples: false }),
      api.chart(params.id, lastChartId),
      api.status(),
    ])
    if (stopped) return
    errorEl.hidden = true
    lastRun = run
    open = run.status === 'running' || run.status === 'paused' || run.cooldown_status === 'open'
    heaterSlots = activeSlots(run, 'heaters')
    sensorSlots = activeSlots(run, 'sensors')
    setSourceBadge(status.source)
    renderMeta(run)
    if (lastPage) paintTables()
    if (addChartSamples(chartPage.samples) || !chartKey) drawChart()
    if (!primed) {
      follow = open
      await loadReview({ tail: follow })
      primed = true
    } else if (follow && view.mode === 'page') {
      await loadReview({ tail: true })
    }
    if (!open) clearInterval(timer)
  }

  const timer = setInterval(() => {
    refresh().catch(showError)
  }, 2000)
  refresh().catch(showError)

  return () => {
    stopped = true
    clearInterval(timer)
    unbindSelect()
    chart.destroy()
  }
}

function isChanges(events) {
  if (events.size !== CHANGE_EVENTS.length) return false
  return CHANGE_EVENTS.every((key) => events.has(key))
}

function round2(value) {
  return Math.round(Number(value) * 100) / 100
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
