// App shell: hash routing between the three pages, tab bar, and the single shared drawer.
import { icon } from './icons.js';
import { createDrawer } from './drawer.js';
import * as dashboard from './pages/dashboard.js';
import * as history from './pages/history.js';
import * as blogs from './pages/blogs.js';

const pages = { dashboard, history, blogs };
const TITLES = { dashboard: 'Dashboard', history: 'History', blogs: 'Blogs' };

const host = document.getElementById('page-host');

const drawer = createDrawer({
  root: document.getElementById('drawer'),
  overlay: document.getElementById('overlay'),
  kinds: { ...dashboard.drawerKinds, ...history.drawerKinds, ...blogs.drawerKinds },
});
const ctx = { drawer };

let current = null;
let pending = null; // drawer item to open once the target page has mounted

const pageFromHash = () => { const p = location.hash.replace(/^#\/?/, ''); return pages[p] ? p : 'dashboard'; };

function route() {
  const name = pageFromHash();
  if (name !== current) {
    if (!pending) drawer.close();
    current = name;
    const main = document.createElement('main');
    main.className = 'page';
    host.replaceChildren(main);
    pages[name].mount(main, ctx);
    document.querySelectorAll('.tab').forEach((t) => {
      const on = t.dataset.tab === name;
      t.classList.toggle('active', on);
      if (on) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
    });
    document.title = `${TITLES[name]} · Marketing AI Agent`;
    window.scrollTo(0, 0);
  }
  if (pending) { const p = pending; pending = null; drawer.open(p.kind, p.id); }
}

// "page:kind:id" -> switch to that page (if needed) and open the item.
function goto(page, kind, id) {
  if (page === current) { drawer.open(kind, id); return; }
  pending = { kind, id };
  location.hash = `#/${page}`;
}

document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-open],[data-goto]');
  if (!t) return;
  if (t.dataset.goto) goto(...t.dataset.goto.split(':'));
  else drawer.open(...t.dataset.open.split(':'));
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.matches('tr[data-open]')) e.target.click();
});
window.addEventListener('hashchange', route);
route();

// Theme switch: persisted in localStorage, applied via data-theme on <html> (set early in index.html).
const toggle = document.getElementById('theme-toggle');
toggle.innerHTML = `<span class="moon">${icon.moon}</span><span class="sun">${icon.sun}</span>`;
toggle.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('theme', next); } catch { /* storage unavailable */ }
});
