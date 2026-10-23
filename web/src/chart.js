import Chart from 'chart.js/auto'
import { stableTempBounds } from './temp-scale.js'

const SENSOR_COLORS = ['#7dd3fc', '#fbbf24']
const GRID = 'rgba(255, 255, 255, 0.06)'
const TICK = '#93a0b5'

Chart.defaults.color = TICK
Chart.defaults.borderColor = GRID
Chart.defaults.font.family = "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"

const NARROW = '(max-width: 800px)'
const START_LINE = '#34d399'
const STOP_LINE = '#f87171'
const MARK_LINE = '#c4b5fd'
const PROJECTED_LINE = '#e879f9'
const OFF_GAP_MS = 30000
const OFF_LABEL = '#93a0b5'

function offSpans(points, gapMs) {
  const spans = []
  for (let index = 1; index < points.length; index += 1) {
    const from = points[index - 1].at
    const to = points[index].at
    if (!Number.isFinite(from) || !Number.isFinite(to) || to - from <= gapMs) continue
    spans.push({ from, to })
  }
  return spans
}

const runMarkersPlugin = {
  id: 'runMarkers',
  afterDatasetsDraw(chart) {
    const markers = chart.$markers
    const area = chart.chartArea
    if (!markers?.length || !area) return
    const xScale = chart.scales.x
    const ctx = chart.ctx
    ctx.save()
    ctx.beginPath()
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top)
    ctx.clip()
    markers.forEach((marker, index) => {
      const x = xScale.getPixelForValue(marker.at)
      if (!Number.isFinite(x) || x < area.left || x > area.right) return
      const start = marker.kind === 'start'
      const stop = marker.kind === 'stop'
      const color = start ? START_LINE : stop ? STOP_LINE : MARK_LINE
      ctx.beginPath()
      ctx.strokeStyle = color
      ctx.lineWidth = 1.5
      ctx.setLineDash([4, 3])
      ctx.moveTo(x, area.top)
      ctx.lineTo(x, area.bottom)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = color
      ctx.font = "11px Inter, ui-sans-serif, system-ui, sans-serif"
      ctx.textBaseline = 'top'
      const label = marker.label || (start ? 'Start' : stop ? 'Stop' : 'Mark')
      const width = ctx.measureText(label).width
      const placeLeft = start
      ctx.textAlign = placeLeft ? 'right' : 'left'
      let textX = placeLeft ? x - 6 : x + 6
      if (placeLeft && textX - width < area.left + 2) {
        ctx.textAlign = 'left'
        textX = Math.min(x + 6, area.right - width - 2)
      }
      if (!placeLeft && textX + width > area.right - 2) {
        ctx.textAlign = 'right'
        textX = Math.max(x - 6, area.left + width + 2)
      }
      ctx.fillText(label, textX, area.top + 4 + (index % 4) * 14)
    })
    ctx.restore()
  },
}

function spanPixels(span, xScale, area) {
  const start = xScale.getPixelForValue(span.from)
  const end = xScale.getPixelForValue(span.to)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  let left = Math.max(area.left, Math.min(start, end))
  let right = Math.min(area.right, Math.max(start, end))
  // A collapsed outage can be only a sliver of a long window. Keep the break
  // wide enough to read, without changing where the samples sit.
  const minBand = 28
  if (right - left < minBand) {
    const mid = Math.min(area.right - minBand / 2, Math.max(area.left + minBand / 2, (left + right) / 2))
    left = mid - minBand / 2
    right = mid + minBand / 2
  }
  if (right - left < 1) return null
  return { left, right }
}

function offLegendHidden(chart) {
  const index = chart.data.datasets.findIndex((dataset) => dataset.label === 'Off')
  if (index < 0) return false
  return Boolean(chart.getDatasetMeta(index).hidden)
}

