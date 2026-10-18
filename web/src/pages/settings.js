import { api } from '../api.js'
import { aboveBoiling, formatHours } from '../format.js'
import { busFields, panelFields, readGpio, syncI2cLabels, validatePins, writeGpio } from '../gpio.js'
import { setSourceBadge } from '../layout.js'
import { openPath } from '../router.js'

const SLOTS = [0, 1]

export function mountSettings(root) {
  root.innerHTML = `
    <form class="dashboard-card settings-card" id="settings-form">
      <div class="card-title-with-timer">
        <h2 class="card-title">Settings</h2>
      </div>
      <p class="lock-banner" id="lock-banner" hidden>
        Heater, probe, and GPIO setup is locked while a run is open. The target, the cabinet limit, the early cutoff, and the minimum on and off times can still be changed.
      </p>
      <fieldset>
        <legend>Default Target</legend>
        <label class="field target-field-setting">Temperature (°F)
          <input name="target_temp_f" id="settings-target" type="number" min="90" max="240" step="1" inputmode="numeric" required />
          <p class="boil-warning" id="target-warning" hidden>Above water's boiling point (212°F).</p>
        </label>
        <p class="help">The first enabled probe is compared with this. Heaters turn off early, by the cutoff below. Without a measured cooling rate they come back on 2°F under that cutoff. After calibration measures the cooling rate, they pulse on near the target to hold it.</p>
      </fieldset>
      <fieldset>
        <legend>Early cutoff</legend>
        <label class="field target-field-setting">Degrees below target (°F)
          <input name="early_cutoff_f" id="early-cutoff" type="number" min="0" max="20" step="0.5" inputmode="decimal" required />
        </label>
        <p class="help">The element turns off this many degrees before the target. It stays hot after the SSR opens, and the probe lags the liquid, so cutting power early lets that residual heat finish the rise instead of overshooting. 3°F is a starting point for a typical small still. Use 0 to wait until the probe reaches the target. A calibration run replaces this with the measured coast. When that run also measures a cooling rate, the element pulses back on near the target instead of waiting for the 2°F band under this cutoff.</p>
      </fieldset>
      <fieldset>
        <legend>Cabinet limit</legend>
        <label class="field target-field-setting">Maximum cabinet temperature (°F)
          <input name="cabinet_limit_f" id="cabinet-limit" type="number" min="100" max="200" step="1" inputmode="numeric" required />
        </label>
        <p class="help">The HTS221 watches the cabinet. Above this temperature every heater turns off and Start stays refused until the cabinet cools back to the limit. 170°F is the default. Humidity is shown and does not trip the stop. Disabling the HTS221 turns this interlock off.</p>
      </fieldset>
      <fieldset>
        <legend>Minimum on time</legend>
        <label class="field target-field-setting">Seconds
          <input name="heater_min_on_seconds" id="heater-min-on" type="number" min="0.5" max="60" step="0.1" inputmode="decimal" required />
        </label>
        <p class="help">Once a heater turns on, it stays on at least this long before the controller can turn it off. Pause and Stop still turn the heaters off immediately.</p>
      </fieldset>
      <fieldset>
        <legend>Minimum off time</legend>
        <label class="field target-field-setting">Seconds
          <input name="heater_min_off_seconds" id="heater-min-off" type="number" min="0.5" max="60" step="0.1" inputmode="decimal" required />
        </label>
        <p class="help">Once a heater turns off, it stays off at least this long. This keeps a zero-crossing SSR from chattering. 0.5–2 seconds is enough. Pause and Stop still turn the heaters off immediately.</p>
      </fieldset>
      <div class="device-columns">
        <section class="device-column">
          <h3 class="column-title">Heaters</h3>
          <p class="help">One must stay enabled. Each heater needs its own BCM pin. Defaults are BCM 12 and 16. Hours count how long that element has been powered, including calibration.</p>
          ${deviceSection('heater', 'Heater')}
          <p class="form-error device-guard" id="heater-guard" data-message="One heater must stay enabled."></p>
        </section>
        <section class="device-column">
          <h3 class="column-title">Temperature probes</h3>
          <p class="help">One must stay enabled. The first enabled probe controls the heaters. A probe name is 1 to 11 characters. Each probe is a MAX31865 with its own chip select. Probe 2 starts disabled. Defaults are chip selects BCM 17 and 8.</p>
          ${deviceSection('sensor', 'Probe')}
          <p class="form-error device-guard" id="sensor-guard" data-message="One temperature probe must stay enabled."></p>
        </section>
      </div>
      <fieldset>
        <legend>Panel</legend>
        <p class="help">Encoder, start and stop buttons, and the LED relays. Defaults are encoder BCM 27 and 22, buttons BCM 5 and 6, and LED relays BCM 23 and 24.</p>
        ${panelFields()}
      </fieldset>
      <fieldset>
        <legend>SPI and I2C</legend>
        <p class="help">Both probes share SPI. Defaults are CLK 11, SDO 9, and SDI 10. The HTS221 and the LCD share I2C. Defaults are SDA 2 and SCL 3. Every pin on this page has to be unique.</p>
        ${busFields()}
      </fieldset>
      <p class="help">Names, pins, and which devices are enabled are copied onto each run when it starts, along with each probe's RTD, wire count, and reference resistor. The installed controller drives these pins. brewski dev on a Mac uses the dummy source and ignores them. Heater defaults are BCM 12 and 16. Each probe starts as a 2-wire PT100 with a 430 Ω reference.</p>
      <p class="form-error" id="settings-error" hidden></p>
      <p class="saved" id="settings-saved" hidden>Saved</p>
      <div class="status-actions">
        <button class="action-btn start-btn" type="submit">Save settings</button>
      </div>
      <section class="reset-panel">
        <h3 class="column-title">Data</h3>
        <p class="help">Erase every run, sample, and cooldown, and restore the default target, heaters, probes, and GPIO pins. Element hours are kept. Heaters turn off. On the dummy source, the pot returns to room temperature. This cannot be undone.</p>
        <button class="action-btn stop-btn" type="button" id="reset-data">Reset all data</button>
      </section>
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
    for (const button of form.querySelectorAll('[data-replace-heater]')) {
      button.disabled = isLocked
    }
    for (const input of form.querySelectorAll(
      'input:not([name="target_temp_f"]):not([name="cabinet_limit_f"]):not([name="heater_min_on_seconds"]):not([name="heater_min_off_seconds"]):not([name="early_cutoff_f"]), select',
    )) {
      input.disabled = isLocked
    }
  }

  form.addEventListener('change', (event) => {
    const rtd = (event.target.name || '').match(/^sensor_rtd_(\d+)$/)
    if (rtd) applyStockReference(form, Number(rtd[1]))
    if ((event.target.name || '').startsWith('i2c_') && event.target.type === 'checkbox') syncI2cLabels(form)
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

  form.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-replace-heater]')
    if (!button || button.disabled) return
    const index = Number(button.dataset.replaceHeater)
    const name = form.elements[`heater_name_${index}`].value.trim() || `Heater ${index + 1}`
    if (!window.confirm(`Reset the hours for ${name}? Do this after you install a new element.`)) return
    savedEl.hidden = true
    errorEl.hidden = true
    try {
      const status = await api.replaceHeater(index)
      renderElementHours(status.heater_life_seconds)
    } catch (error) {
      errorEl.hidden = false
      errorEl.textContent = error.message
    }
  })

  document.getElementById('reset-data').addEventListener('click', async () => {
    if (!window.confirm('Reset all data? Every run is deleted and settings return to their defaults. Element hours are kept.')) return
    savedEl.hidden = true
    errorEl.hidden = true
    try {
      await api.resetData()
      openPath('/')
    } catch (error) {
      errorEl.hidden = false
      errorEl.textContent = error.message
    }
  })

  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    savedEl.hidden = true
    errorEl.hidden = true
    const clash = validatePins(form)
    if (clash) {
      errorEl.hidden = false
      errorEl.textContent = clash
      return
    }
    const body = {
      target_temp_f: Math.round(Number(form.elements.target_temp_f.value)),
      cabinet_limit_f: Math.round(Number(form.elements.cabinet_limit_f.value)),
      early_cutoff_f: Number(form.elements.early_cutoff_f.value),
      heater_min_on_seconds: Number(form.elements.heater_min_on_seconds.value),
      heater_min_off_seconds: Number(form.elements.heater_min_off_seconds.value),
      heaters: readDevices('heater'),
      sensors: readDevices('sensor'),
      gpio: readGpio(form),
    }
    try {
      const saved = await api.saveSettings(body)
      fill(saved)
      savedEl.textContent = 'Saved'
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
    return SLOTS.map((index) => {
      const device = {
        name: form.elements[`${kind}_name_${index}`].value,
        enabled: form.elements[`${kind}_enabled_${index}`].checked,
        gpio: Number(form.elements[`${kind}_gpio_${index}`].value),
      }
      if (kind === 'sensor') {
        device.wires = Number(form.elements[`sensor_wires_${index}`].value)
        device.rtd = form.elements[`sensor_rtd_${index}`].value
        device.ref_ohms = Number(form.elements[`sensor_ref_${index}`].value)
      }
      return device
    })
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
    const minOn = form.elements.heater_min_on_seconds
    if (document.activeElement !== minOn) {
      minOn.value = String(settings.heater_min_on_seconds)
    }
    const cabinet = form.elements.cabinet_limit_f
    if (document.activeElement !== cabinet && settings.cabinet_limit_f != null) {
      cabinet.value = String(Math.round(Number(settings.cabinet_limit_f)))
    }
    const cutoff = form.elements.early_cutoff_f
    if (document.activeElement !== cutoff) {
      cutoff.value = String(settings.early_cutoff_f)
    }
    const minOff = form.elements.heater_min_off_seconds
    if (document.activeElement !== minOff) {
      minOff.value = String(settings.heater_min_off_seconds)
    }
    writeGpio(form, settings.gpio)
    syncTargetWarning()
    applyDeviceState()
    applyLock(locked)
  }

  function writeDevice(kind, index, channel) {
    form.elements[`${kind}_name_${index}`].value = channel.name
    form.elements[`${kind}_gpio_${index}`].value = channel.gpio == null ? '' : String(channel.gpio)
    form.elements[`${kind}_enabled_${index}`].checked = Boolean(channel.enabled)
    if (kind !== 'sensor') return
    const rtd = channel.rtd === 'pt1000' ? 'pt1000' : 'pt100'
    form.elements[`sensor_rtd_${index}`].value = rtd
    form.elements[`sensor_wires_${index}`].value = String([2, 3, 4].includes(channel.wires) ? channel.wires : 2)
    const ref = channel.ref_ohms == null ? (rtd === 'pt1000' ? 4300 : 430) : channel.ref_ohms
    form.elements[`sensor_ref_${index}`].value = String(ref)
  }

  function renderElementHours(seconds) {
    const values = seconds || []
    for (const index of SLOTS) {
      const node = form.querySelector(`[data-element-hours="${index}"]`)
      if (node) node.textContent = `${formatHours(values[index] || 0)} on this element`
    }
  }

  async function refreshLock() {
    if (stopped) return
    const status = await api.status()
    setSourceBadge(status.source)
    applyLock(status.settings_locked)
    renderElementHours(status.heater_life_seconds)
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

function applyStockReference(form, index) {
  const ref = form.elements[`sensor_ref_${index}`]
  const current = Number(ref.value)
  if (ref.value !== '' && current !== 430 && current !== 4300) return
  ref.value = form.elements[`sensor_rtd_${index}`].value === 'pt1000' ? '4300' : '430'
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
            <input name="${kind}_name_${index}" maxlength="${kind === 'sensor' ? 11 : 40}" required />
          </label>
          <label class="field gpio-field">${kind === 'sensor' ? 'Chip select' : 'BCM GPIO'}
            <input name="${kind}_gpio_${index}" type="number" min="2" max="27" step="1" inputmode="numeric" required />
          </label>
        </div>
        ${kind === 'sensor' ? `
        <div class="device-fields probe-config">
          <label class="field">RTD
            <select name="sensor_rtd_${index}">
              <option value="pt100">PT100</option>
              <option value="pt1000">PT1000</option>
            </select>
          </label>
          <label class="field">Wires
            <select name="sensor_wires_${index}">
              <option value="2">2</option>
              <option value="3" selected>3</option>
              <option value="4">4</option>
            </select>
          </label>
          <label class="field ref-field">Reference (Ω)
            <input name="sensor_ref_${index}" type="number" min="100" max="10000" step="0.01" inputmode="decimal" required />
          </label>
        </div>` : ''}
        ${kind === 'heater' ? `
        <div class="element-life">
          <p class="help" data-element-hours="${index}">0.00 h on this element</p>
          <button type="button" class="link-btn" data-replace-heater="${index}">Replace element</button>
        </div>` : ''}
      </section>`
  }).join('')
}
