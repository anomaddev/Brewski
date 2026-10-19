import { api } from './api.js'
import { numberStepper } from './controls.js'
import { aboveBoiling, escapeHtml } from './format.js'
import { busFields, markPinClashes, panelFields, pinSelect, readGpio, syncI2cLabels, validatePins } from './gpio.js'
import { openPath } from './router.js'

const STEPS = ['intro', 'heaters', 'probes', 'panel', 'bus', 'target']

export function mountSetup() {
  const host = document.getElementById('setup-host')
  if (!host) return () => {}
  let stopped = false
  let close = () => {}
  ;(async () => {
    try {
      const settings = await api.settings()
      if (stopped || settings.setup_complete) return
      const status = await api.status()
      if (stopped || status.mode === 'running' || status.mode === 'paused' || status.mode === 'cooling') return
      close = openSetup(host, settings)
    } catch {
      // Leave the page usable when setup status cannot be read.
    }
  })()
  return () => {
    stopped = true
    close()
  }
}

function openSetup(host, settings) {
  host.innerHTML = `
    <div class="setup-overlay" role="dialog" aria-modal="true" aria-labelledby="setup-title">
      <form class="setup-card" id="setup-form" novalidate>
        <p class="eyebrow">Get started</p>
        <p class="setup-progress" id="setup-progress"></p>
        <h1 id="setup-title"></h1>
        <div id="setup-panels">
          <section class="setup-panel" data-step="intro">
            <p class="setup-lead">This still is installed and wired. Brewski needs the heaters, the probes, the panel, the SPI and I2C pins, and the temperature you want to hold. After that, fill the pot with water and calibrate. A run stays locked until that calibration is saved.</p>
          </section>
          <section class="setup-panel" data-step="heaters">
            <p class="setup-lead">One heater has to stay enabled. Each SSR needs its own BCM pin. Defaults are 12 and 16.</p>
            <div class="setup-devices">${deviceFields('heater', 'Heater', settings.heaters)}</div>
          </section>
          <section class="setup-panel" data-step="probes">
            <p class="setup-lead">One probe has to stay enabled. The first enabled probe controls the heaters. A probe name is 1 to 11 characters. Each probe is a MAX31865 with its own chip select. Probe 2 starts disabled until that board is wired. Defaults are chip selects 17 and 8.</p>
            <div class="setup-devices">${deviceFields('sensor', 'Probe', settings.sensors)}</div>
          </section>
          <section class="setup-panel" data-step="panel">
            <p class="setup-lead">The temperature dial is a rotary encoder. Start and stop are push buttons, each with its own LED relay. These pins are saved for the Raspberry Pi. The dummy source does not drive them.</p>
            ${panelFields(settings.gpio)}
          </section>
          <section class="setup-panel" data-step="bus">
            <p class="setup-lead">Both temperature probes share one SPI bus. The HTS221 and the LCD share the I2C bus. Either of those boards can be turned off.</p>
            ${busFields(settings.gpio)}
          </section>
          <section class="setup-panel" data-step="target">
            <p class="setup-lead">Calibration heats water to this temperature and measures how far it keeps climbing after the element turns off. That climb is the coast a later run shuts off early for.</p>
            <label class="field">Temperature (°F)
              ${numberStepper(`<input name="target_temp_f" id="setup-target" type="number" min="90" max="240" step="1" inputmode="numeric" required value="${Math.round(Number(settings.target_temp_f))}" />`)}
            </label>
            <p class="boil-warning" id="setup-boil" hidden>Above water's boiling point (212°F).</p>
            <label class="field">Still size (gal)
              ${numberStepper(`<input name="charge_gallons" id="setup-charge" type="number" min="0.25" max="100" step="0.25" inputmode="decimal" required value="${settings.charge_gallons == null ? '' : settings.charge_gallons}" />`)}
            </label>
            <p class="help">Gallons the pot holds. Calibration uses this size. A later calibration scales the learned coast when the size changes. Start stays off until the calibration is saved.</p>
          </section>
        </div>
        <p class="form-error" id="setup-error" hidden></p>
        <div class="setup-actions">
          <button class="setup-back" type="button" id="setup-back">Back</button>
          <button class="action-btn start-btn" type="submit" id="setup-next">Continue</button>
        </div>
      </form>
    </div>
  `
  document.body.classList.add('setup-open')
  const form = host.querySelector('#setup-form')
  const errorEl = host.querySelector('#setup-error')
  let step = 0
  let left = false

  const syncDevices = () => {
    for (const kind of ['heater', 'sensor']) {
      for (const index of [0, 1]) {
        const input = form.elements[`${kind}_enabled_${index}`]
        const section = form.querySelector(`[data-device="${kind}-${index}"]`)
        section.classList.toggle('is-off', !input.checked)
        section.querySelector('.switch-text').textContent = input.checked ? 'Enabled' : 'Disabled'
      }
    }
  }
  syncDevices()
  syncI2cLabels(form)
  markPinClashes(form)

  form.addEventListener('change', (event) => {
    if (event.target.closest('.gpio-field')) markPinClashes(form)
    if ((event.target.name || '').startsWith('i2c_') && event.target.type === 'checkbox') syncI2cLabels(form)
    const rtd = (event.target.name || '').match(/^sensor_rtd_(\d+)$/)
    if (rtd) applyStockReference(form, Number(rtd[1]))
    const kind = (event.target.name || '').match(/^(heater|sensor)_enabled_/)?.[1]
    if (!kind) return
    if (!event.target.checked && enabledCount(form, kind) === 0) {
      event.target.checked = true
      showError(kind === 'heater' ? 'One heater must stay enabled.' : 'One temperature probe must stay enabled.')
    } else {
      clearError()
    }
    syncDevices()
  })

  const target = form.elements.target_temp_f
  const syncBoil = () => {
    host.querySelector('#setup-boil').hidden = !aboveBoiling(target.value)
  }
  target.addEventListener('input', syncBoil)
  syncBoil()

  const showStep = (next) => {
    step = next
    clearError()
    host.querySelector('#setup-progress').textContent = `${step + 1} of ${STEPS.length}`
    host.querySelector('#setup-title').textContent = titleFor(STEPS[step])
    host.querySelectorAll('.setup-panel').forEach((panel) => {
      panel.classList.toggle('is-active', panel.dataset.step === STEPS[step])
    })
    const back = host.querySelector('#setup-back')
    back.hidden = step === 0
    host.querySelector('#setup-next').textContent = step === STEPS.length - 1 ? 'Finish setup' : 'Continue'
    const focusTarget = step === 0 ? host.querySelector('#setup-next') : host.querySelector('.setup-panel.is-active input')
    focusTarget?.focus()
  }

  host.querySelector('#setup-back').addEventListener('click', () => {
    if (step > 0) showStep(step - 1)
  })

  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    clearError()
    const problem = validateStep(form, STEPS[step])
    if (problem) {
      showError(problem)
      return
    }
    if (step < STEPS.length - 1) {
      showStep(step + 1)
      return
    }
    const next = host.querySelector('#setup-next')
    next.disabled = true
    try {
      const charge = form.elements.charge_gallons.value.trim()
      await api.saveSettings({
        target_temp_f: Math.round(Number(form.elements.target_temp_f.value)),
        charge_gallons: charge === '' ? null : Number(charge),
        heaters: readDevices(form, 'heater'),
        sensors: readDevices(form, 'sensor'),
        gpio: readGpio(form),
        setup_complete: true,
      })
    } catch (error) {
      showError(error.message)
      next.disabled = false
      return
    }
    let update = null
    try {
      update = await api.updateStatus()
    } catch {
      update = null
    }
    if (!update?.available) {
      finishSetup()
      return
    }
    showUpdateOffer(update)
  })

  showStep(0)

  function finishSetup({ reload = false } = {}) {
    close()
    if (reload) {
      window.location.assign('/')
      return
    }
    window.dispatchEvent(new Event('brewski-refresh'))
    if (window.location.pathname !== '/') openPath('/')
  }

  function showUpdateOffer(update) {
    const latest = stripVersion(update.latest)
    const current = stripVersion(update.current)
    const canApply = update.apply_supported === true
    const offer = update.source === 'beta'
      ? `Setup is saved. Beta commit ${escapeHtml(latest)} is available. This still is running ${escapeHtml(current)}.`
      : `Setup is saved. Version ${escapeHtml(latest)} is available. This still is running ${escapeHtml(current)}.`
    host.innerHTML = `
      <div class="setup-overlay" role="dialog" aria-modal="true" aria-labelledby="update-title">
        <div class="setup-card">
          <p class="eyebrow">Get started</p>
          <h1 id="update-title">Update available</h1>
          <p class="setup-lead">${offer}</p>
          <p class="setup-progress" id="update-phase">${canApply ? '' : 'This session cannot install a release. Update from the Pi.'}</p>
          <p class="form-error" id="update-error" hidden></p>
          <div class="setup-actions">
            <button class="setup-back" type="button" id="update-skip">Not now</button>
            <button class="action-btn start-btn" type="button" id="update-apply" ${canApply ? '' : 'disabled'}>Update</button>
          </div>
        </div>
      </div>
    `
    const phaseEl = host.querySelector('#update-phase')
    const errorEl = host.querySelector('#update-error')
    const apply = host.querySelector('#update-apply')
    host.querySelector('#update-skip').addEventListener('click', () => finishSetup())
    apply.addEventListener('click', async () => {
      apply.disabled = true
      host.querySelector('#update-skip').disabled = true
      errorEl.hidden = true
      window.dispatchEvent(new CustomEvent('brewski-updating'))
      try {
        await api.applyUpdate()
      } catch (error) {
        window.dispatchEvent(new CustomEvent('brewski-update-cancel'))
        errorEl.hidden = false
        errorEl.textContent = error.message
        apply.disabled = false
        host.querySelector('#update-skip').disabled = false
        return
      }
      await watchUpdate(latest, phaseEl, errorEl)
    })
  }

  async function watchUpdate(latest, phaseEl, errorEl) {
    while (!left) {
      try {
        const [update, status] = await Promise.all([api.updateStatus(), api.status()])
        phaseEl.textContent = phaseText(update.phase)
        if (update.phase === 'failed') {
          errorEl.hidden = false
          errorEl.textContent = update.error || 'The update did not finish. Setup is still saved.'
          const skip = host.querySelector('#update-skip')
          if (skip) skip.disabled = false
          const apply = host.querySelector('#update-apply')
          if (apply) apply.disabled = true
          return
        }
        if (sameVersion(status.version, latest)) {
          finishSetup({ reload: true })
          return
        }
      } catch {
        phaseEl.textContent = 'Reconnecting'
      }
      await delay(1000)
    }
  }

  function showError(message) {
    errorEl.hidden = false
    errorEl.textContent = message
  }
  function clearError() {
    errorEl.hidden = true
    errorEl.textContent = ''
  }
  function close() {
    left = true
    host.innerHTML = ''
    document.body.classList.remove('setup-open')
  }
  return close
}

