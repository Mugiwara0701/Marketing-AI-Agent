'use client';
// Shell for the signed-in pages: auth guard, top bar, shared data store and the single drawer.
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { Icon } from './Icon';
import { ErrorBox } from './ui';
import { DataProvider } from './DataProvider';
import { DrawerProvider } from './DrawerProvider';
import * as auth from '@/core/auth';
import { getMe } from '@/core/api';

const TABS = [
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/history', label: 'History' },
  { href: '/positive-leads', label: 'Positive Leads' },
  { href: '/blogs', label: 'Blogs' },
];

function Topbar() {
  const pathname = usePathname();
  const router = useRouter();

  // On narrow screens the tab row scrolls sideways; keep the active tab visible.
  useEffect(() => { document.querySelector('.tab.active')?.scrollIntoView({ inline: 'center', block: 'nearest' }); }, [pathname]);

  // Theme switch: persisted in localStorage, applied via data-theme on <html> (set early in app/layout.js).
  const toggleTheme = () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('theme', next); } catch { /* storage unavailable */ }
  };

  const signOut = async () => {
    await auth.signOut();
    router.replace('/login');
  };

  return (
    <header className="topbar">
      <div className="topbar-in">
        <nav className="tabs" aria-label="Main">
          {TABS.map((t) => {
            const on = pathname === t.href;
            return <Link key={t.href} className={`tab${on ? ' active' : ''}`} href={t.href} aria-current={on ? 'page' : undefined}>{t.label}</Link>;
          })}
        </nav>
        <button className="theme-toggle" type="button" aria-label="Toggle dark mode" onClick={toggleTheme}>
          <span className="moon"><Icon name="moon" /></span><span className="sun"><Icon name="sun" /></span>
        </button>
        <button className="btn signout" type="button" onClick={signOut}>Sign out</button>
      </div>
    </header>
  );
}

export function AppShell({ children }) {
  const router = useRouter();
  const [state, setState] = useState('checking'); // 'checking' | 'ready' | 'offline'
  const [attempt, setAttempt] = useState(0);

  // Guard: signed-out users only see the login page. The backend must also accept the Supabase token.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!(await auth.isAuthenticated())) { if (!cancelled) router.replace('/login'); return; }
      try {
        await getMe();
        if (!cancelled) setState('ready');
      } catch (err) {
        if (cancelled) return;
        if (err.status === 0) { setState('offline'); return; }
        await auth.signOut(); // backend rejected the token
        router.replace('/login');
      }
    })().catch(() => { if (!cancelled) router.replace('/login'); });
    return () => { cancelled = true; };
  }, [router, attempt]);

  if (state === 'offline') {
    return (
      <main className="page">
        <ErrorBox what="the server" onRetry={() => { setState('checking'); setAttempt((n) => n + 1); }} />
      </main>
    );
  }
  if (state !== 'ready') return null;
  return (
    <DataProvider>
      <DrawerProvider>
        <Topbar />
        {children}
      </DrawerProvider>
    </DataProvider>
  );
}
