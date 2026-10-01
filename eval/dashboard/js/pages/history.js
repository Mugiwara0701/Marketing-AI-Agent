// Page 2: History (emails sent + replies received).
import * as api from '../api.js';
import { icon } from '../icons.js';
import { esc, pad, fmtDate, badge, toneOf, statusBadge, cardHead, row, rowAttrs, viewCell, tableBody, createLoader } from '../ui.js';

const st = { sent: { status: 'idle', data: [] }, replies: { status: 'idle', data: [] } };
let root;
let ctx;

const loader = createLoader(st, { sent: api.getSentEmails, replies: api.getReplies }, (key) => {
  renderCard(key);
  if (st[key].status === 'ready') ctx.drawer.refresh();
});

const CARDS = {
  sent: {
    ic: 'send', title: 'Emails Sent', totalLabel: 'Total Emails Sent', kind: 'sent', emptyText: 'No emails sent yet',
    head: '<th>#</th><th>Recipient Email</th><th>Subject</th><th>Date/Time Sent</th><th class="view">View</th>',
    row: (e) => `<tr ${rowAttrs('sent', e.id)}><td class="num">${pad(e.number)}</td><td>${esc(e.to)}</td><td><div class="ellip">${esc(e.subject)}</div></td><td class="muted when">${fmtDate(e.sentAt, 'table')}</td>${viewCell}</tr>`,
  },
  replies: {
    ic: 'chat', title: 'Replies Received', totalLabel: 'Total Replies Received', kind: 'reply', emptyText: 'No replies yet',
    head: '<th>#</th><th>Sender Name/Email</th><th>Subject</th><th>Date/Time Received</th><th class="view">View</th>',
    row: (r) => `<tr ${rowAttrs('reply', r.id)}><td class="num">${pad(r.number)}</td><td><div class="name">${esc(r.senderName)}</div><div class="muted small">${esc(r.senderEmail)}</div></td><td><div class="ellip">${esc(r.subject)}</div></td><td class="muted when">${fmtDate(r.receivedAt, 'table')}</td>${viewCell}</tr>`,
  },
};

function renderCard(key) {
  const c = CARDS[key];
  const s = st[key];
  const total = s.status === 'ready' ? `<div class="total-block"><div class="muted small">${c.totalLabel}</div><div class="big">${s.data.length}</div></div>` : '';
  root.querySelector(`#card-${key}`).innerHTML = `${cardHead(c.ic, c.title)}${total}${tableBody(s, {
    head: c.head, rows: s.data.map(c.row), emptyIcon: c.ic, emptyText: c.emptyText, retryKey: key, what: c.title.toLowerCase(),
  })}`;
  ctx.drawer.markSelected();
}

// ---------- Drawer config ----------
const sent = {
  list: () => st.sent.data,
  heading: 'Email Details',
  load: api.getSentEmail,
  describe: (d) => ({
    title: `Email #${pad(d.number)}`,
    badge: badge(d.deliveryStatus, toneOf(d.deliveryStatus)),
    rows: [
      row('From', esc(d.from)), row('To', esc(d.to)), row('Subject', esc(d.subject)), row('Date/Time Sent', fmtDate(d.sentAt)),
      row('Delivery Status', badge(d.deliveryStatus, toneOf(d.deliveryStatus))),
      row('Reply Status', d.replyId ? badge('Replied', 'green') : badge('No reply', 'gray')),
    ],
    action: d.replyId ? `<button class="btn outline block" data-goto="history:reply:${d.replyId}">${icon.ext} Open linked reply</button>` : '',
    bodyTitle: 'Email Body', body: d.body,
  }),
};

const reply = {
  list: () => st.replies.data,
  heading: 'Reply Details',
  load: api.getReply,
  describe: (d) => {
    const o = d.originalEmail;
    return {
      title: `Reply #${pad(d.number)}`,
      badge: statusBadge('Replied'),
      rows: [
        row('From', `${esc(d.senderName)}<div class="muted small">${esc(d.senderEmail)}</div>`), row('To', esc(d.to)),
        row('Subject', esc(d.subject)), row('Date/Time Received', fmtDate(d.receivedAt)),
      ],
      action: `<div class="linked-label">In reply to</div>
        <div class="linked"><b>Email #${pad(o.number)}</b><span class="muted">${esc(o.subject)}</span></div>
        <button class="btn outline block" data-goto="history:sent:${o.id}">${icon.ext} Open original email</button>`,
      bodyTitle: 'Reply Body', body: d.body,
    };
  },
};

export const drawerKinds = { sent, reply };

// ---------- Mount ----------
export function mount(el, context) {
  root = el; ctx = context;
  root.innerHTML = `
    <div class="page-head"><h1 class="page-title">History</h1><p class="muted">All emails sent and replies received.</p></div>
    <div class="grid2 top"><div class="card" id="card-sent"></div><div class="card" id="card-replies"></div></div>`;
  root.addEventListener('click', (e) => {
    const t = e.target.closest('[data-retry]');
    if (t) loader.load(t.dataset.retry);
  });
  Object.keys(st).forEach(renderCard);
  loader.ensure();
}
