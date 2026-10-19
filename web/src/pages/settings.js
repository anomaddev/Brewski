import { api } from '../api.js'
import { aboveBoiling, formatHours } from '../format.js'
import { busFields, panelFields, readGpio, syncI2cLabels, validatePins, writeGpio } from '../gpio.js'
import { setSourceBadge } from '../layout.js'
import { openPath } from '../router.js'

const SLOTS = [0, 1]

export function mountSettings(root) {
  root.innerHTML = `
    <form class="settings-page" id="settings-form">
      <header class="settings-head">
        <h1>Settings</h1>
      </header>
      <p class="lock-banner" id="lock-banner" hidden>
        Heater, probe, and GPIO setup is locked while a run is open. The target, the cabinet limit, the early cutoff, the minimum on and off times, and the update source can still be changed.
      </p>
      <div class="settings-top">
        <section class="dashboard-card settings-card" aria-labelledby="control-title">
          <h2 class="card-title" id="control-title">Control</h2>
          <div class="setting-list">
            ${settingRow(
              'Default target',
              {
                lead: 'Compared with the first enabled probe.',
                rows: [
                  ['Off early', 'By the cutoff below'],
                  ['Before cal.', 'Back on 2°F under that cutoff'],
                  ['After cal.', 'Pulses near the target'],
                ],
              },
              `<label class="field target-field-setting">Temperature (°F)
                <input name="target_temp_f" id="settings-target" type="number" min="90" max="240" step="1" inputmode="numeric" required />
                <p class="boil-warning" id="target-warning" hidden>Above water's boiling point (212°F).</p>
              </label>`,
            )}
            ${settingRow(
              'Early cutoff',
              {
                lead: 'Power drops this far before the target so residual heat finishes the rise.',
                rows: [
                  ['3°F', 'Starting point for a small still'],
                  ['0', 'Wait until the probe hits the target'],
                  ['After cal.', 'Replaced by the measured coast'],
                ],
              },
              `<label class="field target-field-setting">Degrees below target (°F)
                <input name="early_cutoff_f" id="early-cutoff" type="number" min="0" max="20" step="0.5" inputmode="decimal" required />
              </label>`,
            )}
            ${settingRow(
              'Cabinet limit',
              {
                lead: 'The HTS221 stops the heaters and refuses Start above this.',
                rows: [
                  ['170°F', 'Default'],
                  ['Humidity', 'Shown. Does not trip the stop'],
                  ['HTS221 off', 'Interlock is off'],
                ],
              },
              `<label class="field target-field-setting">Maximum (°F)
                <input name="cabinet_limit_f" id="cabinet-limit" type="number" min="100" max="200" step="1" inputmode="numeric" required />
              </label>`,
            )}
            ${settingRow(
              'Minimum on time',
              {
                lead: 'A heater stays on at least this long.',
                rows: [['Pause, Stop', 'Still turn the heaters off now']],
              },
              `<label class="field target-field-setting">Seconds
                <input name="heater_min_on_seconds" id="heater-min-on" type="number" min="0.5" max="60" step="0.1" inputmode="decimal" required />
              </label>`,
            )}
            ${settingRow(
              'Minimum off time',
              {
                lead: 'A heater stays off at least this long so the SSR does not chatter.',
                rows: [
                  ['0.5–2 s', 'Enough for a zero-crossing SSR'],
                  ['Pause, Stop', 'Still turn the heaters off now'],
                ],
              },
              `<label class="field target-field-setting">Seconds
                <input name="heater_min_off_seconds" id="heater-min-off" type="number" min="0.5" max="60" step="0.1" inputmode="decimal" required />
              </label>`,
            )}
          </div>
        </section>
        <section class="dashboard-card settings-card" aria-labelledby="software-title">
          <h2 class="card-title" id="software-title">Software</h2>
          <div class="setting-row source-row">
            <div class="setting-copy">
              <h3>Update source${tip({
                lead: 'Used by Check for Update and the boot prompt.',
                rows: [
                  ['Main', 'Latest published release'],
                  ['Beta', 'Latest commit on beta'],
                ],
              })}</h3>
            </div>
            <div class="source-choice" role="radiogroup" aria-label="Update source">
              <label>Main
                <input type="radio" name="update_source" value="main" checked />
              </label>
              <label>Beta
                <input type="radio" name="update_source" value="beta" />
              </label>
            </div>
          </div>
        </section>
      </div>
      <section class="dashboard-card settings-card">
        <h2 class="card-title">Devices</h2>
        <div class="device-columns">
          <section class="device-column">
            <h3 class="column-title">Heaters${tip({
              lead: 'Each element has its own pin. One stays enabled.',
              rows: [
                ['Defaults', 'BCM 12 and 16'],
                ['Hours', 'Time powered, including calibration'],
              ],
            })}</h3>
            ${deviceSection('heater', 'Heater')}
            <p class="form-error device-guard" id="heater-guard" data-message="One heater must stay enabled."></p>
          </section>
          <section class="device-column">
            <h3 class="column-title">Temperature probes${tip({
              lead: 'The first enabled probe controls the heaters. One stays enabled.',
              rows: [
                ['Name', '1–11 characters'],
                ['RTD', 'PT100 or PT1000'],
                ['Wires', '2, 3, or 4'],
                ['Reference', '430 Ω or 4300 Ω'],
                ['Probe 2', 'Starts disabled'],
                ['Chip select', 'BCM 17 and 8'],
              ],
            })}</h3>
            ${deviceSection('sensor', 'Probe')}
            <p class="form-error device-guard" id="sensor-guard" data-message="One temperature probe must stay enabled."></p>
          </section>
        </div>
      </section>
      <section class="dashboard-card settings-card">
        <h2 class="card-title with-tip">Wiring${tip({
          lead: 'Copied onto each run when it starts.',
          rows: [
            ['Copied', 'Names, pins, RTD, wires, reference'],
            ['Pi', 'The controller drives these pins'],
            ['Dev', 'The dummy source ignores them'],
            ['Heaters', 'BCM 12 and 16'],
            ['Probes', '2-wire PT100, 430 Ω'],
          ],
        })}</h2>
        <h3 class="column-title">Panel${tip({
          lead: 'Encoder, buttons, and LED relays.',
          rows: [
            ['Encoder', 'BCM 27 and 22'],
            ['Buttons', 'BCM 5 and 6'],
            ['Start LED', 'BCM 24'],
            ['Stop LED', 'BCM 23'],
          ],
        })}</h3>
        ${panelFields()}
        <h3 class="column-title wiring-subtitle">SPI and I2C${tip({
          lead: 'Shared buses. Every pin on this page is unique.',
          rows: [
            ['SPI', 'CLK 11, SDO 9, SDI 10'],
            ['I2C', 'SDA 2, SCL 3'],
            ['HTS221 off', 'Cabinet interlock is off'],
            ['LCD off', 'Display stays closed'],
          ],
        })}</h3>
        ${busFields()}
      </section>
      <div class="dashboard-card settings-save">
        <p class="form-error" id="settings-error" hidden></p>
        <p class="saved" id="settings-saved" hidden>Saved</p>
        <div class="status-actions">
          <button class="action-btn start-btn" type="submit">Save settings</button>
        </div>
      </div>
      <section class="dashboard-card reset-panel">
        <h2 class="card-title with-tip">Data${tip({
          lead: 'Erases history and restores defaults. This cannot be undone.',
          rows: [
            ['Kept', 'Element hours'],
            ['Restored', 'Target, devices, pins, source'],
            ['Heaters', 'Turn off'],
            ['Dev', 'Pot returns to room temperature'],
          ],
        })}</h2>
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
      'input:not([name="target_temp_f"]):not([name="cabinet_limit_f"]):not([name="heater_min_on_seconds"]):not([name="heater_min_off_seconds"]):not([name="early_cutoff_f"]):not([name="update_source"]), select',
    )) {
      input.disabled = isLocked
    }
  }

  function closeTips() {
    for (const button of form.querySelectorAll('.tip.is-open')) {
      button.classList.remove('is-open')
      button.setAttribute('aria-expanded', 'false')
    }
  }

  function placeTip(button) {
    const bubble = button.querySelector('.tip-bubble')
    if (!bubble) return
    const margin = 12
    const width = Math.min(228, window.innerWidth - margin * 2)
    bubble.style.width = `${width}px`
    bubble.style.left = '0px'
    bubble.style.right = 'auto'
    const rect = button.getBoundingClientRect()
    const overflow = rect.left + width - (window.innerWidth - margin)
    const shift = Math.max(0, Math.min(overflow, width - 24))
    if (shift) bubble.style.left = `${-shift}px`
    bubble.style.setProperty('--tip-caret', `${7 + shift}px`)
  }

  function onDocumentClick(event) {
    if (!event.target.closest('.tip')) closeTips()
  }

  function onDocumentKey(event) {
    if (event.key === 'Escape') closeTips()
  }

  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onDocumentKey)
  form.addEventListener('mouseover', (event) => {
    const button = event.target.closest('.tip')
    if (button) placeTip(button)
  })
  form.addEventListener('focusin', (event) => {
    const button = event.target.closest('.tip')
    if (button) placeTip(button)
  })

  form.addEventListener('change', (event) => {
    const name = event.target.name || ''
    const rtd = name.match(/^sensor_rtd_(\d+)$/)
    if (rtd) applyStockReference(form, Number(rtd[1]))
    if (name.startsWith('i2c_') && event.target.type === 'checkbox') syncI2cLabels(form)
    const kind = name.match(/^(heater|sensor)_enabled_/)?.[1]
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
    const tipButton = event.target.closest('.tip')
    if (tipButton) {
      event.preventDefault()
      placeTip(tipButton)
      const willOpen = !tipButton.classList.contains('is-open')
      closeTips()
      tipButton.classList.toggle('is-open', willOpen)
      tipButton.setAttribute('aria-expanded', willOpen ? 'true' : 'false')
      return
    }
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
      update_source: form.elements.update_source.value === 'beta' ? 'beta' : 'main',
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
    if (document.activeElement?.name !== 'update_source') {
      form.elements.update_source.value = settings.update_source === 'beta' ? 'beta' : 'main'
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
    document.removeEventListener('click', onDocumentClick)
    document.removeEventListener('keydown', onDocumentKey)
  }
}

function settingRow(title, help, control) {
  return `
    <div class="setting-row">
      <div class="setting-copy">
        <h3>${title}${tip(help)}</h3>
      </div>
      <div class="setting-control">${control}</div>
    </div>`
}

function tip({ lead, rows }, id) {
  const bubbleId = id || `tip-${tipCounter += 1}`
  const items = (rows || []).map(([label, value]) =>
    `<span class="tip-row"><span class="tip-k">${label}</span><span class="tip-v">${value}</span></span>`,
  ).join('')
  return `<button type="button" class="tip" aria-label="Details" aria-describedby="${bubbleId}" aria-expanded="false"><span aria-hidden="true">i</span><span class="tip-bubble" id="${bubbleId}" role="tooltip"><span class="tip-lead">${lead}</span><span class="tip-rows">${items}</span></span></button>`
}

let tipCounter = 0

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
