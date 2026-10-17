import { api } from '../api.js'
import { setSourceBadge } from '../layout.js'

export function mountSettings(root) {
  root.innerHTML = `
    <form class="dashboard-card settings-card" id="settings-form">
      <div class="card-title-with-timer">
        <h2 class="card-title">Settings</h2>
      </div>
      <p class="lock-banner" id="lock-banner" hidden>
        Heater and sensor setup is locked while a run is open. The target temperature can still be changed.
      </p>
      <fieldset>
        <legend>Heaters</legend>
        <div class="choice-row">
          <label><input type="radio" name="heater_count" value="1" /> One heater</label>
          <label><input type="radio" name="heater_count" value="2" /> Two heaters</label>
        </div>
        <label class="field">Heater 1 name <input name="heater_name_1" maxlength="40" required /></label>
        <label class="field" id="heater-2-field">Heater 2 name <input name="heater_name_2" maxlength="40" required /></label>
      </fieldset>
      <fieldset>
        <legend>Temperature sensors</legend>
        <div class="choice-row">
          <label><input type="radio" name="sensor_count" value="1" /> One sensor</label>
          <label><input type="radio" name="sensor_count" value="2" /> Two sensors</label>
        </div>
        <label class="field">Sensor 1 name <input name="sensor_name_1" maxlength="40" required /></label>
        <label class="field" id="sensor-2-field">Sensor 2 name <input name="sensor_name_2" maxlength="40" required /></label>
        <p class="help">Sensor 1 is the probe the controller follows. Sensor 2 is recorded and shown, and it does not decide when the heaters fire.</p>
      </fieldset>
      <label class="field">Default target (°F)
        <input name="target_temp_f" id="settings-target" type="number" min="40" max="212" step="0.1" required />
      </label>
      <p class="help">These names and counts are copied onto each run when it starts. Readings on this controller come from the dummy source until real sensors are connected.</p>
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

  function selectedCount(name) {
    return Number(form.querySelector(`input[name="${name}"]:checked`)?.value || 1)
  }

  function applyCountState() {
    const heater2 = selectedCount('heater_count') === 1
    const sensor2 = selectedCount('sensor_count') === 1
    form.elements.heater_name_2.disabled = locked || heater2
    form.elements.sensor_name_2.disabled = locked || sensor2
    document.getElementById('heater-2-field').classList.toggle('is-unused', heater2)
    document.getElementById('sensor-2-field').classList.toggle('is-unused', sensor2)
  }

  function applyLock(isLocked) {
    locked = isLocked
    document.getElementById('lock-banner').hidden = !isLocked
    for (const input of form.querySelectorAll('input[name="heater_count"], input[name="sensor_count"]')) {
      input.disabled = isLocked
    }
    form.elements.heater_name_1.disabled = isLocked
    form.elements.sensor_name_1.disabled = isLocked
    applyCountState()
  }

  form.addEventListener('change', (event) => {
    if (event.target.name === 'heater_count' || event.target.name === 'sensor_count') applyCountState()
  })

  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    savedEl.hidden = true
    errorEl.hidden = true
    const body = {
      heater_count: selectedCount('heater_count'),
      sensor_count: selectedCount('sensor_count'),
      heater_names: [form.elements.heater_name_1.value, form.elements.heater_name_2.value],
      sensor_names: [form.elements.sensor_name_1.value, form.elements.sensor_name_2.value],
      target_temp_f: Number(form.elements.target_temp_f.value),
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

  function fill(settings) {
    form.querySelector(`input[name="heater_count"][value="${settings.heater_count}"]`).checked = true
    form.querySelector(`input[name="sensor_count"][value="${settings.sensor_count}"]`).checked = true
    form.elements.heater_name_1.value = settings.heater_names[0]
    form.elements.heater_name_2.value = settings.heater_names[1]
    form.elements.sensor_name_1.value = settings.sensor_names[0]
    form.elements.sensor_name_2.value = settings.sensor_names[1]
    if (document.activeElement !== form.elements.target_temp_f) {
      form.elements.target_temp_f.value = String(settings.target_temp_f)
    }
    applyCountState()
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
