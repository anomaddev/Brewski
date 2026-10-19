import Chart from 'chart.js/auto'

const SENSOR_COLORS = ['#7dd3fc', '#fbbf24']
const GRID = 'rgba(255, 255, 255, 0.06)'
const TICK = '#93a0b5'

Chart.defaults.color = TICK
Chart.defaults.borderColor = GRID
Chart.defaults.font.family = "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"

const NARROW = '(max-width: 800px)'
const START_LINE = '#34d399'
const STOP_LINE = '#f87171'
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
    for (const marker of markers) {
      const x = xScale.getPixelForValue(marker.at)
      if (!Number.isFinite(x) || x < area.left || x > area.right) return
      const start = marker.kind === 'start'
      ctx.beginPath()
      ctx.strokeStyle = start ? START_LINE : STOP_LINE
      ctx.lineWidth = 1.5
      ctx.setLineDash([4, 3])
      ctx.moveTo(x, area.top)
      ctx.lineTo(x, area.bottom)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = start ? START_LINE : STOP_LINE
      ctx.font = "11px Inter, ui-sans-serif, system-ui, sans-serif"
      ctx.textBaseline = 'top'
      const label = start ? 'Start' : 'Stop'
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
      ctx.fillText(label, textX, area.top + 4)
    }
    ctx.restore()
  },
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
      const start = xScale.getPixelForValue(span.from)
      const end = xScale.getPixelForValue(span.to)
      if (!Number.isFinite(start) || !Number.isFinite(end)) continue
      const left = Math.max(area.left, Math.min(start, end))
      const right = Math.min(area.right, Math.max(start, end))
      if (right - left < 1) continue
      ctx.fillStyle = 'rgba(147, 160, 181, 0.14)'
      ctx.fillRect(left, area.top, right - left, area.bottom - area.top)
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
    const label = 'Off'
    const textWidth = ctx.measureText(label).width
    const midY = (area.top + area.bottom) / 2
    for (const span of spans) {
      const start = xScale.getPixelForValue(span.from)
      const end = xScale.getPixelForValue(span.to)
      if (!Number.isFinite(start) || !Number.isFinite(end)) continue
      const left = Math.max(area.left, Math.min(start, end))
      const right = Math.min(area.right, Math.max(start, end))
      if (right - left < textWidth + 12) continue
      ctx.fillText(label, (left + right) / 2, midY)
    }
    ctx.restore()
  },
}

Chart.register(offSpansPlugin, runMarkersPlugin)

function clockLabel(at) {
  return new Date(at).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  })
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
                  const value = items[0]?.parsed?.x
                  return Number.isFinite(value) ? clockLabel(value) : ''
                },
              }
            : {},
        },
      },
      scales: {
        y: {
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
                  return new Date(value).toLocaleTimeString('en-US', {
                    hour: 'numeric',
                    minute: '2-digit',
                  })
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

export function setChartPoints(chart, points, sensorNames, markers = []) {
  chart.$markers = markers
  const timed = Boolean(chart.$timed)
  const spans = timed ? offSpans(points, OFF_GAP_MS) : []
  chart.$offSpans = spans
  const datasets = sensorNames.map((name, index) => ({
    label: `${name} (°F)`,
    data: points.map((point) =>
      timed ? { x: point.at, y: point.temps[index] ?? null } : (point.temps[index] ?? null),
    ),
    borderColor: SENSOR_COLORS[index % SENSOR_COLORS.length],
    backgroundColor: index === 0 ? 'rgba(125, 211, 252, 0.16)' : 'rgba(251, 191, 36, 0.08)',
    borderWidth: 2,
    fill: index === 0,
    tension: 0.25,
    pointRadius: 0,
    pointHoverRadius: 4,
    spanGaps: timed ? OFF_GAP_MS : true,
  }))
  if (points.length) {
    datasets.push({
      label: 'Target (°F)',
      data: points.map((point) => (timed ? { x: point.at, y: point.target } : point.target)),
      borderColor: '#64748b',
      borderDash: [6, 4],
      borderWidth: 2,
      pointRadius: 0,
      fill: false,
      tension: 0,
      spanGaps: timed ? OFF_GAP_MS : true,
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
  chart.data.labels = timed ? [] : points.map((point) => point.label)
  chart.data.datasets = datasets
  if (timed) {
    const scale = chart.options.scales.x
    if (points.length) {
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
  chart.update()
}
