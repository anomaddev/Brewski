import Chart from 'chart.js/auto'

const SENSOR_COLORS = ['#7dd3fc', '#fbbf24']
const GRID = 'rgba(255, 255, 255, 0.06)'
const TICK = '#93a0b5'

Chart.defaults.color = TICK
Chart.defaults.borderColor = GRID
Chart.defaults.font.family = "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"

export function createTempChart(canvas) {
  return new Chart(canvas, {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
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
        },
      },
      scales: {
        y: {
          title: { display: true, text: 'Temperature (°F)', color: TICK },
          ticks: { color: TICK },
          grid: { color: GRID },
          border: { display: false },
        },
        x: {
          title: { display: true, text: 'Time', color: TICK },
          ticks: { color: TICK, maxTicksLimit: 8 },
          grid: { display: false },
          border: { display: false },
        },
      },
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
    },
  })
}

export function setChartPoints(chart, points, sensorNames) {
  const datasets = sensorNames.map((name, index) => ({
    label: `${name} (°F)`,
    data: points.map((point) => point.temps[index] ?? null),
    borderColor: SENSOR_COLORS[index % SENSOR_COLORS.length],
    backgroundColor: index === 0 ? 'rgba(125, 211, 252, 0.16)' : 'rgba(251, 191, 36, 0.08)',
    borderWidth: 2,
    fill: index === 0,
    tension: 0.25,
    pointRadius: 0,
    pointHoverRadius: 4,
    spanGaps: true,
  }))
  if (points.length) {
    datasets.push({
      label: 'Target (°F)',
      data: points.map((point) => point.target),
      borderColor: '#64748b',
      borderDash: [6, 4],
      borderWidth: 2,
      pointRadius: 0,
      fill: false,
      tension: 0,
    })
  }
  chart.data.labels = points.map((point) => point.label)
  chart.data.datasets = datasets
  chart.update()
}
