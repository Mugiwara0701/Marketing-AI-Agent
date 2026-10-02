'use client';
// Page 2: History (emails sent + replies received).
import { OpenRow } from './OpenRow';
import { useSection } from './DataProvider';
import { CardHead, ViewCell, DataTable } from './ui';
import { pad, fmtDate } from '@/core/format';

function HistoryCard({ section, icon, title, totalLabel, kind, emptyText, head, row }) {
  const { status, data, reload } = useSection(section);
  return (
    <div className="card">
      <CardHead icon={icon} title={title} />
      {status === 'ready' && <div className="total-block"><div className="muted small">{totalLabel}</div><div className="big">{data.length}</div></div>}
      <DataTable
        status={status} head={head} emptyIcon={icon} emptyText={emptyText} what={title.toLowerCase()} onRetry={reload}
        rows={data.map((x) => <OpenRow key={x.id} kind={kind} id={x.id}>{row(x)}</OpenRow>)}
      />
    </div>
  );
}

export default function HistoryView() {
  return (
    <main className="page">
      <div className="page-head"><h1 className="page-title">History</h1><p className="muted">All emails sent and replies received.</p></div>
      <div className="grid2 top">
        <HistoryCard
          section="sent" icon="send" title="Emails Sent" totalLabel="Total Emails Sent" kind="sent" emptyText="No emails sent yet"
          head={<><th>#</th><th>Recipient Email</th><th>Subject</th><th>Date/Time Sent</th><th className="view">View</th></>}
          row={(e) => (
            <>
              <td className="num">{pad(e.number)}</td><td>{e.to}</td><td><div className="ellip">{e.subject}</div></td>
              <td className="muted when">{fmtDate(e.sentAt, 'table')}</td><ViewCell />
            </>
          )}
        />
        <HistoryCard
          section="replies" icon="chat" title="Replies Received" totalLabel="Total Replies Received" kind="reply" emptyText="No replies yet"
          head={<><th>#</th><th>Sender Name/Email</th><th>Subject</th><th>Date/Time Received</th><th className="view">View</th></>}
          row={(r) => (
            <>
              <td className="num">{pad(r.number)}</td>
              <td><div className="name">{r.senderName}</div><div className="muted small">{r.senderEmail}</div></td>
              <td><div className="ellip">{r.subject}</div></td><td className="muted when">{fmtDate(r.receivedAt, 'table')}</td><ViewCell />
            </>
          )}
        />
      </div>
    </main>
  );
}
