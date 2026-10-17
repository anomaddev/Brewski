import { api } from '../api.js'
import { openPath } from '../router.js'
import { escapeHtml, formatDuration, formatTemp, formatWhen, statusClass, statusLabel } from '../format.js'
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
    const showSecond = settings.heater_count === 2 || history.stats.heater_on_seconds[1] > 0
    const names = settings.heater_names
    document.getElementById('overview-stats').innerHTML = `
      <div class="overview-stat">
        <div class="stat-label">Total Runs</div>
        <div class="stat-value">${history.stats.total_runs}</div>
      </div>
      <div class="overview-stat">
        <div class="stat-label">Average Duration</div>
        <div class="stat-value">${formatDuration(history.stats.average_duration_seconds)}</div>
      </div>
      <div class="overview-stat">
        <div class="stat-label">${escapeHtml(names[0])} on</div>
        <div class="stat-value">${formatDuration(history.stats.heater_on_seconds[0])}</div>
      </div>
      ${
        showSecond
          ? `<div class="overview-stat">
              <div class="stat-label">${escapeHtml(names[1])} on</div>
              <div class="stat-value">${formatDuration(history.stats.heater_on_seconds[1])}</div>
            </div>`
          : ''
      }
    `
    document.getElementById('history-head').innerHTML = `
      <th>Start</th>
      <th>End</th>
      <th>Duration</th>
      <th>Target</th>
      <th>${escapeHtml(names[0])}</th>
      ${showSecond ? `<th>${escapeHtml(names[1])}</th>` : ''}
      <th>Status</th>
    `
    const body = document.getElementById('history-body')
    if (!history.runs.length) {
      body.innerHTML = `<tr><td colspan="${showSecond ? 7 : 6}" class="empty-cell">No runs yet.</td></tr>`
      return
    }
    body.innerHTML = history.runs
      .map((run) => {
        const start = formatWhen(run.started_at)
        const end = formatWhen(run.ended_at)
        return `
          <tr data-href="/history/${run.id}">
            <td><a href="/history/${run.id}">${escapeHtml(start.date)}<br><span class="time-text">${escapeHtml(start.time)}</span></a></td>
            <td>${escapeHtml(end.date)}<br><span class="time-text">${escapeHtml(end.time)}</span></td>
            <td>${formatDuration(run.elapsed_seconds)}</td>
            <td>${formatTemp(run.target_temp_f)}</td>
            <td>${formatDuration(run.heater_on_seconds[0])}</td>
            ${showSecond ? `<td>${formatDuration(run.heater_on_seconds[1] || 0)}</td>` : ''}
            <td><span class="status-badge ${statusClass(run.status)}">${statusLabel(run.status)}</span></td>
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
