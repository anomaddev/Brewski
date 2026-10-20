// Axis math for the controller timeline. Thresholds match src/brewski/timeline.py.

export const GAP_AFTER_MS = 30 * 1000
export const CONDENSE_AFTER_MS = 30 * 60 * 1000
export const CONDENSED_MS = 2 * 60 * 1000

const MINUTE = 60 * 1000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export const TIMELINE_SPANS = [
  { ms: 5 * MINUTE, label: '5 min' },
  { ms: 10 * MINUTE, label: '10 min' },
  { ms: 20 * MINUTE, label: '20 min' },
  { ms: HOUR, label: '1 hour' },
  { ms: 3 * HOUR, label: '3 hours' },
  { ms: 6 * HOUR, label: '6 hours' },
  { ms: 12 * HOUR, label: '12 hours' },
  { ms: DAY, label: '1 day' },
  { ms: 3 * DAY, label: '3 days' },
  { ms: 7 * DAY, label: '1 week' },
  { ms: 30 * DAY, label: '1 month' },
  { ms: 90 * DAY, label: '3 months' },
  { ms: 365 * DAY, label: '1 year' },
  { ms: 5 * 365 * DAY, label: '5 years' },
]

export const DEFAULT_SPAN_INDEX = 2

export function shownMs(durationMs) {
  if (durationMs > CONDENSE_AFTER_MS) return CONDENSED_MS
  return durationMs
}

export function mergeGaps(gaps) {
  const sorted = [...gaps].sort((a, b) => a.from - b.from)
  const merged = []
  for (const gap of sorted) {
    if (!Number.isFinite(gap.from) || !Number.isFinite(gap.to) || gap.to <= gap.from) continue
    const last = merged[merged.length - 1]
    if (last && gap.from <= last.to) last.to = Math.max(last.to, gap.to)
    else merged.push({ from: gap.from, to: gap.to })
  }
  return merged
}

export function toDisplay(moment, gaps) {
  let shrink = 0
  for (const gap of gaps) {
    if (moment <= gap.from) break
    const duration = gap.to - gap.from
    if (duration <= 0) continue
    const shown = shownMs(duration)
    if (moment >= gap.to) {
      shrink += duration - shown
      continue
    }
    const into = moment - gap.from
    shrink += into - (into / duration) * shown
    break
  }
  return moment - shrink
}

export function toReal(display, gaps, lo, hi) {
  let low = lo
  let high = hi
  if (high < low) [low, high] = [high, low]
  if (toDisplay(low, gaps) >= display) return low
  if (toDisplay(high, gaps) <= display) return high
  let left = low
  let right = high
  for (let step = 0; step < 64; step += 1) {
    const mid = (left + right) / 2
    if (toDisplay(mid, gaps) < display) left = mid
    else right = mid
  }
  return Math.round(right)
}

export function clampEnd(end, spanMs, gaps, earliest, now) {
  const capped = Math.min(end, now)
  if (earliest == null) return capped
  const history = toDisplay(now, gaps) - toDisplay(earliest, gaps)
  if (history <= spanMs) return capped
  const minEndDisplay = toDisplay(earliest, gaps) + spanMs * 0.15
  if (toDisplay(capped, gaps) < minEndDisplay) {
    return toReal(minEndDisplay, gaps, earliest, now)
  }
  return capped
}

export function formatOff(durationMs) {
  const minutes = Math.max(1, Math.round(durationMs / MINUTE))
  if (minutes < 60) return `Off ${minutes}m`
  const hours = Math.floor(minutes / 60)
  const remain = minutes % 60
  if (hours < 48) return remain ? `Off ${hours}h ${remain}m` : `Off ${hours}h`
  const days = Math.floor(hours / 24)
  const remHours = hours % 24
  return remHours ? `Off ${days}d ${remHours}h` : `Off ${days}d`
}

export function bindTimelinePan(canvas, onPixels) {
  let drag = null
  canvas.style.touchAction = 'pan-y'

  function finish(event) {
    if (!drag || event.pointerId !== drag.pointerId) return
    if (drag.active) {
      canvas.releasePointerCapture(event.pointerId)
      canvas.classList.remove('is-panning')
    }
    drag = null
  }

  function down(event) {
    if (event.button !== 0) return
    drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      last: event.clientX,
      active: false,
    }
  }

  function move(event) {
    if (!drag || event.pointerId !== drag.pointerId) return
    const dx = event.clientX - drag.x
    const dy = event.clientY - drag.y
    if (!drag.active) {
      if (Math.hypot(dx, dy) < 6) return
      if (Math.abs(dy) > Math.abs(dx)) {
        drag = null
        return
      }
      drag.active = true
      canvas.setPointerCapture(event.pointerId)
      canvas.classList.add('is-panning')
    }
    const step = event.clientX - drag.last
    drag.last = event.clientX
    if (step) onPixels(step)
  }

  function wheel(event) {
    const horizontal = Math.abs(event.deltaX) > Math.abs(event.deltaY) || event.shiftKey
    if (!horizontal) return
    event.preventDefault()
    const delta = Math.abs(event.deltaX) >= Math.abs(event.deltaY) ? event.deltaX : event.deltaY
    if (delta) onPixels(-delta)
  }

  canvas.addEventListener('pointerdown', down)
  canvas.addEventListener('pointermove', move)
  canvas.addEventListener('pointerup', finish)
  canvas.addEventListener('pointercancel', finish)
  canvas.addEventListener('wheel', wheel, { passive: false })

  return () => {
    canvas.removeEventListener('pointerdown', down)
    canvas.removeEventListener('pointermove', move)
    canvas.removeEventListener('pointerup', finish)
    canvas.removeEventListener('pointercancel', finish)
    canvas.removeEventListener('wheel', wheel)
  }
}
