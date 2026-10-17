import { api } from '../api.js'
import { aboveBoiling } from '../format.js'
import { setSourceBadge } from '../layout.js'

const SLOTS = [0, 1]

export function mountSettings(root) {
  root.innerHTML = `
    <form class="dashboard-card settings-card" id="settings-form">
      <div class="card-title-with-timer">
        <h2 class="card-title">Settings</h2>
      </div>
      <p class="lock-banner" id="lock-banner" hidden>
        Heater and probe setup is locked while a run is open. The target temperature can still be changed.
      </p>
      <fieldset>
        <legend>Default Target</legend>
        <label class="field target-field-setting">Temperature (°F)
          <input name="target_temp_f" id="settings-target" type="number" min="40" max="250" step="1" inputmode="numeric" required />
          <p class="boil-warning" id="target-warning" hidden>Above water's boiling point (212°F).</p>
        </label>
        <p class="help">Heaters turn on 2°F below this and off when the control probe reaches it.</p>
      </fieldset>
      <div class="device-columns">
        <section class="device-column">
          <h3 class="column-title">Heaters</h3>
          <p class="help">One must stay enabled. Each heater needs its own BCM pin.</p>
          ${deviceSection('heater', 'Heater')}
          <p class="form-error device-guard" id="heater-guard" data-message="One heater must stay enabled."></p>
        </section>
        <section class="device-column">
          <h3 class="column-title">Temperature probes</h3>
          <p class="help">One must stay enabled. The first enabled probe controls the heaters. Probes can share a pin.</p>
          ${deviceSection('sensor', 'Probe')}
          <p class="form-error device-guard" id="sensor-guard" data-message="One temperature probe must stay enabled."></p>
        </section>
      </div>
      <p class="help">Names, pins, and which devices are enabled are copied onto each run when it starts. Pins are saved for a Raspberry Pi controller. The dummy source ignores them until that controller is connected. Defaults are BCM 17 and 27 for the heaters, and BCM 4 for both probes.</p>
      <p class="form-error" id="settings-error" hidden></p>
      <p class="saved" id="settings-saved" hidden>Saved</p>
      <div class="status-actions">
        <button class="action-btn start-btn" type="submit">Save settings</button>
      </div>
    </form>
  `

  const form = document.getElementById('settings-form')
  const errorEl = document.getElementById('settings-error')
  const savedEl = document.getElementById('settings-saved')
  let locked = false
  let stopped = false

  function applyDeviceState() {
    for (const kind of ['heater', 'sensor']) {
      for (const index of SLOTS) {
        const input = form.elements[`${kind}_enabled_${index}`]
        const section = document.querySelector(`[data-device="${kind}-${index}"]`)
        section.classList.toggle('is-off', !input.checked)
        section.querySelector('.switch-text').textContent = input.checked ? 'Enabled' : 'Disabled'
      }
    }
  }

  function applyLock(isLocked) {
    locked = isLocked
    document.getElementById('lock-banner').hidden = !isLocked
    for (const input of form.querySelectorAll('input:not([name="target_temp_f"])')) {
      input.disabled = isLocked
    }
  }

  form.addEventListener('change', (event) => {
    const kind = (event.target.name || '').match(/^(heater|sensor)_enabled_/)?.[1]
    if (!kind) return
    const guard = document.getElementById(`${kind}-guard`)
    if (!event.target.checked && enabledCount(kind) === 0) {
      event.target.checked = true
      guard.textContent = guard.dataset.message
      savedEl.hidden = true
    } else {
      guard.textContent = ''
    }
    applyDeviceState()
  })

  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    savedEl.hidden = true
    errorEl.hidden = true
    const body = {
      target_temp_f: Math.round(Number(form.elements.target_temp_f.value)),
      heaters: readDevices('heater'),
      sensors: readDevices('sensor'),
    }
    try {
      const saved = await api.saveSettings(body)
      fill(saved)
      savedEl.hidden = false
    } catch (error) {
      errorEl.hidden = false
      errorEl.textContent = error.message
    }
  })

  function enabledCount(kind) {
    return SLOTS.filter((index) => form.elements[`${kind}_enabled_${index}`].checked).length
  }

  function readDevices(kind) {
    return SLOTS.map((index) => ({
      name: form.elements[`${kind}_name_${index}`].value,
      enabled: form.elements[`${kind}_enabled_${index}`].checked,
      gpio: Number(form.elements[`${kind}_gpio_${index}`].value),
    }))
  }

  function syncTargetWarning() {
    document.getElementById('target-warning').hidden = !aboveBoiling(form.elements.target_temp_f.value)
  }

  form.elements.target_temp_f.addEventListener('input', syncTargetWarning)

  function fill(settings) {
    settings.heaters.forEach((heater, index) => writeDevice('heater', index, heater))
    settings.sensors.forEach((sensor, index) => writeDevice('sensor', index, sensor))
    if (document.activeElement !== form.elements.target_temp_f) {
      form.elements.target_temp_f.value = String(Math.round(Number(settings.target_temp_f)))
    }
    syncTargetWarning()
    applyDeviceState()
    applyLock(locked)
  }

  function writeDevice(kind, index, channel) {
    form.elements[`${kind}_name_${index}`].value = channel.name
    form.elements[`${kind}_gpio_${index}`].value = channel.gpio == null ? '' : String(channel.gpio)
    form.elements[`${kind}_enabled_${index}`].checked = Boolean(channel.enabled)
  }

  async function refreshLock() {
    if (stopped) return
    const status = await api.status()
    setSourceBadge(status.source)
    applyLock(status.settings_locked)
  }

  api.settings()
    .then((settings) => {
      fill(settings)
      return refreshLock()
    })
    .catch((error) => {
      errorEl.hidden = false
      errorEl.textContent = error.message
    })

  const timer = setInterval(() => {
    refreshLock().catch(() => {})
  }, 2000)

  return () => {
    stopped = true
    clearInterval(timer)
  }
}

function deviceSection(kind, label) {
  return SLOTS.map((index) => {
    const title = `${label} ${index + 1}`
    return `
      <section class="device" data-device="${kind}-${index}">
        <div class="device-head">
          <p class="device-title">${title}</p>
          <label class="switch">
            <input type="checkbox" role="switch" name="${kind}_enabled_${index}" aria-label="${title}" />
            <span class="switch-track" aria-hidden="true"></span>
            <span class="switch-text">Enabled</span>
          </label>
        </div>
        <div class="device-fields">
          <label class="field">Name
            <input name="${kind}_name_${index}" maxlength="40" required />
          </label>
          <label class="field gpio-field">BCM GPIO
            <input name="${kind}_gpio_${index}" type="number" min="2" max="27" step="1" inputmode="numeric" required />
          </label>
        </div>
      </section>`
  }).join('')
}
