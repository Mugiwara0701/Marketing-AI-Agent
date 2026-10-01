// The ONE drawer component used for leads, sent emails, replies and blogs.
// Each kind supplies: list() -> items for Previous/Next, load(id) -> detail,
// describe(detail) -> { title, badge?, rows[], action?, bodyTitle?, body?, bodyBox? }; heading is the drawer header.
import { icon } from './icons.js';
import { esc, skeleton, formatContent } from './ui.js';

export function createDrawer({ root, overlay, kinds }) {
  const s = { open: false, kind: null, id: null, status: 'idle', detail: null };
  let token = 0; // guards against out-of-order responses when stepping quickly
  let returnTo = null;

  const list = () => (s.kind ? kinds[s.kind].list() : []);

  function markSelected() {
    document.querySelectorAll('[data-open]').forEach((el) => el.classList.toggle('selected', s.open && el.dataset.open === `${s.kind}:${s.id}`));
  }

  function renderState() {
    root.classList.toggle('open', s.open);
    overlay.classList.toggle('open', s.open);
    root.setAttribute('aria-hidden', String(!s.open));
    root.inert = !s.open;
    markSelected();
  }

  function renderContent() {
    if (!s.kind) return;
    const k = kinds[s.kind];
    const items = list();
    const i = items.findIndex((x) => x.id === s.id);
    const focused = root.contains(document.activeElement) ? document.activeElement.dataset : null;
    const v = s.status === 'ready' ? k.describe(s.detail) : null;
    const header = `<div class="drawer-head"><h2>${esc(k.heading)}</h2><button class="icon-btn" data-close aria-label="Close">${icon.close}</button></div>`;
    const steppers = `<div class="steppers">
      <button class="sq" data-step="-1" aria-label="Previous" ${i <= 0 ? 'disabled' : ''}>${icon.chevL}</button>
      <button class="sq" data-step="1" aria-label="Next" ${i < 0 || i >= items.length - 1 ? 'disabled' : ''}>${icon.chevR}</button></div>`;
    const titleMain = v
      ? `<div class="item-title">${esc(v.title)}</div><div class="item-meta"><span class="mono">ID: ${esc(s.detail.id)}</span>${v.badge || ''}</div>`
      : '<div class="skel" style="height:28px;width:150px"></div><div class="skel" style="height:14px;width:110px;margin-top:8px"></div>';
    let content;
    if (s.status === 'loading') content = skeleton(8);
    else if (s.status === 'error') content = `<div class="error">${icon.alert}<div>Couldn't load details.</div><button class="btn" data-retry-detail>Try again</button></div>`;
    else {
      content = `<div class="meta">${v.rows.join('')}</div>${v.action ? `<div class="action">${v.action}</div>` : ''}
        ${v.body != null ? `<h3 class="body-title">${esc(v.bodyTitle)}</h3>${v.bodyBox ? `<div class="notes">${esc(v.body)}</div>` : `<div class="content">${formatContent(v.body)}</div>`}` : ''}`;
    }
    root.innerHTML = `${header}<div class="drawer-body"><div class="title-row"><div>${titleMain}</div>${steppers}</div><hr>${content}</div>`;
    // Keep keyboard focus on the same control after the content is swapped.
    if (focused) (root.querySelector(focused.step ? `[data-step="${focused.step}"]:not(:disabled)` : '[data-close]') || root.querySelector('[data-close]'))?.focus();
  }

  async function open(kind, id) {
    const wasOpen = s.open;
    Object.assign(s, { open: true, kind, id, status: 'loading', detail: null });
    const mine = ++token;
    if (!wasOpen) returnTo = document.activeElement;
    renderState(); renderContent();
    if (!wasOpen) root.focus({ preventScroll: true });
    try {
      const detail = await kinds[kind].load(id);
      if (mine !== token) return;
      Object.assign(s, { status: 'ready', detail });
    } catch {
      if (mine !== token) return;
      s.status = 'error';
    }
    renderContent();
    root.querySelector('.drawer-body')?.scrollTo(0, 0);
  }

  function close() {
    if (!s.open) return;
    token++;
    s.open = false;
    renderState();
    if (returnTo?.isConnected) returnTo.focus({ preventScroll: true });
    returnTo = null;
  }

  function step(delta) {
    const items = list();
    const next = items[items.findIndex((x) => x.id === s.id) + delta];
    if (next) open(s.kind, next.id);
  }

  root.addEventListener('click', (e) => {
    const t = e.target.closest('[data-step],[data-close],[data-retry-detail]');
    if (!t) return;
    if (t.dataset.step) step(Number(t.dataset.step));
    else if ('close' in t.dataset) close();
    else open(s.kind, s.id);
  });
  overlay.addEventListener('click', close);
  document.addEventListener('keydown', (e) => {
    if (!s.open) return;
    if (e.key === 'Escape') close();
    else if (e.key === 'Tab') { // keep focus inside the open drawer
      const f = [...root.querySelectorAll('button:not(:disabled), a[href]')];
      if (!f.length) return;
      const first = f[0]; const last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === root)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  return {
    open, close, markSelected,
    // Re-render when the underlying list changes (e.g. finished loading) so Previous/Next stay correct.
    refresh: () => { if (s.open) renderContent(); },
  };
}
