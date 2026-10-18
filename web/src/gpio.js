import { escapeHtml } from './format.js'

const PIN_FIELDS = [
  ['heater_gpio_0', 'heater 1', 'heater'],
  ['heater_gpio_1', 'heater 2', 'heater'],
  ['sensor_gpio_0', 'probe 1', 'probe'],
  ['sensor_gpio_1', 'probe 2', 'probe'],
  ['spi_clk', 'SPI CLK', 'spi'],
  ['spi_sdo', 'SPI SDO', 'spi'],
  ['spi_sdi', 'SPI SDI', 'spi'],
  ['i2c_sda', 'I2C SDA', 'i2c'],
  ['i2c_scl', 'I2C SCL', 'i2c'],
  ['encoder_a', 'encoder A', 'panel'],
  ['encoder_b', 'encoder B', 'panel'],
  ['led_start', 'start LED relay', 'panel'],
  ['led_stop', 'stop LED relay', 'panel'],
  ['button_start', 'start button', 'panel'],
  ['button_stop', 'stop button', 'panel'],
]

function pinValue(form, name) {
  return Math.round(Number(form.elements[name].value))
}

function bcmList(pins) {
  const labels = pins.map(String)
  if (labels.length <= 1) return labels[0] || ''
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`
  return `${labels.slice(0, -1).join(', ')}, and ${labels[labels.length - 1]}`
}

export function validatePins(form) {
  const pins = PIN_FIELDS.map(([name, label, group]) => ({
    name,
    label,
    group,
    value: pinValue(form, name),
  }))
  for (const pin of pins) {
    if (!Number.isInteger(pin.value) || pin.value < 2 || pin.value > 27) {
      const kind = pin.group === 'probe' ? 'chip select' : 'BCM pin'
      return `Each ${kind} must be a whole number from 2 to 27.`
    }
  }
  const claimed = new Map()
  for (const pin of pins) {
    if (pin.group === 'heater' || pin.group === 'probe') continue
    if (claimed.has(pin.value)) return `BCM ${pin.value} is shared by ${claimed.get(pin.value).label} and ${pin.label}.`
    claimed.set(pin.value, pin)
  }
  const spi = new Set(pins.filter((pin) => pin.group === 'spi').map((pin) => pin.value))
  const heaters = pins.filter((pin) => pin.group === 'heater')
  const probes = pins.filter((pin) => pin.group === 'probe')
  for (const pin of [...heaters, ...probes]) {
    if (!spi.has(pin.value)) continue
    const listed = [...spi].sort((left, right) => left - right)
    const verb = listed.length === 1 ? 'is' : 'are'
    return `BCM ${bcmList(listed)} ${verb} the SPI bus shared by the temperature probes.`
  }
  if (new Set(heaters.map((pin) => pin.value)).size !== heaters.length) {
    return 'Each heater needs its own GPIO pin.'
  }
  if (new Set(probes.map((pin) => pin.value)).size !== probes.length) {
    return 'Each temperature probe needs its own chip select.'
  }
  const heaterValues = new Set(heaters.map((pin) => pin.value))
  if (probes.some((pin) => heaterValues.has(pin.value))) {
    return 'A heater and a probe cannot share a GPIO pin.'
  }
  for (const pin of [...heaters, ...probes]) {
    if (claimed.has(pin.value)) return `BCM ${pin.value} is shared by ${claimed.get(pin.value).label} and ${pin.label}.`
  }
  return ''
}

export function readGpio(form) {
  return {
    spi: {
      clk: pinValue(form, 'spi_clk'),
      sdo: pinValue(form, 'spi_sdo'),
      sdi: pinValue(form, 'spi_sdi'),
    },
    i2c: {
      sda: pinValue(form, 'i2c_sda'),
      scl: pinValue(form, 'i2c_scl'),
    },
    i2c_devices: ['hts221', 'lcd'].map((id) => ({
      id,
      name: form.elements[`i2c_${id}_name`].value.trim(),
      enabled: form.elements[`i2c_${id}_enabled`].checked,
    })),
    encoder: {
      a: pinValue(form, 'encoder_a'),
      b: pinValue(form, 'encoder_b'),
    },
    leds: {
      start: pinValue(form, 'led_start'),
      stop: pinValue(form, 'led_stop'),
    },
    buttons: {
      start: pinValue(form, 'button_start'),
      stop: pinValue(form, 'button_stop'),
    },
  }
}

export function writeGpio(form, gpio) {
  const values = {
    spi_clk: gpio.spi.clk,
    spi_sdo: gpio.spi.sdo,
    spi_sdi: gpio.spi.sdi,
    i2c_sda: gpio.i2c.sda,
    i2c_scl: gpio.i2c.scl,
    encoder_a: gpio.encoder.a,
    encoder_b: gpio.encoder.b,
    led_start: gpio.leds.start,
    led_stop: gpio.leds.stop,
    button_start: gpio.buttons.start,
    button_stop: gpio.buttons.stop,
  }
  for (const [name, value] of Object.entries(values)) {
    const input = form.elements[name]
    if (input && document.activeElement !== input) input.value = value == null ? '' : String(value)
  }
  for (const device of gpio.i2c_devices || []) {
    const name = form.elements[`i2c_${device.id}_name`]
    const enabled = form.elements[`i2c_${device.id}_enabled`]
    if (name) name.value = device.name
    if (enabled) enabled.checked = Boolean(device.enabled)
    const title = form.querySelector(`[data-i2c-title="${device.id}"]`)
    if (title) title.textContent = device.name
  }
  syncI2cLabels(form)
}

export function syncI2cLabels(form) {
  for (const id of ['hts221', 'lcd']) {
    const input = form.elements[`i2c_${id}_enabled`]
    if (!input) continue
    const text = form.querySelector(`[data-i2c-text="${id}"]`)
    if (text) text.textContent = input.checked ? 'Enabled' : 'Disabled'
    const section = form.querySelector(`[data-i2c="${id}"]`)
    if (section) section.classList.toggle('is-off', !input.checked)
  }
}

export function pinField(name, label, value = '') {
  const shown = value === '' || value == null ? '' : escapeHtml(value)
  return `
    <label class="field gpio-field">${label}
      <input name="${name}" type="number" min="2" max="27" step="1" inputmode="numeric" required value="${shown}" />
    </label>`
}

export function panelFields(gpio) {
  const encoder = gpio?.encoder || {}
  const buttons = gpio?.buttons || {}
  const leds = gpio?.leds || {}
  return `
    <div class="pin-grid">
      ${pinField('encoder_a', 'Encoder A', encoder.a)}
      ${pinField('encoder_b', 'Encoder B', encoder.b)}
      ${pinField('button_start', 'Start button', buttons.start)}
      ${pinField('button_stop', 'Stop button', buttons.stop)}
      ${pinField('led_start', 'Start LED relay', leds.start)}
      ${pinField('led_stop', 'Stop LED relay', leds.stop)}
    </div>`
}

export function busFields(gpio) {
  const spi = gpio?.spi || {}
  const i2c = gpio?.i2c || {}
  return `
    <div class="pin-grid">
      ${pinField('spi_clk', 'SPI CLK', spi.clk)}
      ${pinField('spi_sdo', 'SPI SDO', spi.sdo)}
      ${pinField('spi_sdi', 'SPI SDI', spi.sdi)}
      ${pinField('i2c_sda', 'I2C SDA', i2c.sda)}
      ${pinField('i2c_scl', 'I2C SCL', i2c.scl)}
    </div>
    <div class="setup-devices">${i2cSwitches(gpio)}</div>`
}

function i2cSwitches(gpio) {
  const devices = gpio?.i2c_devices || [
    { id: 'hts221', name: 'HTS221', enabled: true },
    { id: 'lcd', name: 'LCD', enabled: true },
  ]
  return devices
    .map((device) => {
      const title = escapeHtml(device.name || device.id)
      return `
        <section class="device${device.enabled ? '' : ' is-off'}" data-i2c="${device.id}">
          <div class="device-head">
            <p class="device-title" data-i2c-title="${device.id}">${title}</p>
            <label class="switch">
              <input type="checkbox" role="switch" name="i2c_${device.id}_enabled" aria-label="${title}" ${device.enabled ? 'checked' : ''} />
              <span class="switch-track" aria-hidden="true"></span>
              <span class="switch-text" data-i2c-text="${device.id}">${device.enabled ? 'Enabled' : 'Disabled'}</span>
            </label>
          </div>
          <input type="hidden" name="i2c_${device.id}_name" value="${title}" />
        </section>`
    })
    .join('')
}