const offSpansPlugin = {
  id: 'offSpans',
  beforeDatasetsDraw(chart) {
    const spans = chart.$offSpans
    const area = chart.chartArea
    if (!spans?.length || !area || offLegendHidden(chart)) return
    const xScale = chart.scales.x
    const ctx = chart.ctx
    ctx.save()
    ctx.beginPath()
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top)
    ctx.clip()
    for (const span of spans) {
      const band = spanPixels(span, xScale, area)
      if (!band) continue
      ctx.fillStyle = 'rgba(147, 160, 181, 0.14)'
      ctx.fillRect(band.left, area.top, band.right - band.left, area.bottom - area.top)
    }
    ctx.restore()
  },
  afterDatasetsDraw(chart) {
    const spans = chart.$offSpans
    const area = chart.chartArea
    if (!spans?.length || !area || offLegendHidden(chart)) return
    const xScale = chart.scales.x
    const ctx = chart.ctx
    ctx.save()
    ctx.beginPath()
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top)
    ctx.clip()
    ctx.fillStyle = OFF_LABEL
    ctx.font = "12px Inter, ui-sans-serif, system-ui, sans-serif"
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    const midY = (area.top + area.bottom) / 2
    for (const span of spans) {
      const band = spanPixels(span, xScale, area)
      if (!band) continue
      const text = span.label || 'Off'
      const width = ctx.measureText(text).width
      let x = (band.left + band.right) / 2
      x = Math.min(area.right - width / 2 - 4, Math.max(area.left + width / 2 + 4, x))
      ctx.fillStyle = 'rgba(16, 20, 27, 0.88)'
      ctx.fillRect(x - width / 2 - 5, midY - 9, width + 10, 18)
      ctx.fillStyle = OFF_LABEL
      ctx.fillText(text, x, midY)
    }
    ctx.restore()
  },
}

const WINDOW_FILL = 'rgba(142, 162, 255, 0.18)'
const WINDOW_EDGE = 'rgba(142, 162, 255, 0.95)'
const PREVIEW_FILL = 'rgba(142, 162, 255, 0.32)'

function bandFor(chart, span) {
  const elapsed = chart.$elapsed
  const area = chart.chartArea
  const xScale = chart.scales?.x
  if (!span || !elapsed?.length || !area || !xScale) return null
  const start = Math.min(Number(span.from), Number(span.to))
  const end = Math.max(Number(span.from), Number(span.to))
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  let first = -1
  let last = -1
  elapsed.forEach((value, index) => {
    if (!Number.isFinite(value) || value < start - 0.05 || value > end + 0.05) return
    if (first < 0) first = index
    last = index
  })
  if (first < 0) return null
  let left = xScale.getPixelForValue(first)
  let right = xScale.getPixelForValue(last)
  if (!Number.isFinite(left) || !Number.isFinite(right)) return null
  if (first > 0) {
    const previous = xScale.getPixelForValue(first - 1)
    if (Number.isFinite(previous)) left -= (left - previous) / 2
  }
  if (last < elapsed.length - 1) {
    const next = xScale.getPixelForValue(last + 1)
    if (Number.isFinite(next)) right += (next - right) / 2
  } else if (last > first) {
    const previous = xScale.getPixelForValue(last - 1)
    if (Number.isFinite(previous)) right += (right - previous) / 2
  }
  if (right < left) {
    const swap = left
    left = right
    right = swap
  }
  if (right - left < 10) {
    const mid = (left + right) / 2
    left = mid - 5
    right = mid + 5
  }
  left = Math.max(area.left, left)
  right = Math.min(area.right, right)
  if (right - left < 1) return null
  return { left, right }
}

function paintBand(ctx, area, band, fill, edge) {
  ctx.fillStyle = fill
  ctx.fillRect(band.left, area.top, band.right - band.left, area.bottom - area.top)
  ctx.strokeStyle = edge
  ctx.lineWidth = 1.5
  ctx.beginPath()
  ctx.moveTo(band.left, area.top)
  ctx.lineTo(band.left, area.bottom)
  ctx.moveTo(band.right, area.top)
  ctx.lineTo(band.right, area.bottom)
  ctx.stroke()
}

