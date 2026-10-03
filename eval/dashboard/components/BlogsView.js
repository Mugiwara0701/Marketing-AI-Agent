'use client';
// Page 3: Blogs (Blog Tracker).
import { OpenRow } from './OpenRow';
import { useSection } from './DataProvider';
import { CardHead, StatusBadge, SiteCell, ViewCell, DataTable } from './ui';
import { pad } from '@/core/format';

const StatBox = ({ label, value, color }) => (
  <div className="stat-box"><div className="muted small"><span className="dot" style={{ background: color }} />{label}</div><div className="big">{value}</div></div>
);

export default function BlogsView() {
  const { status, data, reload } = useSection('blogs');
  const ready = status === 'ready';
  const posted = data.filter((b) => b.status === 'Posted').length;
  return (
    <main className="page">
      <div className="page-head"><h1 className="page-title">Blogs</h1><p className="muted">All blog topics and where they were posted.</p></div>
      <div className="card">
        <div className="blog-top">
          <div>
            <CardHead icon="doc" title="Blog Tracker" />
            {ready && <div className="total-block"><div className="muted small">Total Blogs</div><div className="big">{data.length}</div></div>}
          </div>
          {ready && (
            <div className="stats">
              <StatBox label="Posted" value={posted} color="var(--green)" />
              <StatBox label="Not Posted" value={data.length - posted} color="var(--amber)" />
            </div>
          )}
        </div>
        <DataTable
          status={status} emptyIcon="doc" emptyText="No blogs yet" what="blogs" onRetry={reload}
          head={<><th>#</th><th>Blog Topic/Title</th><th>Status</th><th>Posted On</th><th className="view">View</th></>}
          rows={data.map((b) => (
            <OpenRow key={b.id} kind="blog" id={b.id}>
              <td className="num">{pad(b.number)}</td><td className="name"><div className="ellip wide">{b.title}</div></td>
              <td><StatusBadge text={b.status} /></td><td><SiteCell site={b.site} /></td><ViewCell />
            </OpenRow>
          ))}
        />
      </div>
    </main>
  );
}
