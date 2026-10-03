'use client';
// Holds every loadable section ({ status: 'idle' | 'loading' | 'ready' | 'error', data }) plus the leads filter,
// so state survives tab switches (Start/Pause, filters, loaded data) like the old module-level state did.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import * as api from '@/core/api';

const FETCHERS = {
  pipelines: api.getPipelines,
  stats: api.getLeadStats,
  leads: () => api.getLeads(),
  sent: api.getSentEmails,
  replies: api.getReplies,
  blogs: api.getBlogs,
  positive: api.getPositiveLeads,
};
const EMPTY = { pipelines: [], stats: null, leads: [], sent: [], replies: [], blogs: [], positive: [] };
const initial = () => Object.fromEntries(Object.keys(FETCHERS).map((k) => [k, { status: 'idle', data: EMPTY[k] }]));

export const visibleLeads = (leads, filter) => leads.filter((l) =>
  (filter.status === 'all' || l.status.toLowerCase() === filter.status) && (filter.source === 'all' || l.source === filter.source));

const DataCtx = createContext(null);
export const useData = () => useContext(DataCtx);

export function DataProvider({ children }) {
  const [st, setSt] = useState(initial);
  const [filter, setFilter] = useState({ status: 'all', source: 'all' });
  const busy = useRef(new Set());
  const started = useRef(new Set());

  const load = useCallback(async (key) => {
    if (busy.current.has(key)) return;
    busy.current.add(key);
    setSt((s) => ({ ...s, [key]: { ...s[key], status: 'loading' } }));
    try {
      const data = await FETCHERS[key]();
      setSt((s) => ({ ...s, [key]: { status: 'ready', data } }));
    } catch {
      setSt((s) => ({ ...s, [key]: { ...s[key], status: 'error' } }));
    } finally {
      busy.current.delete(key);
    }
  }, []);

  // Fetch a section only the first time it is needed (a failed section waits for an explicit retry).
  const ensure = useCallback((key) => {
    if (started.current.has(key)) return;
    started.current.add(key);
    load(key);
  }, [load]);

  const update = useCallback((key, fn) => setSt((s) => ({ ...s, [key]: { ...s[key], data: fn(s[key].data) } })), []);

  const value = useMemo(() => ({ st, filter, setFilter, load, ensure, update }), [st, filter, load, ensure, update]);
  return <DataCtx.Provider value={value}>{children}</DataCtx.Provider>;
}

// A section's state, loaded on first use.
export function useSection(key) {
  const { st, load, ensure } = useData();
  useEffect(() => { ensure(key); }, [key, ensure]);
  return { ...st[key], reload: () => load(key) };
}
