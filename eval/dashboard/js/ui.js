// Small rendering helpers shared by every page and the drawer.
import { icon } from './icons.js';

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const pad = (n) => String(n).padStart(3, '0');

// mode: 'full' "Apr 20, 2024, 10:15 AM" | 'table' "Apr 20, 2024 10:15 AM" | 'date' "Apr 20, 2024"
export function fmtDate(iso, mode = 'full') {
  if (!iso) return '—';
  const d = new Date(iso);
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
  if (mode === 'date') return date;
  const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'UTC' });
  return mode === 'table' ? `${date} ${time}` : `${date}, ${time}`;
}

export const badge = (text, tone) => `<span class="badge ${tone}">${esc(text)}</span>`;
export const toneOf = (t) => ({ Running: 'green', Delivered: 'green', Replied: 'green', Posted: 'green', Responded: 'green', Paused: 'amber', Awaiting: 'amber', 'Not Posted': 'amber', Failed: 'red' }[t] || 'gray');
export const statusBadge = (t) => badge(t, toneOf(t));

export const SOURCES = {
  linkedin: { label: 'LinkedIn', icon: 'linkedin', color: '#2f6fed' },
  x: { label: 'X', icon: 'x', color: 'var(--src-x)' },
  other: { label: 'Other', icon: 'other', color: '#b8bccb' },
};
export const sourceCell = (key) => `<span class="src">${icon[SOURCES[key].icon]}${esc(SOURCES[key].label)}</span>`;
export const siteCell = (site) => (site ? `<span class="src">${icon[site]}${esc(site)}</span>` : '<span class="muted">—</span>');

export const skeleton = (rows = 5) => Array.from({ length: rows }, () => '<div class="skel skel-line"></div>').join('');
export const errorBox = (key, what) => `<div class="error">${icon.alert}<div>Couldn't load ${what}.</div><button class="btn" data-retry="${key}">Try again</button></div>`;
export const emptyBox = (ic, text) => `<div class="empty"><div class="tile">${icon[ic]}</div><div>${text}</div></div>`;
export const cardHead = (ic, title, extra = '') => `<div class="card-head"><div class="tile">${icon[ic]}</div><div class="card-title">${title}</div>${extra}</div>`;

export const row = (k, v) => `<div class="meta-row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
export const rowAttrs = (kind, id) => `data-open="${kind}:${id}" tabindex="0"`;
export const viewCell = `<td class="view"><span aria-label="View">${icon.eye}</span></td>`;

// Table body for a loadable section: skeleton / error / empty / table.
export function tableBody(s, { head, rows, emptyIcon, emptyText, retryKey, what }) {
  if (s.status === 'idle' || s.status === 'loading') return skeleton();
  if (s.status === 'error') return errorBox(retryKey, what);
  if (!rows.length) return emptyBox(emptyIcon, emptyText);
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

// A section's state is { status: 'idle' | 'loading' | 'ready' | 'error', data }.
export function createLoader(state, fetchers, onUpdate) {
  async function load(key) {
    if (state[key].status === 'loading') return;
    state[key].status = 'loading';
    onUpdate(key);
    try { state[key].data = await fetchers[key](); state[key].status = 'ready'; }
    catch { state[key].status = 'error'; }
    onUpdate(key);
  }
  // Fetch only sections that have not been loaded yet (used when a page is re-mounted).
  const ensure = () => Object.keys(state).forEach((k) => { if (state[k].status === 'idle') load(k); });
  return { load, ensure };
}

// Minimal formatter for plain-text bodies: "## " / "### " headings, "- " and "1. " lists, "> " quotes,
// paragraphs, auto-linked URLs and emails.
export function formatContent(text) {
  const link = (s) => esc(s)
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])([\w.+-]+@[\w-]+\.[\w.-]+)/g, '$1<a href="mailto:$2">$2</a>');
  return text.split(/\n{2,}/).map((block) => {
    const lines = block.split('\n');
    if (lines.every((l) => l.startsWith('- '))) return `<ul>${lines.map((l) => `<li>${link(l.slice(2))}</li>`).join('')}</ul>`;
    if (lines.every((l) => /^\d+\. /.test(l))) return `<ol>${lines.map((l) => `<li>${link(l.replace(/^\d+\. /, ''))}</li>`).join('')}</ol>`;
    if (lines.every((l) => l.startsWith('> '))) return `<blockquote>${lines.map((l) => link(l.slice(2))).join('<br>')}</blockquote>`;
    if (block.startsWith('### ')) return `<h5>${esc(block.slice(4))}</h5>`;
    if (block.startsWith('## ')) return `<h4>${esc(block.slice(3))}</h4>`;
    return `<p>${lines.map(link).join('<br>')}</p>`;
  }).join('');
}
