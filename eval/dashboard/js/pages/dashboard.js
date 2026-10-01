// Page 1: Pipeline Control + Leads Overview.
import * as api from '../api.js';
import { icon } from '../icons.js';
import { esc, pad, fmtDate, statusBadge, SOURCES, sourceCell, skeleton, errorBox, cardHead, row, rowAttrs, viewCell, tableBody, createLoader } from '../ui.js';

// State lives at module level so it survives tab switches (Start/Pause, filters, loaded data).
const st = {
  pipelines: { status: 'idle', data: [] },
  stats: { status: 'idle', data: null },
  leads: { status: 'idle', data: [] },
};
const filter = { status: 'all', source: 'all' };
let pipeError = '';
let root;
let ctx;

const $ = (sel) => root.querySelector(sel);
const visible = () => st.leads.data.filter((l) => (filter.status === 'all' || l.status.toLowerCase() === filter.status) && (filter.source === 'all' || l.source === filter.source));

const loader = createLoader(
  st,
  { pipelines: api.getPipelines, stats: api.getLeadStats, leads: () => api.getLeads() },
  (key) => {
    if (key === 'pipelines') renderPipeline();
    else if (key === 'stats') { renderStats(); renderSource(); }
    else { renderLeads(); ctx.drawer.refresh(); }
  },
);

// ---------- Pipeline Control ----------
function renderPipeline() {
  const s = st.pipelines;
  let cards;
  if (s.status === 'idle' || s.status === 'loading') cards = `<div class="card">${skeleton(3)}</div>`.repeat(2);
  else if (s.status === 'error') cards = `<div class="card span2">${errorBox('pipelines', 'pipelines')}</div>`;
  else {
    cards = s.data.map((p) => {
      const running = p.status === 'running';
      return `<div class="card pipe">
        <div class="card-head"><div class="tile">${icon[p.id === 'email' ? 'mail' : 'doc']}</div>
          <div><div class="card-title">${esc(p.name)}</div>${statusBadge(running ? 'Running' : 'Paused')}</div></div>
        <p class="desc">${esc(p.description)}</p>
        <div class="row pipe-btns">
          <button class="btn primary" data-pipe="start:${p.id}">${icon.play} Start</button>
          <button class="btn outline" data-pipe="pause:${p.id}">${icon.pause} Pause</button>
        </div></div>`;
    }).join('');
  }
  const off = s.status !== 'ready' ? 'disabled' : '';
  $('#pipeline').innerHTML = `
    <div class="section-head"><h2 class="section-title">Pipeline Control</h2>
      <div class="row">
        <button class="btn primary" data-pipe="start:all" ${off}>${icon.play} Start All</button>
        <button class="btn outline" data-pipe="pause:all" ${off}>${icon.pause} Pause All</button>
      </div></div>
    ${pipeError ? `<div class="notice">${esc(pipeError)}</div>` : ''}
    <div class="grid2">${cards}</div>`;
}

async function pipeAction(action, id) {
  const ids = id === 'all' ? st.pipelines.data.map((p) => p.id) : [id];
  pipeError = '';
  // Optimistic update so the badge and buttons change instantly.
  const prev = new Map(st.pipelines.data.map((p) => [p.id, p.status]));
  st.pipelines.data.forEach((p) => { if (ids.includes(p.id)) p.status = action === 'start' ? 'running' : 'paused'; });
  renderPipeline();
  try {
    await Promise.all(ids.map((i) => (action === 'start' ? api.startPipeline(i) : api.pausePipeline(i))));
  } catch {
    st.pipelines.data.forEach((p) => { if (ids.includes(p.id)) p.status = prev.get(p.id); });
    pipeError = `Couldn't ${action} the pipeline. Please try again.`;
  }
  renderPipeline();
}

// ---------- Leads Overview: stat cards + source breakdown ----------
const STATS = [
  { key: 'total', label: 'Total Leads', ic: 'users', tone: '' },
  { key: 'awaiting', label: 'Awaiting Response', ic: 'clock', tone: 'lavender' },
  { key: 'responded', label: 'Responded', ic: 'reply', tone: 'green' },
];

function renderStats() {
  const s = st.stats;
  $('#stats').innerHTML = STATS.map(({ key, label, ic, tone }) => {
    let body;
    if (s.status === 'idle' || s.status === 'loading') body = '<div class="skel" style="height:30px;width:90px"></div>';
    else if (s.status === 'error') body = `<button class="btn" data-retry="stats">Retry</button>`;
    else body = `<div class="big">${s.data[key]}</div>`;
    return `<div class="card stat"><div class="tile round ${tone}">${icon[ic]}</div><div><div class="muted">${label}</div>${body}</div></div>`;
  }).join('');
}

