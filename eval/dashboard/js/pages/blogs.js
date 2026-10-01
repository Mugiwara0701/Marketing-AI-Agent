// Page 3: Blogs (Blog Tracker).
import * as api from '../api.js';
import { icon } from '../icons.js';
import { esc, pad, fmtDate, statusBadge, siteCell, row, rowAttrs, viewCell, tableBody, createLoader } from '../ui.js';

const st = { blogs: { status: 'idle', data: [] } };
let root;
let ctx;

const loader = createLoader(st, { blogs: api.getBlogs }, () => {
  render();
  if (st.blogs.status === 'ready') ctx.drawer.refresh();
});

function render() {
  const s = st.blogs;
  const ready = s.status === 'ready';
  const posted = s.data.filter((b) => b.status === 'Posted').length;
  const n = s.data.length;
  const box = (label, v, color) => `<div class="stat-box"><div class="muted small"><span class="dot" style="background:${color}"></span>${label}</div><div class="big">${v}</div></div>`;
  const total = ready ? `<div class="stats">${box('Posted', posted, 'var(--green)')}${box('Not Posted', n - posted, 'var(--amber)')}</div>` : '';
  const count = ready ? `<div class="total-block"><div class="muted small">Total Blogs</div><div class="big">${n}</div></div>` : '';
  root.querySelector('#blog-card').innerHTML = `
    <div class="blog-top"><div><div class="card-head"><div class="tile">${icon.doc}</div><div class="card-title">Blog Tracker</div></div>${count}</div>${total}</div>
    ${tableBody(s, {
      head: '<th>#</th><th>Blog Topic/Title</th><th>Status</th><th>Posted On</th><th class="view">View</th>',
      rows: s.data.map((b) => `<tr ${rowAttrs('blog', b.id)}><td class="num">${pad(b.number)}</td><td class="name"><div class="ellip wide">${esc(b.title)}</div></td><td>${statusBadge(b.status)}</td><td>${siteCell(b.site)}</td>${viewCell}</tr>`),
      emptyIcon: 'doc', emptyText: 'No blogs yet', retryKey: 'blogs', what: 'blogs',
    })}`;
  ctx.drawer.markSelected();
}

const blog = {
  heading: 'Blog Details',
  list: () => st.blogs.data,
  load: api.getBlog,
  describe: (d) => {
    const posted = d.status === 'Posted';
    return {
      title: `Blog #${pad(d.number)}`,
      badge: statusBadge(d.status),
      rows: [
        row('Title', `<b>${esc(d.title)}</b>`),
        ...(posted ? [
          row('Posted On', `<a class="src" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${icon[d.site]}${esc(d.site)} ${icon.ext}</a>`),
          row('Date/Time Posted', fmtDate(d.postedAt)),
        ] : []),
      ],
      action: posted ? `<a class="btn outline block" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">${icon.ext} View live post</a>` : '',
      bodyTitle: 'Content', body: `## ${d.title}\n\n${d.content.replace(/^## /gm, '### ')}`,
    };
  },
};

export const drawerKinds = { blog };

export function mount(el, context) {
  root = el; ctx = context;
  root.innerHTML = `
    <div class="page-head"><h1 class="page-title">Blogs</h1><p class="muted">All blog topics and where they were posted.</p></div>
    <div class="card" id="blog-card"></div>`;
  root.addEventListener('click', (e) => {
    const t = e.target.closest('[data-retry]');
    if (t) loader.load(t.dataset.retry);
  });
  render();
  loader.ensure();
}