const windowBandPlugin = {
  id: 'windowBand',
  beforeDatasetsDraw(chart) {
    const area = chart.chartArea
    if (!area) return
    const ctx = chart.ctx
    ctx.save()
    ctx.beginPath()
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top)
    ctx.clip()
    const selected = bandFor(chart, chart.$window)
    if (selected) paintBand(ctx, area, selected, WINDOW_FILL, WINDOW_EDGE)
    const preview = bandFor(chart, chart.$windowPreview)
    if (preview) paintBand(ctx, area, preview, PREVIEW_FILL, WINDOW_EDGE)
    ctx.restore()
  },
}

Chart.register(offSpansPlugin, windowBandPlugin, runMarkersPlugin)

function clockLabel(at, spanMs) {
  const date = new Date(at)
  if (spanMs >= 24 * 60 * 60 * 1000) {
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    })
  }
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
}

function axisLabel(at, spanMs) {
  const date = new Date(at)
  if (!spanMs || spanMs < 24 * 60 * 60 * 1000) {
    return date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  }
  if (spanMs < 14 * 24 * 60 * 60 * 1000) {
    return date.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric' })
  }
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function breakAtSpans(points, spans) {
  if (!spans?.length || points.length < 2) return points
  const ordered = [...spans].sort((left, right) => left.from - right.from)
  const out = []
  let spanIndex = 0
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index]
    out.push(point)
    const next = points[index + 1]
    if (!next) break
    while (spanIndex < ordered.length && ordered[spanIndex].to <= point.at) spanIndex += 1
    const span = ordered[spanIndex]
    if (!span || point.break || next.break) continue
    if (point.at <= span.from + 1 && next.at >= span.to - 1) {
      out.push({ at: (span.from + span.to) / 2, temps: [], target: null, break: true })
      spanIndex += 1
    }
  }
  return out
}

function applyChartLayout(chart) {
  const narrow = window.matchMedia(NARROW).matches
  const { options } = chart
  options.layout.padding = narrow
    ? { top: 4, right: 2, bottom: 0, left: 0 }
    : { top: 8, right: 12, bottom: 4, left: 4 }
  options.plugins.legend.labels.boxWidth = narrow ? 8 : 10
  options.plugins.legend.labels.boxHeight = narrow ? 8 : 10
  options.plugins.legend.labels.padding = narrow ? 8 : 12
  options.plugins.legend.labels.font = { size: narrow ? 11 : 12 }
  options.scales.y.title.display = !narrow
  options.scales.y.ticks.maxTicksLimit = narrow ? 5 : 8
  options.scales.x.title.display = !narrow
  options.scales.x.ticks.maxTicksLimit = narrow ? 4 : 8
  options.scales.x.ticks.maxRotation = 0
  options.scales.x.ticks.minRotation = 0
  options.interaction.mode = narrow ? 'index' : 'nearest'
  chart.update('none')
}

export function createTempChart(canvas, { timed = false } = {}) {
  const chart = new Chart(canvas, {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { top: 8, right: 12, bottom: 4, left: 4 } },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: { color: '#d5dbe6', boxWidth: 10, boxHeight: 10, usePointStyle: true },
        },
        tooltip: {
          mode: 'index',
          intersect: false,
          backgroundColor: '#1c2330',
          titleColor: '#f4f7fb',
          bodyColor: '#d5dbe6',
          borderColor: 'rgba(255, 255, 255, 0.08)',
          borderWidth: 1,
          padding: 10,
          filter(item) {
            return item.dataset.label !== 'Off'
          },
          callbacks: timed
            ? {
                title(items) {
                  const item = items[0]
                  const value = item?.parsed?.x
                  if (!Number.isFinite(value)) return ''
                  const host = item.chart
                  const real = host.$toReal ? host.$toReal(value) : value
                  return clockLabel(real, host.$spanMs)
                },
              }
            : {},
        },
      },
      scales: {
        y: {
          bounds: 'data',
          title: { display: true, text: 'Temperature (°F)', color: TICK },
          ticks: { color: TICK },
          grid: { color: GRID },
          border: { display: false },
        },
        x: timed
          ? {
              type: 'linear',
              offset: false,
              bounds: 'data',
              title: { display: true, text: 'Time', color: TICK },
              ticks: {
                color: TICK,
                maxTicksLimit: 8,
                maxRotation: 0,
                minRotation: 0,
                callback(value) {
                  const host = this.chart
                  const real = host.$toReal ? host.$toReal(value) : value
                  if (host.$hideTick?.(real)) return ''
                  return axisLabel(real, host.$spanMs)
                },
              },
              grid: { display: false },
              border: { display: false },
            }
          : {
              title: { display: true, text: 'Time', color: TICK },
              ticks: { color: TICK, maxTicksLimit: 8, maxRotation: 0, minRotation: 0 },
              grid: { display: false },
              border: { display: false },
            },
      },
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
    },
  })
  const media = window.matchMedia(NARROW)
  const onChange = () => applyChartLayout(chart)
  media.addEventListener('change', onChange)
  const destroy = chart.destroy.bind(chart)
  chart.destroy = () => {
    media.removeEventListener('change', onChange)
    destroy()
  }
  chart.$timed = timed
  chart.$markers = []
  chart.$offSpans = []
  applyChartLayout(chart)
  return chart
}

