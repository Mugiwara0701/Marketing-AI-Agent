// What the shared drawer shows for each kind of item. Each kind supplies:
//   section  -> data section that backs Previous/Next (loaded on demand)
//   list()   -> items for Previous/Next, load(id) -> detail
//   describe(detail) -> { title, badge?, rows[], action?, bodyTitle?, body?, bodyBox? }
import { Icon } from './Icon';
import { Badge, StatusBadge, SourceCell, ExtLink, MetaRow } from './ui';
import { pad, fmtDate, toneOf } from '@/core/format';
import { visibleLeads } from './DataProvider';
import * as api from '@/core/api';

const ActionButton = ({ onClick, icon, children }) => (
  <button className="btn outline block" onClick={onClick}><Icon name={icon} /> {children}</button>
);

export function getKinds({ st, filter }, goto) {
  const lead = {
    section: 'leads',
    heading: 'Lead Details',
    list: () => visibleLeads(st.leads.data, filter),
    load: api.getLead,
    describe: (d) => {
      const c = d.conversation;
      return {
        title: `Lead #${pad(d.number)}`,
        badge: <StatusBadge text={d.status} />,
        rows: [
          <MetaRow key="name" k="Name">{d.name}</MetaRow>,
          <MetaRow key="title" k="Job Title">{d.title}</MetaRow>,
          <MetaRow key="co" k="Company"><ExtLink href={d.companyUrl}>{d.company}</ExtLink></MetaRow>,
          <MetaRow key="src" k="Source"><SourceCell source={d.source} /><div className="profile-link"><ExtLink href={d.profileUrl}>View profile</ExtLink></div></MetaRow>,
          <MetaRow key="mail" k="Email"><a className="mail-link" href={`mailto:${d.email}`}><Icon name="mail" />{d.email}</a></MetaRow>,
          <MetaRow key="added" k="Date Added">{fmtDate(d.addedAt)}</MetaRow>,
          <MetaRow key="status" k="Status"><StatusBadge text={d.status} /></MetaRow>,
          <MetaRow key="last" k="Last Contact Date">{fmtDate(d.lastContactAt)}</MetaRow>,
        ],
        action: c
          ? <ActionButton icon="mail" onClick={() => goto('history', c.type, c.id)}>Open email conversation</ActionButton>
          : <div className="muted small">No email conversation yet.</div>,
        bodyTitle: 'Notes', body: d.notes, bodyBox: true,
      };
    },
  };

  // Same drawer as a lead, but Previous/Next steps through positive leads only and the reply label is shown.
  const positiveLead = {
    ...lead,
    section: 'positive',
    list: () => st.positive.data,
    describe: (d) => {
      const v = lead.describe(d);
      const label = <MetaRow key="label" k="Reply Label">{d.replyLabel === 'interested' ? <Badge text="Interested" tone={toneOf('Interested')} /> : <span className="muted">{d.replyLabel || '—'}</span>}</MetaRow>;
      return { ...v, rows: [...v.rows.slice(0, 7), label, ...v.rows.slice(7)] };
    },
  };

  const sent = {
    section: 'sent',
    heading: 'Email Details',
    list: () => st.sent.data,
    load: api.getSentEmail,
    describe: (d) => ({
      title: `Email #${pad(d.number)}`,
      badge: <Badge text={d.deliveryStatus} tone={toneOf(d.deliveryStatus)} />,
      rows: [
        <MetaRow key="from" k="From">{d.from}</MetaRow>,
        <MetaRow key="to" k="To">{d.to}</MetaRow>,
        <MetaRow key="subj" k="Subject">{d.subject}</MetaRow>,
        <MetaRow key="sent" k="Date/Time Sent">{fmtDate(d.sentAt)}</MetaRow>,
        <MetaRow key="del" k="Delivery Status"><Badge text={d.deliveryStatus} tone={toneOf(d.deliveryStatus)} /></MetaRow>,
        <MetaRow key="reply" k="Reply Status">{d.replyId ? <Badge text="Replied" tone="green" /> : <Badge text="No reply" tone="gray" />}</MetaRow>,
      ],
      action: d.replyId ? <ActionButton icon="ext" onClick={() => goto('history', 'reply', d.replyId)}>Open linked reply</ActionButton> : null,
      bodyTitle: 'Email Body', body: d.body,
    }),
  };

  const reply = {
    section: 'replies',
    heading: 'Reply Details',
    list: () => st.replies.data,
    load: api.getReply,
    describe: (d) => {
      const o = d.originalEmail;
      return {
        title: `Reply #${pad(d.number)}`,
        badge: <StatusBadge text="Replied" />,
        rows: [
          <MetaRow key="from" k="From">{d.senderName}<div className="muted small">{d.senderEmail}</div></MetaRow>,
          <MetaRow key="to" k="To">{d.to}</MetaRow>,
          <MetaRow key="subj" k="Subject">{d.subject}</MetaRow>,
          <MetaRow key="recv" k="Date/Time Received">{fmtDate(d.receivedAt)}</MetaRow>,
        ],
        action: (
          <>
            <div className="linked-label">In reply to</div>
            <div className="linked"><b>Email #{pad(o.number)}</b><span className="muted">{o.subject}</span></div>
            <ActionButton icon="ext" onClick={() => goto('history', 'sent', o.id)}>Open original email</ActionButton>
          </>
        ),
        bodyTitle: 'Reply Body', body: d.body,
      };
    },
  };

  const blog = {
    section: 'blogs',
    heading: 'Blog Details',
    list: () => st.blogs.data,
    load: api.getBlog,
    describe: (d) => {
      const posted = d.status === 'Posted';
      return {
        title: `Blog #${pad(d.number)}`,
        badge: <StatusBadge text={d.status} />,
        rows: [
          <MetaRow key="title" k="Title"><b>{d.title}</b></MetaRow>,
          ...(posted ? [
            <MetaRow key="on" k="Posted On"><a className="src" href={d.url} target="_blank" rel="noopener noreferrer"><Icon name={d.site} />{d.site} <Icon name="ext" /></a></MetaRow>,
            <MetaRow key="at" k="Date/Time Posted">{fmtDate(d.postedAt)}</MetaRow>,
          ] : []),
        ],
        action: posted ? <a className="btn outline block" href={d.url} target="_blank" rel="noopener noreferrer"><Icon name="ext" /> View live post</a> : null,
        bodyTitle: 'Content', body: `## ${d.title}\n\n${d.content.replace(/^## /gm, '### ')}`,
      };
    },
  };

  return { lead, positiveLead, sent, reply, blog };
}
