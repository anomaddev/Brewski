import { escapeHtml } from './format.js'
import { closeSidebar, mountShell } from './layout.js'
import { mountDashboard } from './pages/dashboard.js'
import { mountHistory } from './pages/history.js'
import { mountRun } from './pages/run.js'
import { mountSettings } from './pages/settings.js'
import { mountSetup } from './setup.js'
import { mountStatusStrip } from './status-strip.js'
import { mountUpdateOverlay } from './update-overlay.js'

let cleanup = () => {}

function matchRoute(path) {
  if (path === '/') return { id: 'dashboard', title: 'Dashboard', mount: mountDashboard, params: {} }
  if (path === '/history') return { id: 'history', title: 'History', mount: mountHistory, params: {} }
  const run = path.match(/^\/history\/(\d+)$/)
  if (run) return { id: 'history', title: 'Run', mount: mountRun, params: { id: run[1] } }
  if (path === '/settings') return { id: 'settings', title: 'Settings', mount: mountSettings, params: {} }
  return null
}

export function openPath(path) {
  if (path === window.location.pathname) return
  window.history.pushState(null, '', path)
  render()
}

async function render() {
  const previous = cleanup
  cleanup = () => {}
  previous()
  closeSidebar()
  const route = matchRoute(window.location.pathname)
  document.title = route ? `Brewski — ${route.title}` : 'Brewski'
  const page = mountShell(route ? route.id : '')
  const stripCleanup = mountPageStatus(route)
  const setupCleanup = mountSetup()
  const updateCleanup = mountUpdateOverlay()
  const release = () => {
    stripCleanup()
    setupCleanup()
    updateCleanup()
  }
  if (!route) {
    page.innerHTML = `
      <div class="dashboard-card">
        <h2 class="card-title">Page not found</h2>
        <p class="help"><a href="/">Back to the dashboard</a></p>
      </div>`
    cleanup = release
    return
  }
  try {
    const pageCleanup = (await route.mount(page, route.params)) || (() => {})
    cleanup = () => {
      pageCleanup()
      release()
    }
  } catch (error) {
    page.innerHTML = `
      <div class="dashboard-card">
        <h2 class="card-title">Something went wrong</h2>
        <p class="form-error">${escapeHtml(error.message)}</p>
      </div>`
    cleanup = release
  }
}

function mountPageStatus(route) {
  if (route?.id === 'dashboard') return () => {}
  const host = document.getElementById('status-strip-host')
  if (!host) return () => {}
  return mountStatusStrip(host)
}

export function startRouter() {
  window.addEventListener('popstate', () => {
    render()
  })
  document.body.addEventListener('click', (event) => {
    const link = event.target.closest('a[href]')
    if (!link || link.target === '_blank' || link.hasAttribute('download') || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    const url = new URL(link.href, window.location.origin)
    if (url.origin !== window.location.origin) return
    // Run export and other API URLs are real requests. Treating them as pages shows "Page not found".
    if (url.pathname === '/api' || url.pathname.startsWith('/api/')) return
    event.preventDefault()
    if (url.pathname === window.location.pathname) {
      closeSidebar()
      return
    }
    window.history.pushState(null, '', url.pathname)
    render()
  })
  render()
}
