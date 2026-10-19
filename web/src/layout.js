const LINKS = [
  ['/', 'Dashboard', 'dashboard'],
  ['/history', 'History', 'history'],
  ['/settings', 'Settings', 'settings'],
]

export function mountShell(active) {
  const app = document.getElementById('app')
  const links = LINKS.map(
    ([href, label, id]) =>
      `<a href="${href}" class="sidebar-item${id === active ? ' is-active' : ''}">${label}</a>`,
  ).join('')
  app.innerHTML = `
    <div class="shell">
      <aside class="rail" id="sidebar">
        <div class="rail-top">
          <button class="sidebar-close" id="sidebar-close" type="button" aria-label="Close menu">×</button>
          <div class="brand">
            <span class="brand-mark" aria-hidden="true">B</span>
            <div>
              <div class="brand-name">Brewski <span class="app-version" data-app-version></span></div>
              <div class="brand-sub">Still controller</div>
            </div>
          </div>
        </div>
        <nav class="rail-nav">${links}</nav>
        <div class="rail-foot">
          <div class="source-badge">Dummy source</div>
        </div>
      </aside>
      <div class="sidebar-overlay" id="sidebar-overlay"></div>
      <div class="workspace">
        <header class="menu-bar">
          <button class="menu-btn" id="menu-btn" type="button" aria-label="Open menu">
            <span class="menu-icon">☰</span>
          </button>
          <div class="menu-title">Brewski <span class="app-version" data-app-version></span></div>
          <div class="source-badge">Dummy source</div>
        </header>
        <div class="status-strip-host" id="status-strip-host"></div>
        <main class="container">
          <div id="page"></div>
        </main>
      </div>
      <div id="setup-host"></div>
      <div id="update-host"></div>
    </div>
  `
  const sidebar = document.getElementById('sidebar')
  const menuBtn = document.getElementById('menu-btn')
  const closeBtn = document.getElementById('sidebar-close')
  menuBtn.setAttribute('aria-expanded', 'false')
  menuBtn.setAttribute('aria-controls', 'sidebar')
  const setOpen = (open) => {
    sidebar.classList.toggle('active', open)
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false')
  }
  menuBtn.addEventListener('click', () => setOpen(true))
  closeBtn.addEventListener('click', () => setOpen(false))
  document.getElementById('sidebar-overlay').addEventListener('click', () => setOpen(false))
  sidebar.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setOpen(false)
  })
  return document.getElementById('page')
}

export function setSourceBadge(source) {
  const label = source === 'dummy' ? 'Dummy source' : source === 'pi' ? 'Pi' : source || 'Controller'
  document.querySelectorAll('.source-badge').forEach((badge) => {
    badge.textContent = label
  })
}

export function setAppVersion(version) {
  const label = version ? `v${version}` : ''
  document.querySelectorAll('[data-app-version]').forEach((node) => {
    node.textContent = label
  })
}

export function closeSidebar() {
  document.getElementById('sidebar')?.classList.remove('active')
  document.getElementById('menu-btn')?.setAttribute('aria-expanded', 'false')
}