function stripVersion(value) {
  return String(value || '').replace(/^v/i, '')
}

function sameVersion(running, latest) {
  return latest && stripVersion(running) === stripVersion(latest)
}

function phaseText(phase) {
  if (phase === 'downloading') return 'Downloading'
  if (phase === 'building') return 'Building the dashboard'
  if (phase === 'installing') return 'Installing'
  if (phase === 'complete') return 'Starting'
  if (phase === 'failed') return 'Update failed'
  return 'Starting the update'
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function titleFor(step) {
  if (step === 'heaters') return 'Heaters'
  if (step === 'probes') return 'Temperature probes'
  if (step === 'panel') return 'Panel'
  if (step === 'bus') return 'SPI and I2C'
  if (step === 'target') return 'Target'
  return 'Set up this still'
}

function deviceFields(kind, label, channels) {
  return [0, 1]
    .map((index) => {
      const channel = channels[index] || { name: '', enabled: true, gpio: '' }
      const title = `${label} ${index + 1}`
      return `
        <section class="device" data-device="${kind}-${index}">
          <div class="device-head">
            <p class="device-title">${title}</p>
            <label class="switch">
              <input type="checkbox" role="switch" name="${kind}_enabled_${index}" aria-label="${title}" ${channel.enabled ? 'checked' : ''} />
              <span class="switch-track" aria-hidden="true"></span>
              <span class="switch-text">${channel.enabled ? 'Enabled' : 'Disabled'}</span>
            </label>
          </div>
          <div class="device-fields">
            <label class="field">Name
              <input name="${kind}_name_${index}" maxlength="${kind === 'sensor' ? 11 : 40}" required value="${escapeHtml(channel.name)}" />
            </label>
            <label class="field gpio-field">${kind === 'sensor' ? 'Chip select' : 'BCM GPIO'}
              ${pinSelect(`${kind}_gpio_${index}`, channel.gpio)}
            </label>
          </div>
          ${kind === 'sensor' ? probeConfig(index, channel) : ''}
        </section>`
    })
    .join('')
}

function probeConfig(index, channel) {
  const rtd = channel.rtd === 'pt1000' ? 'pt1000' : 'pt100'
  const wires = [2, 3, 4].includes(channel.wires) ? channel.wires : 2
  const ref = channel.ref_ohms == null ? (rtd === 'pt1000' ? 4300 : 430) : channel.ref_ohms
  const wireOptions = [2, 3, 4]
    .map((count) => `<option value="${count}" ${wires === count ? 'selected' : ''}>${count}</option>`)
    .join('')
  return `
    <div class="device-fields probe-config">
      <label class="field">RTD
        <select name="sensor_rtd_${index}">
          <option value="pt100" ${rtd === 'pt100' ? 'selected' : ''}>PT100</option>
          <option value="pt1000" ${rtd === 'pt1000' ? 'selected' : ''}>PT1000</option>
        </select>
      </label>
      <label class="field">Wires
        <select name="sensor_wires_${index}">${wireOptions}</select>
      </label>
      <label class="field ref-field">Reference (Ω)
        ${numberStepper(`<input name="sensor_ref_${index}" type="number" min="100" max="10000" step="0.01" inputmode="decimal" required value="${ref}" />`)}
      </label>
    </div>`
}

function applyStockReference(form, index) {
  const ref = form.elements[`sensor_ref_${index}`]
  const current = Number(ref.value)
  if (ref.value !== '' && current !== 430 && current !== 4300) return
  ref.value = form.elements[`sensor_rtd_${index}`].value === 'pt1000' ? '4300' : '430'
}

function enabledCount(form, kind) {
  return [0, 1].filter((index) => form.elements[`${kind}_enabled_${index}`].checked).length
}

function readDevices(form, kind) {
  return [0, 1].map((index) => {
    const device = {
      name: form.elements[`${kind}_name_${index}`].value.trim(),
      enabled: form.elements[`${kind}_enabled_${index}`].checked,
      gpio: Math.round(Number(form.elements[`${kind}_gpio_${index}`].value)),
    }
    if (kind === 'sensor') {
      device.wires = Number(form.elements[`sensor_wires_${index}`].value)
      device.rtd = form.elements[`sensor_rtd_${index}`].value
      device.ref_ohms = Number(form.elements[`sensor_ref_${index}`].value)
    }
    return device
  })
}

function validateStep(form, step) {
  if (step === 'heaters' || step === 'probes') {
    const kind = step === 'heaters' ? 'heater' : 'sensor'
    const label = kind === 'heater' ? 'heater' : 'temperature probe'
    if (enabledCount(form, kind) === 0) return `One ${label} must stay enabled.`
    const devices = readDevices(form, kind)
    for (const device of devices) {
      const nameLimit = kind === 'sensor' ? 11 : 40
      if (!device.name || device.name.length > nameLimit) {
        return `Each name needs 1 to ${nameLimit} characters.`
      }
      if (!Number.isInteger(device.gpio) || device.gpio < 2 || device.gpio > 27) {
        const pin = kind === 'sensor' ? 'chip select' : 'BCM pin'
        return `Each ${pin} must be a whole number from 2 to 27.`
      }
    }
    if (kind === 'sensor') {
      for (const device of devices) {
        if (![2, 3, 4].includes(device.wires)) return 'Each probe must be 2-wire, 3-wire, or 4-wire.'
        if (device.rtd !== 'pt100' && device.rtd !== 'pt1000') return 'Each probe must be a PT100 or PT1000.'
        if (!Number.isFinite(device.ref_ohms) || device.ref_ohms < 100 || device.ref_ohms > 10000) {
          return 'Each reference resistor must be between 100 and 10000 ohms.'
        }
      }
    }
  }
  if (step === 'heaters' || step === 'probes' || step === 'panel' || step === 'bus' || step === 'target') {
    const clash = validatePins(form)
    if (clash) return clash
  }
  if (step === 'target') {
    const target = Number(form.elements.target_temp_f.value)
    if (!Number.isInteger(target) || target < 90 || target > 240) {
      return 'Target must be a whole number from 90 to 240.'
    }
    const raw = form.elements.charge_gallons.value.trim()
    const gallons = Number(raw)
    if (raw === '' || !Number.isFinite(gallons) || gallons < 0.25 || gallons > 100) {
      return 'Still size must be between 0.25 and 100 gallons.'
    }
  }
  return ''
}
