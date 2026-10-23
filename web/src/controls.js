const STEP_UP = `<svg viewBox="0 0 12 8" aria-hidden="true"><path d="M1 6.5 6 1.5l5 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`
const STEP_DOWN = `<svg viewBox="0 0 12 8" aria-hidden="true"><path d="M1 1.5 6 6.5l5-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`

export function numberStepper(input) {
  return `<span class="num-step">${input}<span class="num-step-btns"><button type="button" class="step-btn" data-step="1" tabindex="-1" aria-label="Increase">${STEP_UP}</button><button type="button" class="step-btn" data-step="-1" tabindex="-1" aria-label="Decrease">${STEP_DOWN}</button></span></span>`
}

export function bindNumberSteppers() {
  document.addEventListener('click', (event) => {
    const button = event.target.closest('.num-step [data-step]')
    if (!button) return
    const input = button.closest('.num-step')?.querySelector('input')
    if (!input || input.disabled) return
    stepNumber(input, Number(button.dataset.step))
  })
}

function stepNumber(input, direction) {
  if (direction !== 1 && direction !== -1) return
  const step = input.step === '' || input.step === 'any' ? 1 : Number(input.step)
  if (!Number.isFinite(step) || step <= 0) return
  const places = decimalPlaces(step)
  const scale = 10 ** places
  const fallback = input.min === '' ? 0 : Number(input.min)
  const current = input.value === '' ? fallback : Number(input.value)
  if (!Number.isFinite(current)) return
  let next = (Math.round(current * scale) + direction * Math.round(step * scale)) / scale
  if (input.min !== '') next = Math.max(next, Number(input.min))
  if (input.max !== '') next = Math.min(next, Number(input.max))
  const shown = places ? next.toFixed(places) : String(next)
  if (shown === input.value) return
  input.value = shown
  input.dispatchEvent(new Event('input', { bubbles: true }))
  input.dispatchEvent(new Event('change', { bubbles: true }))
}

function decimalPlaces(step) {
  const text = String(step)
  const exp = text.match(/e-(\d+)$/i)
  if (exp) return Number(exp[1])
  const fraction = text.split('.')[1]
  return fraction ? fraction.length : 0
}
