'use client';
// Page 4: Positive Leads (leads whose reply was classified as interested).
import { OpenRow } from './OpenRow';
import { Icon } from './Icon';
import { useSection } from './DataProvider';
import { CardHead, SourceCell, ViewCell, DataTable } from './ui';
import { pad, fmtDate } from '@/core/format';

const Skel = <div className="skel" style={{ height: 30, width: 90 }} />;

function StatCards({ positive, replies }) {
  const loading = [positive, replies].some((s) => s.status === 'idle' || s.status === 'loading');
  const failed = [positive, replies].some((s) => s.status === 'error');
  const retry = () => [positive, replies].forEach((s) => s.status === 'error' && s.reload());
  const rate = replies.data.length ? `${((positive.data.length / replies.data.length) * 100).toFixed(1)}%` : '—';
  const stats = [
    { label: 'Positive Leads', ic: 'check', tone: 'green', value: positive.data.length, section: positive },
    { label: 'Replies Received', ic: 'chat', tone: '', value: replies.data.length, section: replies },
    { label: 'Positive Rate', ic: 'bolt', tone: 'lavender', value: rate, section: null },
  ];
  return (
    <div className="grid3">
      {stats.map(({ label, ic, tone, value, section }) => {
        const bad = section ? section.status === 'error' : failed;
        const wait = section ? ['idle', 'loading'].includes(section.status) : loading;
        return (
          <div key={label} className="card stat">
            <div className={`tile round ${tone}`}><Icon name={ic} /></div>
            <div>
              <div className="muted">{label}</div>
              {bad ? <button className="btn" onClick={retry}>Retry</button> : wait ? Skel : <div className="big">{value}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function PositiveLeadsView() {
  const positive = useSection('positive');
  const replies = useSection('replies');
  return (
    <main className="page">
      <div className="page-head"><h1 className="page-title">Positive Leads</h1><p className="muted">Leads who replied with interest (a call, pricing or a proposal).</p></div>
      <StatCards positive={positive} replies={replies} />
      <div className="card">
        <CardHead icon="reply" title="Leads to Follow Up" />
        <DataTable
          status={positive.status} emptyIcon="reply" emptyText="No positive leads yet" what="positive leads" onRetry={positive.reload}
          head={<><th>#</th><th>Name</th><th>Source</th><th>Replied On</th><th>Reply Preview</th><th className="view">View</th></>}
          rows={positive.data.map((l) => (
            <OpenRow key={l.id} kind="positiveLead" id={l.id}>
              <td className="num">{pad(l.number)}</td>
              <td><div className="name">{l.name}</div><div className="muted small">{l.company}</div></td>
              <td><SourceCell source={l.source} /></td>
              <td className="muted when">{fmtDate(l.repliedAt, 'table')}</td>
              <td><div className="ellip preview">{l.preview}</div></td>
              <ViewCell />
            </OpenRow>
          ))}
        />
      </div>
    </main>
  );
}
