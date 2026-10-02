'use client';
// Page 1: Pipeline Control + Leads Overview.
import { useState } from 'react';
import { Icon } from './Icon';
import { OpenRow } from './OpenRow';
import { useData, useSection, visibleLeads } from './DataProvider';
import { StatusBadge, SourceCell, Skeleton, ErrorBox, CardHead, ViewCell, DataTable } from './ui';
import * as api from '@/core/api';
import { pad, fmtDate, SOURCES } from '@/core/format';

// ---------- Pipeline Control ----------
function PipelineControl() {
  const { status, data, reload } = useSection('pipelines');
  const { update } = useData();
  const [error, setError] = useState('');

  async function act(action, id) {
    const ids = id === 'all' ? data.map((p) => p.id) : [id];
    const prev = new Map(data.map((p) => [p.id, p.status]));
    const apply = (next) => update('pipelines', (list) => list.map((p) => (ids.includes(p.id) ? { ...p, status: next(p) } : p)));
    setError('');
    // Optimistic update so the badge and buttons change instantly.
    apply(() => (action === 'start' ? 'running' : 'paused'));
    try {
      await Promise.all(ids.map((i) => (action === 'start' ? api.startPipeline(i) : api.pausePipeline(i))));
    } catch {
      apply((p) => prev.get(p.id));
      setError(`Couldn't ${action} the pipeline. Please try again.`);
    }
  }

  const off = status !== 'ready';
  let cards;
  if (status === 'idle' || status === 'loading') cards = [0, 1].map((i) => <div key={i} className="card"><Skeleton rows={3} /></div>);
  else if (status === 'error') cards = <div className="card span2"><ErrorBox what="pipelines" onRetry={reload} /></div>;
  else {
    cards = data.map((p) => {
      const running = p.status === 'running';
      return (
        <div key={p.id} className="card pipe">
          <div className="card-head">
            <div className="tile"><Icon name={p.id === 'email' ? 'mail' : 'doc'} /></div>
            <div><div className="card-title">{p.name}</div><StatusBadge text={running ? 'Running' : 'Paused'} /></div>
          </div>
          <p className="desc">{p.description}</p>
          <div className="row pipe-btns">
            <button className="btn primary" onClick={() => act('start', p.id)}><Icon name="play" /> Start</button>
            <button className="btn outline" onClick={() => act('pause', p.id)}><Icon name="pause" /> Pause</button>
          </div>
        </div>
      );
    });
  }

  return (
    <section aria-label="Pipeline control">
      <div className="section-head">
        <h2 className="section-title">Pipeline Control</h2>
        <div className="row">
          <button className="btn primary" disabled={off} onClick={() => act('start', 'all')}><Icon name="play" /> Start All</button>
          <button className="btn outline" disabled={off} onClick={() => act('pause', 'all')}><Icon name="pause" /> Pause All</button>
        </div>
      </div>
      {error && <div className="notice">{error}</div>}
      <div className="grid2">{cards}</div>
    </section>
  );
}

// ---------- Leads Overview: stat cards + source breakdown ----------
const STATS = [
  { key: 'total', label: 'Total Leads', ic: 'users', tone: '' },
  { key: 'awaiting', label: 'Awaiting Response', ic: 'clock', tone: 'lavender' },
  { key: 'responded', label: 'Responded', ic: 'reply', tone: 'green' },
];

function StatCards({ stats }) {
  const { status, data, reload } = stats;
  return (
    <div className="grid3">
      {STATS.map(({ key, label, ic, tone }) => (
        <div key={key} className="card stat">
          <div className={`tile round ${tone}`}><Icon name={ic} /></div>
          <div>
            <div className="muted">{label}</div>
            {status === 'idle' || status === 'loading' ? <div className="skel" style={{ height: 30, width: 90 }} />
              : status === 'error' ? <button className="btn" onClick={reload}>Retry</button>
              : <div className="big">{data[key]}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

function SourceBreakdown({ stats }) {
  const { status, data, reload } = stats;
  const keys = Object.keys(SOURCES);
  let body;
  if (status === 'idle' || status === 'loading') body = <Skeleton rows={3} />;
  else if (status === 'error') body = <ErrorBox what="lead sources" onRetry={reload} />;
  else {
    const { bySource } = data;
    body = (
      <>
        <div className="stack" role="img" aria-label={keys.map((k) => `${SOURCES[k].label} ${bySource[k]}`).join(', ')}>
          {keys.map((k) => <span key={k} style={{ flexGrow: bySource[k], background: SOURCES[k].color }} title={`${SOURCES[k].label}: ${bySource[k]}`} />)}
        </div>
        <div className="legend">
          {keys.map((k) => (
            <div key={k} className="legend-item">
              <Icon name={SOURCES[k].icon} />
              <span className="legend-label">{SOURCES[k].label}</span><span className="legend-count">{bySource[k]}</span>
            </div>
          ))}
        </div>
      </>
    );
  }
  return <div className="card"><CardHead icon="users" title="Leads by Source" />{body}</div>;
}

// ---------- Leads table ----------
function Chip({ group, value, children }) {
  const { filter, setFilter } = useData();
  const on = filter[group] === value;
  return <button className={`chip${on ? ' active' : ''}`} aria-pressed={on} onClick={() => setFilter((f) => ({ ...f, [group]: value }))}>{children}</button>;
}

function LeadsTable() {
  const { status, data, reload } = useSection('leads');
  const { filter } = useData();
  const list = visibleLeads(data, filter);
  return (
    <div className="card">
      {status === 'ready' && data.length > 0 && (
        <div className="filters">
          <div className="chip-group">
            <Chip group="status" value="all">All</Chip>
            <Chip group="status" value="awaiting">Awaiting</Chip>
            <Chip group="status" value="responded">Responded</Chip>
          </div>
          <div className="chip-group">
            <Chip group="source" value="all">All sources</Chip>
            {Object.entries(SOURCES).map(([k, v]) => <Chip key={k} group="source" value={k}>{v.label}</Chip>)}
          </div>
        </div>
      )}
      <DataTable
        status={status}
        head={<><th>#</th><th>Name</th><th>Source</th><th>Status</th><th>Date Added</th><th className="view">View</th></>}
        rows={list.map((l) => (
          <OpenRow key={l.id} kind="lead" id={l.id}>
            <td className="num">{pad(l.number)}</td><td className="name">{l.name}</td><td><SourceCell source={l.source} /></td>
            <td><StatusBadge text={l.status} /></td><td className="muted">{fmtDate(l.addedAt, 'date')}</td><ViewCell />
          </OpenRow>
        ))}
        emptyIcon="users" emptyText={data.length ? 'No leads match these filters' : 'No leads yet'} what="leads" onRetry={reload}
      />
    </div>
  );
}

export default function DashboardView() {
  const stats = useSection('stats');
  return (
    <main className="page">
      <PipelineControl />
      <section aria-label="Leads overview">
        <div className="section-head"><h2 className="section-title">Leads Overview</h2></div>
        <div className="stack-v">
          <StatCards stats={stats} />
          <div><SourceBreakdown stats={stats} /></div>
          <div><LeadsTable /></div>
        </div>
      </section>
    </main>
  );
}
