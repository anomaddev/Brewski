import { api } from '../api.js'
import { openPath } from '../router.js'
import { escapeHtml, formatCooldown, formatDuration, formatHours, formatTarget, formatWhen, kindBadge, statusClass, statusLabel } from '../format.js'
import { setSourceBadge } from '../layout.js'

export function mountHistory(root) {
  root.innerHTML = `
    <div class="history-page">
      <section class="dashboard-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Overview</h2>
        </div>
        <div class="overview-stats" id="overview-stats"></div>
      </section>
      <section class="dashboard-card">
        <div class="card-title-with-timer">
          <h2 class="card-title">Run History</h2>
        </div>
        <p class="form-error" id="history-error" hidden></p>
        <div class="history-table-container">
          <table class="history-table">
            <thead>
              <tr id="history-head"></tr>
            </thead>
            <tbody id="history-body"></tbody>
          </table>
        </div>
      </section>
    </div>
  `

  let stopped = false
  const errorEl = document.getElementById('history-error')

  async function refresh() {
    if (stopped) return
    const [settings, history, status] = await Promise.all([api.settings(), api.runs(), api.status()])
    setSourceBadge(status.source)
    const heaters = visibleHeaters(settings, history.stats)
    document.getElementById('overview-stats').innerHTML = `
      <div class="overview-stat">
        <div class="stat-label">Total Runs</div>
        <div class="stat-value">${history.stats.total_runs}</div>
      </div>
      <div class="overview-stat">
        <div class="stat-label">Calibrations</div>
        <div class="stat-value">${history.stats.calibration_runs || 0}</div>
      </div>
      <div class="overview-stat">
        <div class="stat-label">Average Duration</div>
        <div class="stat-value">${formatDuration(history.stats.average_duration_seconds)}</div>
      </div>
      <div class="overview-stat">
        <div class="stat-label">Average cooldown</div>
        <div class="stat-value">${history.stats.average_cooldown_seconds == null ? '—' : formatDuration(history.stats.average_cooldown_seconds)}</div>
      </div>
      ${heaters
        .map(
          (heater) => `
            <div class="overview-stat" title="${escapeHtml(heater.name)} has been powered for ${escapeHtml(formatDuration(heater.lifeSeconds))}, including calibration.">
              <div class="stat-label">${escapeHtml(heater.name)} hours</div>
              <div class="stat-value">${formatHours(heater.lifeSeconds)}</div>
            </div>`,
        )
        .join('')}
    `
    document.getElementById('history-head').innerHTML = `
      <th>Start</th>
      <th>End</th>
      <th>Duration</th>
      <th>Cooldown</th>
      <th>Target</th>
      ${heaters.map((heater) => `<th title="Time on during this run">${escapeHtml(heater.name)}</th>`).join('')}
      <th>Status</th>
    `
    const body = document.getElementById('history-body')
    const columnCount = 6 + heaters.length
    if (!history.runs.length) {
      body.innerHTML = `<tr><td colspan="${columnCount}" class="empty-cell">No runs yet.</td></tr>`
      return
    }
    body.innerHTML = history.runs
      .map((run) => {
        const start = formatWhen(run.started_at)
        const end = formatWhen(run.ended_at)
        const cells = [
          ['Start', `<a href="/history/${run.id}">${escapeHtml(start.date)}<br><span class="time-text">${escapeHtml(start.time)}</span></a>`],
          ['End', `<span>${escapeHtml(end.date)}<br><span class="time-text">${escapeHtml(end.time)}</span></span>`],
          ['Duration', formatDuration(run.elapsed_seconds)],
          ['Cooldown', formatCooldown(run.cooldown_status, run.cooldown_seconds)],
          ['Target', formatTarget(run.target_temp_f)],
          ...heaters.map((heater) => [
            heater.name,
            formatDuration(run.heater_on_seconds[heater.index] || 0),
          ]),
        ]
        cells.push([
          'Status',
          `${kindBadge(run.kind)}<span class="status-badge ${statusClass(run.status)}">${statusLabel(run.status)}</span>`,
        ])
        return `
          <tr data-href="/history/${run.id}">
            ${cells.map(([label, value]) => `<td data-label="${escapeHtml(label)}">${value}</td>`).join('')}
          </tr>`
      })
      .join('')
  }

  document.getElementById('history-body').addEventListener('click', (event) => {
    if (event.target.closest('a')) return
    const row = event.target.closest('tr[data-href]')
    if (row) openPath(row.dataset.href)
  })

  const timer = setInterval(() => {
    refresh().catch((error) => {
      errorEl.hidden = false
      errorEl.textContent = error.message
    })
  }, 3000)
  refresh().catch((error) => {
    errorEl.hidden = false
    errorEl.textContent = error.message
  })

  return () => {
    stopped = true
    clearInterval(timer)
  }
}

function visibleHeaters(settings, stats) {
  const onSeconds = stats.heater_on_seconds || []
  const lifeSeconds = stats.heater_life_seconds || []
  return (settings.heaters || []).map((heater, index) => ({
    name: heater.name,
    index,
    seconds: onSeconds[index] || 0,
    lifeSeconds: lifeSeconds[index] || 0,
    show: heater.enabled || (onSeconds[index] || 0) > 0 || (lifeSeconds[index] || 0) > 0,
  })).filter((heater) => heater.show)
}
