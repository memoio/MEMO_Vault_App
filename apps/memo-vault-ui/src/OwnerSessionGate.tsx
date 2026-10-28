import { useEffect, useState, type ReactNode } from 'react';
import { setOwnerCsrf } from './ownerSession';
import { text as t } from './authorization.zh';

export default function OwnerSessionGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [checking, setChecking] = useState(true);
  const [token, setToken] = useState('');
  const [autoFilled, setAutoFilled] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const response = await fetch('/api/v1/auth/session');
        if (!response.ok) throw new Error('Vault API unavailable');
        const session = await response.json();
        if (!active) return;
        setOwnerCsrf(session.csrf_token ?? ''); setReady(session.authenticated === true);
        if (!session.authenticated && import.meta.env.DEV) {
          try {
            const local = await fetch('/__memo_vault_local_owner', { cache: 'no-store' });
            if (local.ok) {
              const data = await local.json();
              if (active && typeof data.token === 'string') {
                setToken(current => current || data.token);
                setAutoFilled(true);
              }
            }
          } catch { /* Manual entry remains available if local autofill is unavailable. */ }
        }
      } catch { if (active) setError('Vault API unavailable'); }
      finally { if (active) setChecking(false); }
    })();
    const expire = () => { setReady(false); setOwnerCsrf(''); };
    window.addEventListener('vault-session-expired', expire);
    return () => { active = false; window.removeEventListener('vault-session-expired', expire); };
  }, []);
  async function login() {
    setBusy(true); setError('');
    try {
      const response = await fetch('/api/v1/auth/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) });
      if (!response.ok) throw new Error(`Authentication failed (${response.status})`);
      const result = await response.json(); setOwnerCsrf(result.csrf_token); setToken(''); setReady(true);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Authentication failed'); }
    finally { setBusy(false); }
  }
  if (checking) return <main className="owner-login"><p>{t.checking}</p></main>;
  if (ready) return children;
  return <main className="owner-login"><form className="panel" onSubmit={e => { e.preventDefault(); void login(); }}>
    <h1>{t.ownerTitle}</h1><p>{t.ownerHint}</p>
    {autoFilled && <p className="subtle">已为本机开发测试自动填入凭证。</p>}
    <label>{t.ownerToken}<input type="password" value={token} autoComplete="off" onChange={e => setToken(e.target.value)} required /></label>
    {error && <p role="alert" className="error-banner">{error}</p>}
    <button className="primary-button" disabled={busy || !token}>{t.login}</button>
  </form></main>;
}
