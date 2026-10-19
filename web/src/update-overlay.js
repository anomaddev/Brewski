import { api } from './api.js'

const ACTIVE = new Set(['downloading', 'building', 'installing'])
const BANDS = {
  starting: [4, 10],
  downloading: [12, 38],
  building: [42, 74],
  installing: [78, 96],
  complete: [100, 100],
}

export function mountUpdateOverlay() {
  const host = document.getElementById('update-host')
  if (!host) return () => {}
  let stopped = false
  let shown = false
  let committed = false
  let mode = ''
  let phase = 'starting'
  let phaseAt = 0
  let lostContact = false
  let completeAt = 0
  let latest = ''

  function paint(nextPhase, detail, versionText) {
    if (nextPhase !== phase) {
      phase = nextPhase
      phaseAt = performance.now()
      if (nextPhase === 'complete') completeAt = phaseAt
    }
    host.querySelector('#updating-title').textContent = mode === 'reboot' ? 'Rebooting' : 'Updating'
    host.querySelector('#updating-detail').textContent = detail
    const versionEl = host.querySelector('#updating-version')
    versionEl.hidden = !versionText
    versionEl.textContent = versionText || ''
    const failed = nextPhase === 'failed'
    host.querySelector('#updating-error').hidden = !failed
    host.querySelector('#updating-actions').hidden = !failed
    host.querySelector('.update-bar').hidden = mode === 'reboot' || failed
    if (!failed && mode !== 'reboot') moveBar()
  }

  function moveBar() {
    const elapsed = performance.now() - phaseAt
    const value = percent(phase, elapsed)
    if (value == null) return
    const bar = host.querySelector('.update-bar')
    const fill = host.querySelector('.update-bar-fill')
    bar.setAttribute('aria-valuenow', String(Math.round(value)))
    fill.style.width = `${value}%`
  }

  function show(nextMode) {
    if (!shown) {
      host.innerHTML = overlayMarkup()
      host.querySelector('#updating-dismiss').addEventListener('click', dismiss)
      document.body.classList.add('update-open')
      shown = true
      phase = ''
      phaseAt = performance.now()
    }
    mode = nextMode
  }

  function dismiss() {
    committed = false
    lostContact = false
    completeAt = 0
    mode = ''
    hide()
  }

  function hide() {
    if (!shown) return
    shown = false
    host.innerHTML = ''
    document.body.classList.remove('update-open')
  }

  function fail(message) {
    show('update')
    committed = true
    const errorEl = host.querySelector('#updating-error')
    errorEl.hidden = false
    errorEl.textContent = message || 'The update did not finish.'
    paint('failed', 'Update failed', versionLine(latest))
  }

  async function poll() {
    if (stopped) return
    let update = null
    let status = null
    try {
      ;[update, status] = await Promise.all([api.updateStatus(), api.status()])
    } catch {
      if (shown && (committed || mode === 'reboot')) {
        lostContact = true
        const detail = host.querySelector('#updating-detail')
        if (detail) {
          detail.textContent = mode !== 'reboot' && phase === 'complete'
            ? 'Waiting for restart'
            : 'Reconnecting'
        }
      }
      return
    }
    if (lostContact && (committed || mode === 'reboot')) {
      window.location.reload()
      return
    }
    const active = ACTIVE.has(update.phase)
    if (active) {
      committed = true
      latest = update.latest || latest
      show('update')
      paint(update.phase, phaseText(update.phase), versionLine(update.latest, update.current))
      return
    }
    if (update.phase === 'failed' && committed) {
      fail(update.error)
      return
    }
    if (update.phase === 'complete' && committed) {
      latest = update.latest || latest
      show('update')
      paint('complete', 'Waiting for restart', versionLine(update.latest, update.current))
      if (sameVersion(status?.version, update.latest) || performance.now() - completeAt > 15000) {
        window.location.reload()
      }
    }
  }

  function onUpdating() {
    if (committed) return
    show('update')
    paint('starting', 'Starting the update', '')
  }

  function onCancel() {
    if (committed) return
    hide()
  }

  function onRebooting() {
    show('reboot')
    paint('starting', 'The Pi is restarting. This page will reconnect when Brewski is back.', '')
    mode = 'reboot'
  }

  window.addEventListener('brewski-updating', onUpdating)
  window.addEventListener('brewski-update-cancel', onCancel)
  window.addEventListener('brewski-rebooting', onRebooting)
  const timer = setInterval(() => {
    poll().catch(() => {})
  }, 1000)
  const barTimer = setInterval(() => {
    if (shown && mode !== 'reboot') moveBar()
  }, 250)
  poll().catch(() => {})

  return () => {
    stopped = true
    clearInterval(timer)
    clearInterval(barTimer)
    window.removeEventListener('brewski-updating', onUpdating)
    window.removeEventListener('brewski-update-cancel', onCancel)
    window.removeEventListener('brewski-rebooting', onRebooting)
    hide()
  }
}

function overlayMarkup() {
  return `
    <div class="setup-overlay update-overlay" role="dialog" aria-modal="true" aria-labelledby="updating-title">
      <div class="setup-card">
        <p class="eyebrow">Brewski</p>
        <h1 id="updating-title">Updating</h1>
        <p class="setup-lead" id="updating-detail" aria-live="polite">Starting the update</p>
        <div class="update-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="4" aria-label="Update progress">
          <span class="update-bar-fill"></span>
        </div>
        <p class="setup-progress" id="updating-version" hidden></p>
        <p class="form-error" id="updating-error" hidden></p>
        <div class="setup-actions" id="updating-actions" hidden>
          <button class="setup-back" type="button" id="updating-dismiss">Close</button>
        </div>
      </div>
    </div>`
}

function percent(phaseName, elapsed) {
  const band = BANDS[phaseName]
  if (!band) return null
  const [from, to] = band
  if (from === to) return to
  const creep = 1 - Math.exp(-elapsed / 28000)
  return from + (to - from) * creep
}

function phaseText(phaseName) {
  if (phaseName === 'downloading') return 'Downloading'
  if (phaseName === 'building') return 'Building Brewski'
  if (phaseName === 'installing') return 'Installing'
  if (phaseName === 'complete') return 'Waiting for restart'
  if (phaseName === 'failed') return 'Update failed'
  return 'Starting the update'
}

function versionLine(latest, current) {
  const next = stripVersion(latest)
  const now = stripVersion(current)
  if (now && next && now !== next) return `${now} → ${next}`
  return next || now || ''
}

function stripVersion(value) {
  return String(value || '').replace(/^v/i, '')
}

function sameVersion(running, latest) {
  const next = stripVersion(latest)
  return Boolean(next) && stripVersion(running) === next
}