function renderSource() {
  const s = st.stats;
  let body;
  if (s.status === 'idle' || s.status === 'loading') body = skeleton(3);
  else if (s.status === 'error') body = errorBox('stats', 'lead sources');
  else {
    const { total, bySource } = s.data;
    const keys = Object.keys(SOURCES);
    body = `<div class="stack" role="img" aria-label="${keys.map((k) => `${SOURCES[k].label} ${bySource[k]}`).join(', ')}">
        ${keys.map((k) => `<span style="flex-grow:${bySource[k]};background:${SOURCES[k].color}" title="${esc(SOURCES[k].label)}: ${bySource[k]}"></span>`).join('')}</div>
      <div class="legend">${keys.map((k) => `<div class="legend-item">${icon[SOURCES[k].icon]}
        <span class="legend-label">${esc(SOURCES[k].label)}</span><span class="legend-count">${bySource[k]}</span></div>`).join('')}</div>`;
  }
  $('#source').innerHTML = `<div class="card">${cardHead('users', 'Leads by Source')}${body}</div>`;
}

// ---------- Leads table ----------
const chip = (group, v, label) => `<button class="chip ${filter[group] === v ? 'active' : ''}" data-filter="${group}:${v}" aria-pressed="${filter[group] === v}">${label}</button>`;

function renderLeads() {
  const s = st.leads;
  const list = visible();
  const ready = s.status === 'ready' && s.data.length > 0;
  const filters = ready ? `<div class="filters">
    <div class="chip-group">${chip('status', 'all', 'All')}${chip('status', 'awaiting', 'Awaiting')}${chip('status', 'responded', 'Responded')}</div>
    <div class="chip-group">${chip('source', 'all', 'All sources')}${Object.entries(SOURCES).map(([k, v]) => chip('source', k, esc(v.label))).join('')}</div></div>` : '';
  $('#leads').innerHTML = `<div class="card">${filters}
    ${tableBody(s, {
      head: '<th>#</th><th>Name</th><th>Source</th><th>Status</th><th>Date Added</th><th class="view">View</th>',
      rows: list.map((l) => `<tr ${rowAttrs('lead', l.id)}><td class="num">${pad(l.number)}</td><td class="name">${esc(l.name)}</td><td>${sourceCell(l.source)}</td><td>${statusBadge(l.status)}</td><td class="muted">${fmtDate(l.addedAt, 'date')}</td>${viewCell}</tr>`),
      emptyIcon: 'users', emptyText: s.data.length ? 'No leads match these filters' : 'No leads yet', retryKey: 'leads', what: 'leads',
    })}</div>`;
  ctx.drawer.markSelected();
}

// ---------- Drawer config ----------
const ext = (href, label) => `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)} ${icon.ext}</a>`;

const describe = (d) => {
  const c = d.conversation;
  const action = c
    ? `<button class="btn outline block" data-goto="history:${c.type}:${c.id}">${icon.mail} Open email conversation</button>`
    : '<div class="muted small">No email conversation yet.</div>';
  return {
    title: `Lead #${pad(d.number)}`,
    badge: statusBadge(d.status),
    rows: [
      row('Name', esc(d.name)), row('Job Title', esc(d.title)), row('Company', ext(d.companyUrl, d.company)),
      row('Source', `${sourceCell(d.source)}<div class="profile-link">${ext(d.profileUrl, 'View profile')}</div>`),
      row('Email', `<a class="mail-link" href="mailto:${esc(d.email)}">${icon.mail}${esc(d.email)}</a>`), row('Date Added', fmtDate(d.addedAt)),
      row('Status', statusBadge(d.status)), row('Last Contact Date', fmtDate(d.lastContactAt)),
    ],
    action, bodyTitle: 'Notes', body: d.notes, bodyBox: true,
  };
};

export const drawerKinds = { lead: { heading: 'Lead Details', list: visible, load: api.getLead, describe } };

// ---------- Mount ----------
export function mount(el, context) {
  root = el; ctx = context;
  root.innerHTML = `
    <section id="pipeline" aria-label="Pipeline control"></section>
    <section aria-label="Leads overview">
      <div class="section-head"><h2 class="section-title">Leads Overview</h2></div>
      <div class="stack-v"><div class="grid3" id="stats"></div><div id="source"></div><div id="leads"></div></div>
    </section>`;
  root.addEventListener('click', (e) => {
    const t = e.target.closest('[data-pipe],[data-retry],[data-filter]');
    if (!t) return;
    if (t.dataset.pipe) pipeAction(...t.dataset.pipe.split(':'));
    else if (t.dataset.retry) loader.load(t.dataset.retry);
    else {
      const [group, v] = t.dataset.filter.split(':');
      filter[group] = v;
      renderLeads();
      $(`[data-filter="${t.dataset.filter}"]`)?.focus();
    }
  });
  renderPipeline(); renderStats(); renderSource(); renderLeads();
  loader.ensure();
}
