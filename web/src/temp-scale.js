// A hold within about a degree makes Chart.js refit the axis on every sample,
// so the plot collapses to a 1°F band that jumps with each reading. Keep at
// least a 10°F span, snap it to a coarse grid, and leave that window alone
// until the trace leaves it or the visible range gets much wider or narrower.

const MIN_SPAN_F = 10
const TIGHT_STEP_F = 5
const WIDE_PAD = 0.08
const SHRINK_RATIO = 0.7

function niceStep(span) {
  const rough = Math.max(span / 6, 1)
  const power = 10 ** Math.floor(Math.log10(rough))
  const fraction = rough / power
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10
  return nice * power
}

function snapDown(value, step) {
  return Math.round(Math.floor(value / step + 1e-8) * step * 1000) / 1000
}

function snapUp(value, step) {
  return Math.round(Math.ceil(value / step - 1e-8) * step * 1000) / 1000
}

export function idealTempBounds(low, high) {
  const span = high - low
  if (!(span >= MIN_SPAN_F)) {
    const pad = (MIN_SPAN_F - (span > 0 ? span : 0)) / 2
    const min = snapDown(low - pad, TIGHT_STEP_F)
    let max = snapUp(high + pad, TIGHT_STEP_F)
    if (max - min < MIN_SPAN_F) max = min + MIN_SPAN_F
    if (high > max) max = snapUp(high, TIGHT_STEP_F)
    return { min, max }
  }
  const pad = span * WIDE_PAD
  const step = niceStep(span + pad * 2)
  return {
    min: snapDown(low - pad, step),
    max: snapUp(high + pad, step),
  }
}

export function stableTempBounds(low, high, previous) {
  const bottom = Math.min(low, high)
  const top = Math.max(low, high)
  const ideal = idealTempBounds(bottom, top)
  if (!previous || !Number.isFinite(previous.min) || !Number.isFinite(previous.max) || !(previous.max > previous.min)) {
    return ideal
  }
  if (bottom < previous.min || top > previous.max) return ideal
  const dataSpan = top - bottom
  const axisSpan = previous.max - previous.min
  const idealSpan = ideal.max - ideal.min
  if (idealSpan <= axisSpan * SHRINK_RATIO && dataSpan <= axisSpan * 0.5) return ideal
  return { min: previous.min, max: previous.max }
}
