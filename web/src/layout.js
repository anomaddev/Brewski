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
              <div class="brand-name">Brewski</div>
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
          <div class="menu-title">Brewski</div>
          <div class="source-badge">Dummy source</div>
        </header>
        <main class="container">
          <div id="page"></div>
        </main>
      </div>
    </div>
  `
  const sidebar = document.getElementById('sidebar')
  document.getElementById('menu-btn').addEventListener('click', () => sidebar.classList.add('active'))
  document.getElementById('sidebar-close').addEventListener('click', () => sidebar.classList.remove('active'))
  document.getElementById('sidebar-overlay').addEventListener('click', () => sidebar.classList.remove('active'))
  return document.getElementById('page')
}

export function setSourceBadge(source) {
  const label = source === 'dummy' ? 'Dummy source' : source || 'Controller'
  document.querySelectorAll('.source-badge').forEach((badge) => {
    badge.textContent = label
  })
}

export function closeSidebar() {
  document.getElementById('sidebar')?.classList.remove('active')
}