function datasetExtent(datasets) {
  let low = Infinity
  let high = -Infinity
  for (const dataset of datasets) {
    if (!dataset || dataset.label === 'Off' || !dataset.data) continue
    for (const value of dataset.data) {
      const y = value != null && typeof value === 'object' ? value.y : value
      if (!Number.isFinite(y)) continue
      if (y < low) low = y
      if (y > high) high = y
    }
  }
  if (!Number.isFinite(low) || !Number.isFinite(high)) return null
  return { low, high }
}

function applyTemperatureScale(chart, datasets) {
  const scale = chart.options.scales.y
  const extent = datasetExtent(datasets)
  if (!extent) {
    scale.min = undefined
    scale.max = undefined
    chart.$yBounds = null
    return
  }
  const bounds = stableTempBounds(extent.low, extent.high, chart.$yBounds)
  chart.$yBounds = bounds
  scale.min = bounds.min
  scale.max = bounds.max
}

function pointValue(point, index, timed) {
  if (!timed) return point.break ? null : (point.temps[index] ?? null)
  if (point.break) return { x: point.at, y: null }
  return { x: point.at, y: point.temps[index] ?? null }
}

export function setChartWindow(chart, span) {
  chart.$window = span && Number.isFinite(Number(span.from)) && Number.isFinite(Number(span.to))
    ? { from: Number(span.from), to: Number(span.to) }
    : null
  chart.update('none')
}

function elapsedAt(chart, pixel) {
  const scale = chart.scales?.x
  const elapsed = chart.$elapsed || []
  if (!scale || !elapsed.length || !Number.isFinite(pixel)) return null
  const index = scale.getValueForPixel(pixel)
  if (!Number.isFinite(index)) return null
  const clamped = Math.max(0, Math.min(elapsed.length - 1, Math.round(index)))
  const value = Number(elapsed[clamped])
  return Number.isFinite(value) ? value : null
}

export function bindChartWindowSelect(chart, onSelect) {
  const canvas = chart.canvas
  let origin = null
  let moved = false

  function clearPreview() {
    chart.$windowPreview = null
    canvas.classList.remove('is-selecting')
  }

  function onDown(event) {
    if (event.button != null && event.button !== 0) return
    origin = event.offsetX
    moved = false
    if (canvas.setPointerCapture) canvas.setPointerCapture(event.pointerId)
  }

  function onMove(event) {
    if (origin == null) return
    if (Math.abs(event.offsetX - origin) < 8) return
    moved = true
    const from = elapsedAt(chart, origin)
    const to = elapsedAt(chart, event.offsetX)
    if (from == null || to == null) return
    chart.$windowPreview = { from, to }
    canvas.classList.add('is-selecting')
    chart.update('none')
  }

  function onUp(event) {
    if (origin == null) return
    const from = elapsedAt(chart, origin)
    const to = elapsedAt(chart, event.offsetX)
    const choose = moved && from != null && to != null && Math.abs(to - from) >= 0.5
    origin = null
    moved = false
    clearPreview()
    if (choose) onSelect({ from: Math.min(from, to), to: Math.max(from, to) })
    else chart.update('none')
  }

  function onCancel() {
    if (origin == null) return
    origin = null
    moved = false
    clearPreview()
    chart.update('none')
  }

  canvas.addEventListener('pointerdown', onDown)
  canvas.addEventListener('pointermove', onMove)
  canvas.addEventListener('pointerup', onUp)
  canvas.addEventListener('pointercancel', onCancel)
  return () => {
    canvas.removeEventListener('pointerdown', onDown)
    canvas.removeEventListener('pointermove', onMove)
    canvas.removeEventListener('pointerup', onUp)
    canvas.removeEventListener('pointercancel', onCancel)
  }
}

