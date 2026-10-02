'use client';
// Login page: email + password sign-in, with a forgot-password mode.
import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from './Icon';
import * as auth from '@/core/auth';

const COPY = {
  signin: { title: 'Welcome back', sub: 'Sign in to your Marketing AI Agent dashboard.', submit: 'Sign in' },
  reset: { title: 'Reset password', sub: "Enter your email and we'll send you a reset link.", submit: 'Send reset link' },
};

export default function LoginView() {
  const router = useRouter();
  const [mode, setMode] = useState('signin');
  const [busy, setBusy] = useState(false);
  const [showPw, setShowPw] = useState(false);
  const [msg, setMsg] = useState({ text: '', tone: 'error' });
  const emailRef = useRef(null);
  const passwordRef = useRef(null);
  const c = COPY[mode];

  // Signed-in users skip this page; the stylesheet hides the top bar and centres the card via body.auth-view.
  useEffect(() => {
  let cancelled = false;
  auth.isAuthenticated().then((ok) => {
    if (cancelled) return;
    if (ok) router.replace('/dashboard');
    else { document.body.classList.add('auth-view'); emailRef.current?.focus(); }
  });
  return () => { cancelled = true; document.body.classList.remove('auth-view'); };
}, [router]);

  const switchMode = () => { setMode(mode === 'signin' ? 'reset' : 'signin'); setMsg({ text: '', tone: 'error' }); };

  async function onSubmit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setMsg({ text: '', tone: 'error' });
    const email = emailRef.current.value.trim();
    try {
      if (mode === 'signin') {
        await auth.signIn(email, passwordRef.current.value);
        router.replace('/dashboard');
        return;
      }
      await auth.requestPasswordReset(email);
      setMsg({ text: 'If that email has an account, a reset link is on its way.', tone: 'ok' });
    } catch (err) {
      setMsg({ text: err.message || 'Something went wrong. Please try again.', tone: 'error' });
      (mode === 'signin' ? passwordRef : emailRef).current?.focus();
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="page">
      <div className="auth-card card">
        <div className="tile"><Icon name="bolt" /></div>
        <h1 className="auth-title">{c.title}</h1>
        <p className="auth-sub muted">{c.sub}</p>
        <form className="auth-form" noValidate onSubmit={onSubmit}>
          <label className="field"><span>Email</span>
            <input ref={emailRef} id="auth-email" name="email" type="email" autoComplete="username" placeholder="you@company.com" required />
          </label>
          <label className="field" hidden={mode === 'reset'}><span>Password</span>
            <span className="pw-wrap">
              <input ref={passwordRef} id="auth-password" name="password" type={showPw ? 'text' : 'password'} autoComplete="current-password" placeholder="At least 8 characters" required={mode === 'signin'} />
              <button className="pw-toggle" type="button" aria-label={showPw ? 'Hide password' : 'Show password'} aria-pressed={showPw} onClick={() => setShowPw(!showPw)}><Icon name="eye" /></button>
            </span>
          </label>
          <div className="auth-msg" role="alert" aria-live="polite" data-tone={msg.tone}>{msg.text}</div>
          <button className={`btn primary auth-submit${busy ? ' loading' : ''}`} type="submit" disabled={busy}>{c.submit}</button>
        </form>
        <button className="link-btn" type="button" onClick={switchMode}>{mode === 'signin' ? 'Forgot your password?' : 'Back to sign in'}</button>
      </div>
    </main>
  );
}
