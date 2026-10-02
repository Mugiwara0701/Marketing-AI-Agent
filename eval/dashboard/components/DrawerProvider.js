'use client';
// The ONE drawer used for leads, sent emails, replies and blogs. Pages call open(kind, id) (or goto(page, kind, id)
// to switch page first); what each kind shows lives in drawerKinds.js.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { Icon } from './Icon';
import { Skeleton, ErrorBox } from './ui';
import { FormattedContent } from './FormattedContent';
import { useData } from './DataProvider';
import { getKinds } from './drawerKinds';

const DrawerCtx = createContext(null);
export const useDrawer = () => useContext(DrawerCtx);

export function DrawerProvider({ children }) {
  const data = useData();
  const router = useRouter();
  const pathname = usePathname();
  const [s, setS] = useState({ open: false, kind: null, id: null, status: 'idle', detail: null });

  const rootRef = useRef(null);
  const bodyRef = useRef(null);
  const openRef = useRef(false);
  const token = useRef(0); // guards against out-of-order responses when stepping quickly
  const returnTo = useRef(null);
  const pending = useRef(null); // drawer item to open once the target page has mounted
  const gotoRef = useRef(null);

  const kinds = useMemo(() => getKinds(data, (...a) => gotoRef.current(...a)), [data]);
  const kindsRef = useRef(kinds);
  kindsRef.current = kinds;
  const ensureRef = useRef(data.ensure);
  ensureRef.current = data.ensure;

  const open = useCallback(async (kind, id) => {
    const k = kindsRef.current[kind];
    ensureRef.current(k.section);
    if (!openRef.current) returnTo.current = document.activeElement;
    openRef.current = true;
    setS({ open: true, kind, id, status: 'loading', detail: null });
    const mine = ++token.current;
    try {
      const detail = await k.load(id);
      if (mine === token.current) setS((p) => ({ ...p, status: 'ready', detail }));
    } catch {
      if (mine === token.current) setS((p) => ({ ...p, status: 'error' }));
    }
  }, []);

  const close = useCallback(() => {
    if (!openRef.current) return;
    token.current++;
    openRef.current = false;
    setS((p) => ({ ...p, open: false }));
    if (returnTo.current?.isConnected) returnTo.current.focus({ preventScroll: true });
    returnTo.current = null;
  }, []);

  // "page, kind, id" -> switch to that page (if needed) and open the item.
  gotoRef.current = (page, kind, id) => {
    if (pathname === `/${page}`) { open(kind, id); return; }
    pending.current = { kind, id };
    router.push(`/${page}`);
  };

  // On navigation: open the pending item, otherwise close the drawer.
  useEffect(() => {
    const p = pending.current;
    if (p) { pending.current = null; open(p.kind, p.id); } else close();
  }, [pathname, open, close]);

  const items = s.kind ? kinds[s.kind].list() : [];
  const index = items.findIndex((x) => x.id === s.id);

  const step = (delta) => {
    const next = items[index + delta];
    if (next) open(s.kind, next.id);
  };

  useEffect(() => {
    if (!s.open) return undefined;
    const onKey = (e) => {
      const root = rootRef.current;
      if (e.key === 'Escape') close();
      else if (e.key === 'Tab') { // keep focus inside the open drawer
        const f = [...root.querySelectorAll('button:not(:disabled), a[href]')];
        if (!f.length) return;
        const first = f[0]; const last = f[f.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === root)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [s.open, close]);

  // Keep keyboard focus inside the drawer when content swaps (e.g. the focused Previous/Next just became disabled).
  useEffect(() => {
    const root = rootRef.current;
    if (!s.open || !root) return;
    const a = document.activeElement;
    if (!root.contains(a)) root.focus({ preventScroll: true });
    else if (a.disabled) root.querySelector('[aria-label="Close"]')?.focus();
  }, [s.open, s.status, s.id]);

  useEffect(() => { bodyRef.current?.scrollTo(0, 0); }, [s.status, s.id]);

  const ctx = useMemo(() => ({
    open, close,
    goto: (...a) => gotoRef.current(...a),
    isSelected: (kind, id) => s.open && s.kind === kind && s.id === id,
  }), [open, close, s.open, s.kind, s.id]);

  const k = s.kind ? kinds[s.kind] : null;
  const v = k && s.status === 'ready' ? k.describe(s.detail) : null;

  return (
    <DrawerCtx.Provider value={ctx}>
      {children}
      <div className={`overlay${s.open ? ' open' : ''}`} onClick={close} />
      <aside className={`drawer${s.open ? ' open' : ''}`} ref={rootRef} role="dialog" aria-modal="true" aria-label="Details" aria-hidden={!s.open} tabIndex={-1} inert={!s.open}>
        {k && (
          <>
            <div className="drawer-head">
              <h2>{k.heading}</h2>
              <button className="icon-btn" onClick={close} aria-label="Close"><Icon name="close" /></button>
            </div>
            <div className="drawer-body" ref={bodyRef}>
              <div className="title-row">
                <div>
                  {v ? (
                    <>
                      <div className="item-title">{v.title}</div>
                      <div className="item-meta"><span className="mono">ID: {s.detail.id}</span>{v.badge}</div>
                    </>
                  ) : (
                    <>
                      <div className="skel" style={{ height: 28, width: 150 }} />
                      <div className="skel" style={{ height: 14, width: 110, marginTop: 8 }} />
                    </>
                  )}
                </div>
                <div className="steppers">
                  <button className="sq" onClick={() => step(-1)} aria-label="Previous" disabled={index <= 0}><Icon name="chevL" /></button>
                  <button className="sq" onClick={() => step(1)} aria-label="Next" disabled={index < 0 || index >= items.length - 1}><Icon name="chevR" /></button>
                </div>
              </div>
              <hr />
              {s.status === 'loading' && <Skeleton rows={8} />}
              {s.status === 'error' && <ErrorBox what="details" onRetry={() => open(s.kind, s.id)} />}
              {v && (
                <>
                  <div className="meta">{v.rows}</div>
                  {v.action && <div className="action">{v.action}</div>}
                  {v.body != null && (
                    <>
                      <h3 className="body-title">{v.bodyTitle}</h3>
                      {v.bodyBox ? <div className="notes">{v.body}</div> : <div className="content"><FormattedContent text={v.body} /></div>}
                    </>
                  )}
                </>
              )}
            </div>
          </>
        )}
      </aside>
    </DrawerCtx.Provider>
  );
}