export function setChartPoints(chart, points, sensorNames, markers = [], view = null) {
  chart.$markers = markers
  chart.$elapsed = points.map((point) => {
    const value = Number(point.elapsed)
    return Number.isFinite(value) ? value : null
  })
  const timed = Boolean(chart.$timed)
  const spans = view ? view.spans || [] : timed ? offSpans(points, OFF_GAP_MS) : []
  chart.$offSpans = spans
  chart.$toReal = view?.toReal || null
  chart.$spanMs = view?.spanMs || null
  chart.$hideTick = view?.hideTick || null
  const projection = timed && view?.projection?.length ? view.projection : []
  const plotted = view?.connect ? breakAtSpans(points, spans) : points
  const gapSpan = view?.connect ? false : OFF_GAP_MS
  const datasets = sensorNames.map((name, index) => ({
    label: `${name} (°F)`,
    data: plotted.map((point) => pointValue(point, index, timed)),
    borderColor: SENSOR_COLORS[index % SENSOR_COLORS.length],
    backgroundColor: index === 0 ? 'rgba(125, 211, 252, 0.16)' : 'rgba(251, 191, 36, 0.08)',
    borderWidth: 2,
    fill: index === 0,
    tension: 0.25,
    pointRadius: 0,
    pointHoverRadius: 4,
    spanGaps: timed ? gapSpan : true,
  }))
  if (plotted.length) {
    datasets.push({
      label: 'Target (°F)',
      data: plotted.map((point) => {
        if (!timed) return point.break ? null : point.target
        if (point.break) return { x: point.at, y: null }
        return { x: point.at, y: point.target }
      }),
      borderColor: '#64748b',
      borderDash: [6, 4],
      borderWidth: 2,
      pointRadius: 0,
      fill: false,
      tension: 0,
      spanGaps: timed ? gapSpan : true,
    })
  }
  if (projection.length) {
    datasets.push({
      label: 'Projected (°F)',
      data: projection.map((point) => ({ x: point.at, y: point.temp })),
      borderColor: PROJECTED_LINE,
      borderDash: [5, 4],
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      fill: false,
      tension: 0,
      spanGaps: false,
    })
  }
  if (spans.length) {
    datasets.push({
      label: 'Off',
      data: [],
      borderColor: 'rgba(147, 160, 181, 0.45)',
      backgroundColor: 'rgba(147, 160, 181, 0.16)',
      pointStyle: 'rect',
      pointRadius: 0,
      pointHoverRadius: 0,
      borderWidth: 0,
    })
  }
  chart.data.labels = timed ? [] : plotted.map((point) => point.label)
  chart.data.datasets = datasets
  if (timed) {
    const scale = chart.options.scales.x
    if (view && Number.isFinite(view.min) && Number.isFinite(view.max)) {
      scale.min = view.min
      scale.max = view.max > view.min ? view.max : view.min + 1000
    } else if (points.length) {
      let start = points[0].at
      let end = start
      for (const point of points) {
        if (point.at < start) start = point.at
        if (point.at > end) end = point.at
      }
      scale.min = start
      scale.max = end > start ? end : start + 1000
    } else {
      scale.min = undefined
      scale.max = undefined
    }
  }
  applyTemperatureScale(chart, datasets)
  chart.update()
}
