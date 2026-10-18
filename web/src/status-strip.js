import { api } from './api.js'
import { aboveBoiling, escapeHtml, formatDuration, formatHours, formatTarget, statusClass, statusLabel } from './format.js'
import { setAppVersion, setSourceBadge } from './layout.js'

export function mountStatusStrip(host) {
  host.hidden = true
  host.innerHTML = `
    <a class="status-strip" href="/" data-mode="idle" title="Open dashboard">
      <span class="status-strip-inner">
        <span class="status-strip-state">
          <span class="status-badge status-idle" data-strip="mode">Idle</span>
          <span class="status-badge status-calibration" data-strip="kind" hidden>Calibration</span>
          <span class="status-strip-timer" data-strip="timer" hidden>00:00:00</span>
        </span>
        <span class="status-strip-temps" data-strip="temps"></span>
        <span class="status-strip-emergency" data-strip="emergency" hidden></span>
        <span class="status-strip-side">
          <span class="status-strip-stat">
            <span class="status-strip-kicker">Cabinet</span>
            <strong data-strip="cabinet">—</strong>
          </span>
          <span class="status-strip-stat">
            <span class="status-strip-kicker">Humidity</span>
            <strong data-strip="humidity">—</strong>
          </span>
          <span class="status-strip-stat">
            <span class="status-strip-kicker">Limit</span>
            <strong data-strip="limit">—</strong>
          </span>
          <span class="status-strip-heaters" data-strip="heaters"></span>
          <span class="status-strip-stat">
            <span class="status-strip-kicker">Target</span>
            <strong data-strip="target">—</strong>
          </span>
        </span>
      </span>
    </a>
  `

  const strip = host.querySelector('.status-strip')
  let stopped = false

  function renderTemps(status) {
    const temps = host.querySelector('[data-strip="temps"]')
    const names = status.sensor_names || []
    const key = JSON.stringify(names)
    if (temps.dataset.key !== key) {
      temps.dataset.key = key
      temps.innerHTML = names
        .map(
          (name, index) => `
            <span class="status-strip-stat${index === 0 ? ' is-control' : ''}" data-probe="${index}">
              <span class="status-strip-kicker">${escapeHtml(name)}${index === 0 ? ' · control' : ''}</span>
              <strong data-temp="${index}">—</strong>
            </span>`,
        )
        .join('')
    }
    names.forEach((_, index) => {
      const value = status.temps_f?.[index]
      const missing = value == null || Number.isNaN(Number(value))
      const stat = temps.querySelector(`[data-probe="${index}"]`)
      stat.classList.toggle('is-hot', aboveBoiling(value))
      stat.querySelector(`[data-temp="${index}"]`).textContent = missing ? '—' : `${Number(value).toFixed(1)}°F`
    })
  }

  function renderHeaters(status) {
    const heaters = host.querySelector('[data-strip="heaters"]')
    const names = status.heater_names || []
    const key = JSON.stringify(names)
    if (heaters.dataset.key !== key) {
      heaters.dataset.key = key
      heaters.innerHTML = names
        .map(
          (name, index) => `
            <span class="status-strip-stat" data-heater-stat="${index}">
              <span class="status-strip-kicker">${escapeHtml(name)}</span>
              <strong data-heater="${index}"><i aria-hidden="true"></i><span>Off</span></strong>
            </span>`,
        )
        .join('')
    }
    names.forEach((_, index) => {
      const on = Boolean(status.heaters_on?.[index])
      const stat = heaters.querySelector(`[data-heater-stat="${index}"]`)
      stat.classList.toggle('is-on', on)
      stat.querySelector(`[data-heater="${index}"] span`).textContent = on ? 'On' : 'Off'
      const slot = (status.heater_indexes || [])[index] ?? index
      const seconds = (status.heater_life_seconds || [])[slot] || 0
      stat.title = `${formatHours(seconds)} on this element`
    })
  }

  function render(status) {
    setSourceBadge(status.source)
    setAppVersion(status.version)
    strip.dataset.mode = status.mode || 'idle'
    const mode = host.querySelector('[data-strip="mode"]')
    mode.textContent = statusLabel(status.mode)
    mode.className = `status-badge ${statusClass(status.mode)}`
    host.querySelector('[data-strip="kind"]').hidden = status.run_kind !== 'calibration'
    const active = status.mode === 'running' || status.mode === 'paused' || status.mode === 'cooling'
    const timer = host.querySelector('[data-strip="timer"]')
    timer.hidden = !active
    if (active) timer.textContent = formatDuration(status.elapsed_seconds)
    host.querySelector('[data-strip="target"]').textContent = formatTarget(status.target_temp_f)
    const cabinet = status.cabinet_temp_f
    const humidity = status.cabinet_humidity_pct
    const limit = status.cabinet_limit_f
    host.querySelector('[data-strip="cabinet"]').textContent =
      cabinet == null || Number.isNaN(Number(cabinet)) ? '—' : `${Number(cabinet).toFixed(1)}°F`
    host.querySelector('[data-strip="humidity"]').textContent =
      humidity == null || Number.isNaN(Number(humidity)) ? '—' : `${Math.round(Number(humidity))}%`
    host.querySelector('[data-strip="limit"]').textContent =
      limit == null || Number.isNaN(Number(limit)) ? '—' : `${Math.round(Number(limit))}°F`
    const codes = status.emergency || []
    const alarm = host.querySelector('[data-strip="emergency"]')
    alarm.hidden = codes.length === 0
    alarm.textContent = codes.map((item) => `${item.code}: ${item.summary}`).join(' ')
    renderTemps(status)
    renderHeaters(status)
    host.hidden = false
  }

  async function refresh() {
    if (stopped) return
    render(await api.status())
  }

  const timer = setInterval(() => {
    refresh().catch(() => {})
  }, 1000)
  refresh().catch(() => {})

  return () => {
    stopped = true
    clearInterval(timer)
  }
}
